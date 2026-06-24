# risk_guardian.py
# 손절·익절 감시 모듈 (Upbit 버전)
#
# 역할:
#   현재 보유 중인 코인들을 감시하면서,
#   손실이 너무 커지면 강제로 팔고 (하드 손절),
#   수익이 충분히 났을 때 추세가 꺾이면 파는 (트레일링 스탑) 기능을 실행한다.
#
# 두 가지 청산 조건:
#
#   [1] 하드 손절 (고정 2N Stop — 최우선 처리)
#       현재가 ≤ stop_loss_price (마지막 매수가 - 2 × ATR)
#       → 진입 또는 피라미딩(추가 매수) 시점에 계산되어 저장된 고정 손절가를 사용하며,
#          장중에 가격이 상승하더라도 손절가를 추가로 상향하지 않는다.
#
#   [2] 트레일링 스탑 (익절)
#       ① 10일 신저가 경신 → 무조건 청산
#       ② 어제 일봉 종가 < 5MA (수익권일 때만) → 익절 청산
#          · 장중 휴쓰(whipsaw) 방지를 위해 어제 확정 종가를 기준으로 판단한다.
#          · 어제 종가·5MA는 하루 동안 변하지 않으므로, 업비트 일봉이 확정되는 09:00 이후
#            그날 첫 실행(09:06)에만 한 번 판단한다 (ma5_check_record.json 가드).

import json
import os
import time
from datetime import datetime
from typing import Optional

import pytz

import indicator_calc
import telegram_alert
import trade_ledger
import upbit_client
from config import get_watchlist
from turtle_order_logic import load_position_state, save_position_state

KST = pytz.timezone("Asia/Seoul")

# 5MA 익절 "하루 1회" 가드 파일 — 오늘 이미 5MA 익절 판단을 했는지 날짜로 기록
_DIR                  = os.path.dirname(os.path.abspath(__file__))
MA5_CHECK_RECORD_FILE = os.path.join(_DIR, "ma5_check_record.json")


# ───────────────────────────────────
# 5MA 익절 하루 1회 가드
# ───────────────────────────────────

def _is_ma5_check_done_today() -> bool:
    """오늘 이미 5MA 익절 판단을 했는지 확인한다.

    ma5_check_record.json 의 last_checked_date 가 오늘(KST) 이면 True.
    어제 종가·5MA는 하루 동안 변하지 않으므로, 하루 첫 실행 때만 5MA 익절을 판단하고
    그 이후 실행에서는 수익권 조건(현재가>평균가)의 장중 변동으로 인한 오판을 막는다.
    """
    if not os.path.exists(MA5_CHECK_RECORD_FILE):
        return False
    try:
        with open(MA5_CHECK_RECORD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        today = datetime.now(KST).strftime("%Y-%m-%d")
        return data.get("last_checked_date") == today
    except (json.JSONDecodeError, IOError):
        return False


def _mark_ma5_check_done_today():
    """오늘 5MA 익절 판단을 완료했다고 ma5_check_record.json 에 기록한다."""
    today = datetime.now(KST).strftime("%Y-%m-%d")
    try:
        with open(MA5_CHECK_RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_checked_date": today}, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"[risk_guardian] ma5_check_record.json 저장 오류: {e}")


# ─────────────────────────────────────────
# 청산 조건 확인
# ─────────────────────────────────────────

def check_hard_stop(ticker: str, current_price: float, pos: dict) -> bool:
    """하드 손절 조건을 확인한다.

    현재가가 미리 계산된 손절가(stop_loss_price) 이하로 내려온 경우
    즉시 전량 매도 신호를 반환한다.
    """
    stop_loss_price = pos.get("stop_loss_price", 0.0)
    if stop_loss_price <= 0:
        return False

    if current_price <= stop_loss_price:
        name = get_watchlist().get(ticker, {}).get("name", ticker)
        print(f"[risk_guardian] {name}({ticker}) ❌ 하드 손절 발동! "
              f"현재가 {current_price:,.0f}원 ≤ 손절가 {stop_loss_price:,.0f}원")
        return True

    return False


def check_trailing_stop(ticker: str, current_price: float, pos: dict, indicators: dict,
                        ma5_check_allowed: bool = True):
    """트레일링 스탑 조건을 확인한다.

    조건 ①: 10일 신저가 경신 → 추세 종료, 무조건 청산 (매 실행 실시간)
    조건 ②: 어제 일봉 종가 < 5MA + 수익권 → 익절 (하루 1회, ma5_check_allowed 일 때만)
         - 추세 꼬임 판단은 어제 확정 종가(prev_close) vs 어제까지 5MA(ma5_prev) 로 한다.
         - 수익권 판단(current_price > avg_buy_price)은 실시간 현재가로 한다.

    Args:
        ma5_check_allowed: True 일 때만 5MA 익절 조건을 평가한다 (하루 1회 가드).

    Returns:
        청산 이유 문자열 또는 None.
    """
    name          = get_watchlist().get(ticker, {}).get("name", ticker)
    day10_low     = indicators.get("day10_low",  0.0)
    ma5_prev      = indicators.get("ma5_prev",   0.0)
    prev_close    = indicators.get("prev_close", 0.0)
    avg_buy_price = pos.get("avg_buy_price",     0.0)

    # 조건 ①: 10일 신저가 경신 (매 실행 실시간 감시)
    if day10_low > 0 and current_price <= day10_low:
        print(f"[risk_guardian] {name}({ticker}) 📉 10일 신저가 경신! "
              f"현재가 {current_price:,.0f}원 ≤ 10일 신저가 {day10_low:,.0f}원")
        return "10일 신저가 경신 익절"

    # 조건 ②: 어제 일봉 종가가 5MA 하향 돌파 (하루 1회·수익권일 때만)
    if not ma5_check_allowed:
        return None

    if ma5_prev > 0 and prev_close > 0 and prev_close < ma5_prev:
        if avg_buy_price > 0 and current_price > avg_buy_price:
            profit_pct = (current_price - avg_buy_price) / avg_buy_price * 100
            print(f"[risk_guardian] {name}({ticker}) 📉 어제 종가 5MA 하향 돌파 (수익권 익절)! "
                  f"어제종가 {prev_close:,.0f}원 < 5MA {ma5_prev:,.0f}원 "
                  f"(현재 수익률 +{profit_pct:.1f}%)")
            return "5MA 하향 돌파 익절"
        elif avg_buy_price > 0 and current_price <= avg_buy_price:
            print(f"[risk_guardian] {name}({ticker}) 어제 종가 5MA 아래지만 손실 구간 "
                  f"→ 5MA 스탑 미적용 (하드 손절 대기)")

    return None


# ─────────────────────────────────────────
# 청산 주문 실행
# ─────────────────────────────────────────

def place_exit_order(ticker: str, volume: float, reason: str, current_price: float = 0.0):
    """전량 매도 주문을 실행하고 포지션을 청산한다."""
    watchlist = get_watchlist()
    if ticker not in watchlist:
        held = load_position_state()
        if ticker not in held:
            print(f"[risk_guardian] {ticker} 감시 코인 외 + 보유 기록 없음 → 매도 주문 거부")
            return

    name = watchlist.get(ticker, {}).get("name", ticker)

    if volume <= 0:
        print(f"[risk_guardian] {name}({ticker}) 매도 가능 수량=0 → 스킵")
        return

    result = upbit_client.place_order(
        ticker, volume=volume, side="SELL", order_type="MARKET",
    )
    if not result["success"]:
        msg = (f"⚠️ 청산 주문 실패\n"
               f"코인: {name}({ticker})\n"
               f"수량: {volume:.8f}\n"
               f"사유: {reason}\n"
               f"오류: {result['message']}")
        print(f"[risk_guardian] {msg}")
        telegram_alert.SendMessage(msg)
        return

    order_no        = result["order_no"]
    executed_price  = result.get("executed_price", current_price) or current_price
    executed_volume = result.get("executed_volume", volume) or volume
    paid_fee        = result.get("paid_fee", 0.0)  # Upbit 가 돌려준 실제 수수료

    # held_coin_record 에서 해당 코인 삭제
    position_state = load_position_state()
    removed_pos    = position_state.pop(ticker, {})
    save_position_state(position_state)

    avg_buy_price  = removed_pos.get("avg_buy_price",  0.0)
    last_buy_price = removed_pos.get("last_buy_price", 0.0)

    sell_price = executed_price if executed_price > 0 else last_buy_price

    if avg_buy_price > 0 and sell_price > 0:
        profit_rate = round((sell_price - avg_buy_price) / avg_buy_price * 100, 2)
    else:
        profit_rate = 0.0

    # 원장 분류(source)는 이름 변경 전 원래 reason 으로 먼저 결정 (변경 후 조회하면 맵에서 못 찾음)
    exit_source_map = {
        "2N 하드 손절":          "EXIT_STOP",
        "10일 신저가 경신 익절": "EXIT_10LOW",
        "5MA 하향 돌파 익절":    "EXIT_5MA",
    }
    exit_source = exit_source_map.get(reason, "EXIT_STOP")

    # 실제 수익이 손실이면 "익절" → "손절"로 표시 수정 (텔레그램·note 표시용)
    if "익절" in reason and profit_rate < 0:
        reason = reason.replace("익절", "손절")

    # 실수령금액 = 매도 거래금액 - 수수료
    gross_amount = round(sell_price * executed_volume, 0)
    net_amount   = round(gross_amount - paid_fee, 0)

    # 수익금 = (매도가 - 평균 매입가) × 수량 - 수수료
    if avg_buy_price > 0:
        profit_amount = round((sell_price - avg_buy_price) * executed_volume - paid_fee, 0)
    else:
        profit_amount = ""

    trade_ledger.append_trade({
        "side":          "SELL",
        "ticker":        ticker,
        "coin_name":     name,
        "volume":        executed_volume,
        "unit_price":    sell_price,
        "order_no":      order_no,
        "order_type":    "MARKET",
        "source":        exit_source,
        "fee":           paid_fee,
        "net_amount":    net_amount,    # 실수령금액 = 거래금액 - 수수료
        "profit_rate":   profit_rate,
        "profit_amount": profit_amount, # 수익금 = (매도가 - 평균매입가) × 수량 - 수수료
        "note":          reason,
    })

    emoji = "💰" if profit_rate >= 0 else "🔴"
    profit_sign = "+" if profit_rate >= 0 else ""

    # 수익금 표시 문구 (평균 매입가가 있을 때만)
    profit_amount_str = (
        f"\n수익금: {int(profit_amount):+,}원"
        if isinstance(profit_amount, (int, float)) else ""
    )

    telegram_alert.SendMessage(
        f"{emoji} 포지션 청산\n"
        f"코인: {name}({ticker})\n"
        f"수량: {executed_volume:.8f}개\n"
        f"평균 매입가: {avg_buy_price:,.0f}원 → 매도가: {sell_price:,.0f}원\n"
        f"수익률: {profit_sign}{profit_rate:.2f}%"
        f"{profit_amount_str}\n"
        f"실수령금액: {int(net_amount):,}원\n"
        f"사유: {reason}"
    )


def place_partial_exit_order(ticker: str, volume: float, reason: str, tp_source: str,
                             current_price: float = 0.0, mark_tp_5: bool = False, mark_tp_10: bool = False) -> bool:
    """분할 매도 주문을 실행하고 포지션 정보를 부분 갱신한다."""
    watchlist = get_watchlist()
    if ticker not in watchlist:
        held = load_position_state()
        if ticker not in held:
            print(f"[risk_guardian] {ticker} 감시 코인 외 + 보유 기록 없음 → 분할 매도 주문 거부")
            return False

    name = watchlist.get(ticker, {}).get("name", ticker)

    if volume <= 0:
        print(f"[risk_guardian] {name}({ticker}) 분할 매도 가능 수량=0 → 스킵")
        return False

    # 최소 주문 금액 안전장치 (업비트 5,000원 제한)
    approx_krw = volume * (current_price or 1.0)
    if approx_krw < 5000:
        print(f"[risk_guardian] {name}({ticker}) 분할 익절 예정 금액({approx_krw:,.0f}원)이 "
              f"업비트 최소 주문 금액 5,000원 미만 → 스킵")
        return False

    result = upbit_client.place_order(
        ticker, volume=volume, side="SELL", order_type="MARKET",
    )
    if not result["success"]:
        msg = (f"⚠️ 분할 익절 주문 실패\n"
               f"코인: {name}({ticker})\n"
               f"수량: {volume:.8f}\n"
               f"사유: {reason}\n"
               f"오류: {result['message']}")
        print(f"[risk_guardian] {msg}")
        telegram_alert.SendMessage(msg)
        return False

    order_no        = result["order_no"]
    executed_price  = result.get("executed_price", current_price) or current_price
    executed_volume = result.get("executed_volume", volume) or volume
    paid_fee        = result.get("paid_fee", 0.0)  # Upbit 가 돌려준 실제 수수료

    # held_coin_record 갱신 (수량 차감 및 플래그 세팅)
    position_state = load_position_state()
    if ticker in position_state:
        pos = position_state[ticker]
        old_vol = float(pos.get("total_volume", 0.0))
        new_vol = max(0.0, old_vol - executed_volume)
        pos["total_volume"] = new_vol
        
        if mark_tp_5:
            pos["tp_5_done"] = True
        if mark_tp_10:
            pos["tp_10_done"] = True
            
        save_position_state(position_state)
        avg_buy_price = pos.get("avg_buy_price", 0.0)
    else:
        avg_buy_price = 0.0

    sell_price = executed_price

    if avg_buy_price > 0 and sell_price > 0:
        profit_rate = round((sell_price - avg_buy_price) / avg_buy_price * 100, 2)
    else:
        profit_rate = 0.0

    # 실수령금액 = 매도 거래금액 - 수수료
    gross_amount = round(sell_price * executed_volume, 0)
    net_amount   = round(gross_amount - paid_fee, 0)

    # 수익금 = (매도가 - 평균 매입가) × 수량 - 수수료
    if avg_buy_price > 0:
        profit_amount = round((sell_price - avg_buy_price) * executed_volume - paid_fee, 0)
    else:
        profit_amount = ""

    trade_ledger.append_trade({
        "side":          "SELL",
        "ticker":        ticker,
        "coin_name":     name,
        "volume":        executed_volume,
        "unit_price":    sell_price,
        "order_no":      order_no,
        "order_type":    "MARKET",
        "source":        tp_source,
        "fee":           paid_fee,
        "net_amount":    net_amount,
        "profit_rate":   profit_rate,
        "profit_amount": profit_amount,
        "note":          reason,
    })

    emoji = "💰" if profit_rate >= 0 else "🔴"
    profit_sign = "+" if profit_rate >= 0 else ""

    profit_amount_str = (
        f"\n수익금: {int(profit_amount):+,}원"
        if isinstance(profit_amount, (int, float)) else ""
    )

    telegram_alert.SendMessage(
        f"{emoji} 포지션 분할 익절\n"
        f"코인: {name}({ticker})\n"
        f"수량: {executed_volume:.8f}개 매도\n"
        f"평균 매입가: {avg_buy_price:,.0f}원 → 매도가: {sell_price:,.0f}원\n"
        f"수익률: {profit_sign}{profit_rate:.2f}%"
        f"{profit_amount_str}\n"
        f"실수령금액: {int(net_amount):,}원\n"
        f"사유: {reason}"
    )
    return True


# ─────────────────────────────────────────
# 메인 실행 함수
# ─────────────────────────────────────────

def run_guardian(balance: Optional[list] = None, indicators_map: Optional[dict] = None):
    """전체 보유 코인의 손절·익절 조건을 감시하고 청산을 실행한다."""
    print("[risk_guardian] 손절·익절 감시 시작")

    if balance is None:
        try:
            balance = upbit_client.get_balance()
        except Exception as e:
            print(f"[risk_guardian] 잔고 조회 오류: {e}")
            return

    if not balance:
        print("[risk_guardian] 보유 코인 없음")
        return

    position_state = load_position_state()
    watchlist      = get_watchlist()

    # 5MA 익절 하루 1회 가드: 오늘 아직 5MA 판단을 안 했으면만 이번 실행에서 평가한다.
    # 업비트 일봉이 확정되는 09:00 이후 그날 첫 실행(09:06)에만 어제 종가·5MA를 한 번 비교한다.
    ma5_check_allowed = not _is_ma5_check_done_today()
    if ma5_check_allowed:
        print("[risk_guardian] 5MA 익절 판단: 오늘 첫 실행 → 어제 종가 vs 5MA 비교 수행")
    else:
        print("[risk_guardian] 5MA 익절 판단: 오늘 이미 수행됨 → 5MA 익절 조건 스킵 (10일신저가·2N손절은 계속 감시)")

    for item in balance:
        ticker        = item["ticker"]
        current_price = float(item["current_price"])
        sellable_qty  = float(item["sellable_qty"])

        # held_coin_record 에 없으면 수동 매수 코인으로 판단 → 스킵
        if ticker not in position_state:
            name = watchlist.get(ticker, {}).get("name", ticker)
            print(f"[risk_guardian] {name}({ticker}) held_coin_record.json 에 기록 없음 "
                  f"→ 수동 보유 코인으로 판단, 자동 청산 스킵")
            continue

        pos  = position_state[ticker]
        name = watchlist.get(ticker, {}).get("name", ticker)

        if ticker not in watchlist:
            print(f"[risk_guardian] {name}({ticker}) ⚠️ 감시 목록에서 제외됐지만 보유 중 "
                  f"→ 손절·익절 감시 계속")

        # 출력·트레일링 스탑 양쪽에서 쓸 지표를 미리 한 번만 가져온다
        indicators = {}
        try:
            if indicators_map and ticker in indicators_map:
                indicators = indicators_map[ticker]
            else:
                time.sleep(0.3)
                indicators = indicator_calc.get_all_indicators(ticker)
        except Exception as e:
            print(f"[risk_guardian] {ticker} 지표 계산 오류: {e}")
            indicators = {}



        # 3가지 매도 기준 가격 후보 수집 (가격이 위에 있을수록 먼저 발동)
        avg_buy_price   = pos.get("avg_buy_price", 0)
        stop_loss_price = pos.get("stop_loss_price", 0)
        day10_low       = indicators.get("day10_low", 0) or 0
        ma5_prev        = indicators.get("ma5_prev", 0)  or 0

        sell_candidates = []
        if stop_loss_price > 0:
            sell_candidates.append(("2N 하드손절", stop_loss_price))
        if day10_low > 0:
            sell_candidates.append(("10일 신저가", day10_low))
        # 5MA 익절은 하루 1회 허용 구간이고, 평균가보다 ma5_prev 가 높을 때만 표시 (수익권 익절 조건)
        if ma5_check_allowed and ma5_prev > 0 and avg_buy_price > 0 and ma5_prev > avg_buy_price:
            sell_candidates.append(("5MA 익절(어제종가기준)", ma5_prev))

        # 후보 중 가장 높은 가격 = 가장 먼저 발동될 매도 기준
        if sell_candidates:
            sell_candidates.sort(key=lambda x: x[1], reverse=True)
            top_label, top_price = sell_candidates[0]
            sell_str = f"매도기준: {top_price:,.0f}원 ({top_label})"
        else:
            sell_str = "매도기준: -"

        # 현재가 옆에 평균가 대비 수익률(%) 과 평가 수익금(원) 표시
        if avg_buy_price > 0:
            profit_pct    = (current_price - avg_buy_price) / avg_buy_price * 100
            profit_amount = (current_price - avg_buy_price) * sellable_qty
            profit_sign   = "+" if profit_pct >= 0 else ""
            profit_str    = f" ({profit_sign}{profit_pct:.2f}%, {profit_sign}{profit_amount:,.0f}원)"
        else:
            profit_str    = ""

        print(f"[risk_guardian] {name}({ticker}) 감시 중 — 현재가: {current_price:,.0f}원{profit_str} "
              f"| 평균가: {avg_buy_price:,.0f}원 "
              f"| {sell_str} "
              f"| 다음피라미딩가: {pos.get('next_pyramid_price', 0):,.0f}원")

        # ─────────────────────────────────────
        # [분할 익절 판정] 5% 및 10% 분할 익절
        # ─────────────────────────────────────
        if avg_buy_price > 0:
            tp_5_done = pos.get("tp_5_done", False)
            tp_10_done = pos.get("tp_10_done", False)

            # 1. 10% 이상 상승 구간
            if profit_pct >= 10.0 and not tp_10_done:
                # 갭상승 등으로 5% 익절을 거치지 않은 경우라도 10% 조건인 33%만 익절하고 두 플래그 모두 True 처리
                sell_vol = sellable_qty * 0.33
                success = place_partial_exit_order(
                    ticker, sell_vol, "10% 달성 분할 익절", "EXIT_TP_10",
                    current_price=current_price, mark_tp_5=True, mark_tp_10=True
                )
                if success:
                    pos["tp_5_done"] = True
                    pos["tp_10_done"] = True
                    pos["total_volume"] = max(0.0, float(pos.get("total_volume", 0.0)) - sell_vol)
                    sellable_qty = max(0.0, sellable_qty - sell_vol)

            # 2. 5% 이상 10% 미만 상승 구간
            elif profit_pct >= 5.0 and profit_pct < 10.0 and not tp_5_done:
                sell_vol = sellable_qty * 0.25
                success = place_partial_exit_order(
                    ticker, sell_vol, "5% 달성 분할 익절", "EXIT_TP_5",
                    current_price=current_price, mark_tp_5=True
                )
                if success:
                    pos["tp_5_done"] = True
                    pos["total_volume"] = max(0.0, float(pos.get("total_volume", 0.0)) - sell_vol)
                    sellable_qty = max(0.0, sellable_qty - sell_vol)

        # ① 하드 손절 먼저 확인
        if check_hard_stop(ticker, current_price, pos):
            place_exit_order(ticker, sellable_qty, "2N 하드 손절", current_price)
            position_state.pop(ticker, None)  # 루프 끝 저장 때 재등록 방지
            continue

        # ② 트레일링 스탑 확인 (지표는 위에서 이미 계산됨)
        try:
            exit_reason = check_trailing_stop(ticker, current_price, pos, indicators,
                                              ma5_check_allowed=ma5_check_allowed)
            if exit_reason:
                place_exit_order(ticker, sellable_qty, exit_reason, current_price)
                position_state.pop(ticker, None)  # 루프 끝 저장 때 재등록 방지
        except Exception as e:
            print(f"[risk_guardian] {ticker} 트레일링 스탑 확인 오류: {e}")

    # 청산되지 않은 코인들의 갱신된 최고가·손절가 저장
    save_position_state(position_state)

    # 오늘 5MA 익절 판단을 수행했으면 하루 1회 가드 기록 (이후 실행에서는 5MA 조건 스킵)
    if ma5_check_allowed:
        _mark_ma5_check_done_today()

    print("[risk_guardian] 손절·익절 감시 완료")
