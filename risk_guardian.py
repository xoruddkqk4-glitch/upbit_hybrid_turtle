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
#   [1] 하드 손절 (2N Stop — 최우선 처리)
#       현재가 ≤ stop_loss_price (마지막 매수가 - 2 × ATR)
#       → 추세 예측 실패 → 즉시 전량 매도
#
#   [2] 트레일링 스탑 (익절)
#       ① 10일 신저가 경신 → 무조건 청산
#       ② 5MA 하향 돌파 (수익권일 때만) → 익절 청산

import time
from typing import Optional

import indicator_calc
import telegram_alert
import trade_ledger
import upbit_client
from config import get_watchlist
from turtle_order_logic import load_position_state, save_position_state


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


def check_trailing_stop(ticker: str, current_price: float, pos: dict, indicators: dict):
    """트레일링 스탑 조건을 확인한다.

    조건 ①: 10일 신저가 경신 → 추세 종료, 무조건 청산
    조건 ②: 5MA 하향 돌파 + 수익권 → 익절

    Returns:
        청산 이유 문자열 또는 None.
    """
    name          = get_watchlist().get(ticker, {}).get("name", ticker)
    day10_low     = indicators.get("day10_low", 0.0)
    ma5           = indicators.get("ma5",       0.0)
    avg_buy_price = pos.get("avg_buy_price",    0.0)

    # 조건 ①: 10일 신저가 경신
    if day10_low > 0 and current_price <= day10_low:
        print(f"[risk_guardian] {name}({ticker}) 📉 10일 신저가 경신! "
              f"현재가 {current_price:,.0f}원 ≤ 10일 신저가 {day10_low:,.0f}원")
        return "10일 신저가 경신 익절"

    # 조건 ②: 5MA 하향 돌파 (수익권일 때만)
    if ma5 > 0 and current_price < ma5:
        if avg_buy_price > 0 and current_price > avg_buy_price:
            profit_pct = (current_price - avg_buy_price) / avg_buy_price * 100
            print(f"[risk_guardian] {name}({ticker}) 📉 5MA 하향 돌파 (수익권 익절)! "
                  f"현재가 {current_price:,.0f}원 < 5MA {ma5:,.0f}원 "
                  f"(수익률 +{profit_pct:.1f}%)")
            return "5MA 하향 돌파 익절"
        elif avg_buy_price > 0 and current_price <= avg_buy_price:
            print(f"[risk_guardian] {name}({ticker}) 5MA 아래지만 손실 구간 "
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

    # 실수령금액 = 매도 거래금액 - 수수료
    gross_amount = round(sell_price * executed_volume, 0)
    net_amount   = round(gross_amount - paid_fee, 0)

    # 수익금 = (매도가 - 평균 매입가) × 수량 - 수수료
    if avg_buy_price > 0:
        profit_amount = round((sell_price - avg_buy_price) * executed_volume - paid_fee, 0)
    else:
        profit_amount = ""

    exit_source_map = {
        "2N 하드 손절":          "EXIT_STOP",
        "10일 신저가 경신 익절": "EXIT_10LOW",
        "5MA 하향 돌파 익절":    "EXIT_5MA",
    }
    exit_source = exit_source_map.get(reason, "EXIT_STOP")

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

        print(f"[risk_guardian] {name}({ticker}) 감시 중 — 현재가: {current_price:,.0f}원 "
              f"| 손절가: {pos.get('stop_loss_price', 0):,.0f}원")

        # ① 하드 손절 먼저 확인
        if check_hard_stop(ticker, current_price, pos):
            place_exit_order(ticker, sellable_qty, "2N 하드 손절", current_price)
            continue

        # ② 트레일링 스탑 확인
        try:
            if indicators_map and ticker in indicators_map:
                indicators = indicators_map[ticker]
            else:
                time.sleep(0.3)
                indicators = indicator_calc.get_all_indicators(ticker)
            exit_reason = check_trailing_stop(ticker, current_price, pos, indicators)
            if exit_reason:
                place_exit_order(ticker, sellable_qty, exit_reason, current_price)
        except Exception as e:
            print(f"[risk_guardian] {ticker} 지표 계산 오류: {e}")

    print("[risk_guardian] 손절·익절 감시 완료")
