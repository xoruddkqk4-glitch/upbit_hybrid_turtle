# turtle_order_logic.py
# 터틀 트레이딩 주문 실행 모듈 (Upbit 버전)
#
# 역할:
#   1. 얼마나 살지 계산한다 (리스크 기반 Unit 수량 계산)
#   2. 진입 주문을 실행하고 포지션 상태를 기록한다 (1차 진입)
#   3. 가격이 일정 이상 오르면 추가로 산다 (피라미딩)
#   4. 진입 신호가 온 코인과 기존 보유 코인의 피라미딩을 통합 처리한다
#
# held_coin_record.json 구조:
# {
#   "KRW-BTC": {
#     "current_unit":          2,             ← 현재 몇 번 샀는지
#     "last_buy_price":        95000000.0,    ← 가장 최근에 산 가격 (손절·피라미딩 기준)
#     "avg_buy_price":         94500000.0,    ← 평균 매입 단가
#     "stop_loss_price":       93000000.0,    ← 이 가격 이하로 내려오면 손절
#     "next_pyramid_price":    95600000.0,    ← 이 가격 이상 오르면 추가 매수
#     "max_unit":              3,             ← 최대 추가 매수 횟수
#     "total_volume":          0.015,         ← 현재 보유 수량 합계 (코인 단위)
#     "entry_source":          "TURTLE_S1",  ← 1차 진입 경로
#     "effective_risk_factor": 0.0082,        ← 실제 적용 리스크 계수 (상한 조정 시 0.01 미만)
#   }
# }
#
# 주의: Upbit 은 코인 수량(volume) 을 소수점(8자리) 단위로 거래한다.
#       업비트 최소 주문금액(5,000원) 을 충족하지 않으면 주문을 스킵한다.

import json
import os
import time
from typing import Optional

import indicator_calc
import telegram_alert
import trade_ledger
import upbit_client
from config import get_watchlist

_DIR = os.path.dirname(os.path.abspath(__file__))
HELD_COIN_RECORD_FILE = os.path.join(_DIR, "held_coin_record.json")

# 업비트 최소 주문 금액 (원화) — 이보다 작은 주문은 거절된다
MIN_ORDER_KRW = 5000

# 한 코인 당 최대 Unit (피라미딩 상한)
MAX_UNIT_PER_COIN = 3

# 포트폴리오 전체 Unit 상한 (모든 코인의 current_unit 합계)
MAX_TOTAL_UNITS = 12

# 트레이드당 리스크 계수 (터틀 정석: 1%)
# 손절가 = 진입가 - 2 × ATR 이므로 2N 이탈 시 최대 손실 = RISK_PER_TRADE × 2 = 자본의 2%.
# 공격적으로 운용하려면 0.02 (최대 손실 4%) 로 올릴 수 있으나 권장하지 않는다.
RISK_PER_TRADE = 0.01

# ─────────────────────────────────────────
# 1 Unit당 최대 매수 금액 상한
# ─────────────────────────────────────────
#
# 이론 1U 명목가(= capital × RISK_PER_TRADE / ATR × price) 가 상한을 초과하면
# 해당 종목의 risk factor 를 낮춰 1U 명목가를 상한에 맞춘다.
# 조정된 risk factor(effective_risk_factor) 는 종목별로 다르며 held_coin_record 에 저장된다.

MAX_UNIT_KRW_RATIO = 0.10   # 1 Unit당 최대 매수 금액 = 총 자본 × 0.10


# ─────────────────────────────────────────
# 파일 입출력
# ─────────────────────────────────────────

def load_position_state() -> dict:
    """held_coin_record.json 을 읽어서 반환한다."""
    if os.path.exists(HELD_COIN_RECORD_FILE):
        try:
            with open(HELD_COIN_RECORD_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, IOError):
            print(f"[turtle] {HELD_COIN_RECORD_FILE} 읽기 오류 → 빈 상태로 시작")
    return {}


def save_position_state(state: dict):
    """포지션 상태를 held_coin_record.json 에 저장한다."""
    try:
        with open(HELD_COIN_RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"[turtle] 포지션 상태 저장 오류: {e}")


def get_total_units(state: dict) -> int:
    """포트폴리오 전체 Unit 합계를 반환한다."""
    return sum(pos.get("current_unit", 0) for pos in state.values())


# ─────────────────────────────────────────
# Unit 수량 계산
# ─────────────────────────────────────────

def calc_unit_size(ticker: str, price: float, atr_n: float, total_capital: float):
    """리스크 기반 Unit 수량(코인 개수)을 계산한다.

    이론 1U 명목가(= capital × RISK_PER_TRADE / ATR × price)가
    1U 최대 금액(총 자본 × MAX_UNIT_KRW_RATIO)을 초과하면
    해당 종목의 risk factor 를 낮춰 명목가를 상한에 맞춘다.

    Args:
        ticker:        업비트 티커 (로그용)
        price:         현재 1코인 가격 (원)
        atr_n:         ATR(N) 값 (원 단위)
        total_capital: 총 자본 (원)

    Returns:
        (volume, krw_amount, effective_risk_factor) 튜플 또는 None (스킵).
        effective_risk_factor: 실제 적용된 리스크 계수
                               (상한 미초과 시 RISK_PER_TRADE, 초과 시 그보다 작은 값)
    """
    name = get_watchlist().get(ticker, {}).get("name", ticker)

    if total_capital <= 0:
        print(f"[turtle] {name}({ticker}) 총자본 0 → 수량 계산 불가, 스킵")
        return None

    if atr_n <= 0:
        print(f"[turtle] {name}({ticker}) ATR(N)=0 → 수량 계산 불가, 스킵")
        return None

    if price <= 0:
        print(f"[turtle] {name}({ticker}) 현재가 {price} → 수량 계산 불가, 스킵")
        return None

    # ① 이론 1 Unit 수량·명목가 계산 (RISK_PER_TRADE = 1%)
    risk_volume = (total_capital * RISK_PER_TRADE) / atr_n
    if risk_volume <= 0:
        print(f"[turtle] {name}({ticker}) 계산 수량 0 (ATR={atr_n:,.0f}) → 스킵")
        return None
    risk_krw = risk_volume * price

    # ② 1 Unit 최대 매수 금액 상한 확인 및 effective_risk_factor 계산
    max_unit_krw = total_capital * MAX_UNIT_KRW_RATIO

    if risk_krw <= max_unit_krw:
        # 상한 미초과: 이론 수량 그대로 사용
        volume         = risk_volume
        krw_amount     = risk_krw
        effective_risk = RISK_PER_TRADE
        print(f"[turtle] {name}({ticker}) [정상] 1U {krw_amount:,.0f}원 "
              f"(자본 {krw_amount/total_capital*100:.1f}%) — 수량 {volume:.6f}, "
              f"리스크 {effective_risk*100:.2f}%")
    else:
        # 상한 초과: risk factor 를 낮춰 1U 금액을 max_unit_krw 에 맞춤
        # effective_risk = MAX_UNIT_KRW_RATIO × ATR / price (역산 공식)
        volume         = max_unit_krw / price
        krw_amount     = max_unit_krw
        effective_risk = MAX_UNIT_KRW_RATIO * atr_n / price
        print(f"[turtle] {name}({ticker}) [상한 조정] 이론 1U {risk_krw:,.0f}원 "
              f"(자본 {risk_krw/total_capital*100:.1f}%) → 자본 {MAX_UNIT_KRW_RATIO*100:.0f}% "
              f"({krw_amount:,.0f}원) 로 축소 | 리스크 {effective_risk*100:.4f}%")

    # ③ 최소 주문 금액 체크
    if krw_amount < MIN_ORDER_KRW:
        print(f"[turtle] {name}({ticker}) 주문금액 {krw_amount:,.0f}원 < "
              f"업비트 최소 {MIN_ORDER_KRW:,}원 → 스킵")
        return None

    return (volume, krw_amount, effective_risk)


# ─────────────────────────────────────────
# 피라미딩 트리거 확인
# ─────────────────────────────────────────

def check_pyramid_trigger(ticker: str, current_price: float, pos: dict) -> bool:
    """피라미딩(추가 매수) 조건이 충족됐는지 확인한다.

    조건:
      ① 현재가 ≥ next_pyramid_price (마지막 매수가 + 0.5 × ATR)
      ② 아직 최대 Unit 에 도달하지 않음 (current_unit < max_unit)
    """
    current_unit       = pos.get("current_unit",       0)
    max_unit           = pos.get("max_unit",           MAX_UNIT_PER_COIN)
    next_pyramid_price = pos.get("next_pyramid_price", 0.0)

    if current_unit >= max_unit:
        return False
    if current_price < next_pyramid_price:
        return False

    name = get_watchlist().get(ticker, {}).get("name", ticker)
    print(f"[turtle] {name}({ticker}) 피라미딩 조건 충족! "
          f"현재가 {current_price:,.0f}원 ≥ 피라미딩 기준 {next_pyramid_price:,.0f}원 "
          f"(현재 {current_unit}/{max_unit} Unit)")
    return True


# ─────────────────────────────────────────
# 주문 실행
# ─────────────────────────────────────────

def place_entry_order(
    ticker: str, volume: float, krw_amount: float,
    price: float, atr_n: float, max_unit: int,
    entry_source:          str   = "TARGET_30MIN",
    effective_risk_factor: float = RISK_PER_TRADE,
):
    """1차 진입 주문을 실행하고 포지션 상태를 기록한다.

    effective_risk_factor: calc_unit_size() 가 계산한 실제 적용 리스크 계수.
                           상한 미초과 시 RISK_PER_TRADE(0.01), 초과 시 그보다 작은 값.
    """
    watchlist = get_watchlist()
    if ticker not in watchlist:
        print(f"[turtle] {ticker} 감시 코인 외 → 진입 주문 거부")
        return

    name = watchlist[ticker]["name"]

    # 매수: Upbit 은 시장가 매수 시 KRW 금액을 지정한다
    result = upbit_client.place_order(
        ticker, volume=volume, side="BUY",
        order_type="MARKET", krw_amount=krw_amount,
    )
    if not result["success"]:
        msg = (f"⚠️ 진입 주문 실패\n"
               f"코인: {name}({ticker})\n"
               f"금액: {krw_amount:,.0f}원\n"
               f"오류: {result['message']}")
        print(f"[turtle] {msg}")
        telegram_alert.SendMessage(msg)
        return

    order_no = result["order_no"]
    executed_price  = result.get("executed_price", price) or price
    executed_volume = result.get("executed_volume", volume) or volume

    # 손절가·피라미딩가 계산 (체결가 기준)
    stop_loss_price    = executed_price - 2.0 * atr_n
    next_pyramid_price = executed_price + 0.5 * atr_n

    position_state = load_position_state()
    position_state[ticker] = {
        "current_unit":          1,
        "last_buy_price":        executed_price,
        "avg_buy_price":         executed_price,
        "stop_loss_price":       stop_loss_price,
        "next_pyramid_price":    next_pyramid_price,
        "max_unit":              max_unit,
        "total_volume":          executed_volume,
        "entry_source":          entry_source,
        "effective_risk_factor": effective_risk_factor,
    }
    save_position_state(position_state)

    # 체결 원장 기록
    source_map = {
        "TARGET_30MIN": "ENTRY_30MIN",
        "TURTLE_S1":    "ENTRY_S1",
        "TURTLE_S2":    "ENTRY_S2",
    }
    ledger_source = source_map.get(entry_source, "ENTRY_30MIN")

    # 상한 조정 여부에 따라 표시 레이블 결정
    risk_label = (
        f"리스크 {effective_risk_factor*100:.2f}%"
        if effective_risk_factor >= RISK_PER_TRADE
        else f"리스크 {effective_risk_factor*100:.4f}% ↓상한조정"
    )

    trade_ledger.append_trade({
        "side":        "BUY",
        "ticker":      ticker,
        "coin_name":   name,
        "volume":      executed_volume,
        "unit_price":  executed_price,
        "order_no":    order_no,
        "order_type":  "MARKET",
        "source":      ledger_source,
        "note":        f"1차 진입({risk_label}) | 손절가: {stop_loss_price:,.0f}원 | "
                       f"다음 피라미딩: {next_pyramid_price:,.0f}원",
    })

    # 텔레그램 알림
    source_label = {
        "TURTLE_S2":    "터틀S2(55일신고가)",
        "TURTLE_S1":    "터틀S1(20일신고가)",
        "TARGET_30MIN": "목표가30분",
    }.get(entry_source, entry_source)
    telegram_alert.SendMessage(
        f"✅ 터틀 진입 [{risk_label}]\n"
        f"코인: {name}({ticker})\n"
        f"수량: {executed_volume:.8f}개 @{executed_price:,.0f}원\n"
        f"투입금액: {krw_amount:,.0f}원\n"
        f"진입 경로: {source_label} (최대 {max_unit} Unit)\n"
        f"손절가: {stop_loss_price:,.0f}원 | 다음 피라미딩: {next_pyramid_price:,.0f}원"
    )


def place_pyramid_order(ticker: str, volume: float, krw_amount: float,
                        price: float, atr_n: float):
    """피라미딩(추가 매수) 주문을 실행하고 포지션 상태를 업데이트한다."""
    watchlist = get_watchlist()
    if ticker not in watchlist:
        print(f"[turtle] {ticker} 감시 코인 외 → 피라미딩 주문 거부")
        return

    name = watchlist[ticker]["name"]

    position_state = load_position_state()
    if ticker not in position_state:
        print(f"[turtle] {ticker} held_coin_record.json 에 기록 없음 → 피라미딩 불가")
        return

    pos          = position_state[ticker]
    current_unit = pos.get("current_unit", 0)
    max_unit     = pos.get("max_unit",     MAX_UNIT_PER_COIN)

    if current_unit >= max_unit:
        print(f"[turtle] {name}({ticker}) 이미 최대 Unit ({current_unit}/{max_unit}) → 피라미딩 중단")
        return

    result = upbit_client.place_order(
        ticker, volume=volume, side="BUY",
        order_type="MARKET", krw_amount=krw_amount,
    )
    if not result["success"]:
        msg = (f"⚠️ 피라미딩 주문 실패\n"
               f"코인: {name}({ticker})\n"
               f"금액: {krw_amount:,.0f}원 ({current_unit + 1}차)\n"
               f"오류: {result['message']}")
        print(f"[turtle] {msg}")
        telegram_alert.SendMessage(msg)
        return

    order_no        = result["order_no"]
    executed_price  = result.get("executed_price", price) or price
    executed_volume = result.get("executed_volume", volume) or volume

    # 평균 매입단가 재계산 (가중 평균)
    old_total_vol  = pos.get("total_volume",  0.0)
    old_avg_price  = pos.get("avg_buy_price", executed_price)
    new_total_vol  = old_total_vol + executed_volume
    new_avg_price  = (
        (old_avg_price * old_total_vol + executed_price * executed_volume) / new_total_vol
        if new_total_vol > 0 else executed_price
    )

    new_unit              = current_unit + 1
    new_stop_loss_price   = executed_price - 2.0 * atr_n
    new_next_pyramid      = executed_price + 0.5 * atr_n

    position_state[ticker].update({
        "current_unit":       new_unit,
        "last_buy_price":     executed_price,
        "avg_buy_price":      new_avg_price,
        "stop_loss_price":    new_stop_loss_price,
        "next_pyramid_price": new_next_pyramid,
        "total_volume":       new_total_vol,
    })
    save_position_state(position_state)

    trade_ledger.append_trade({
        "side":        "BUY",
        "ticker":      ticker,
        "coin_name":   name,
        "volume":      executed_volume,
        "unit_price":  executed_price,
        "order_no":    order_no,
        "order_type":  "MARKET",
        "source":      "PYRAMID",
        "note":        f"{new_unit}차 피라미딩 | 손절가: {new_stop_loss_price:,.0f}원",
    })

    telegram_alert.SendMessage(
        f"📈 피라미딩\n"
        f"코인: {name}({ticker})\n"
        f"추가 수량: {executed_volume:.8f}개 @{executed_price:,.0f}원 "
        f"({new_unit}/{max_unit} Unit)\n"
        f"투입금액: {krw_amount:,.0f}원\n"
        f"평균 단가: {new_avg_price:,.0f}원\n"
        f"새 손절가: {new_stop_loss_price:,.0f}원 | 다음 피라미딩: {new_next_pyramid:,.0f}원"
    )


# ─────────────────────────────────────────
# 메인 실행 함수
# ─────────────────────────────────────────

def run_orders(
    entry_signals: list,
    total_capital: Optional[float] = None,
    krw_balance: Optional[float] = None,
    indicators_map: Optional[dict] = None,
):
    """진입 신호 처리 + 기존 포지션 피라미딩 체크 (메인 실행 함수).

    Args:
        entry_signals: timer_agent.run_timer_check() 반환 목록
                       예: [{"ticker": "KRW-BTC", "entry_source": "TURTLE_S1"}, ...]
    """
    print("[turtle] 주문 처리 시작")

    # ① 총 자본 조회
    if total_capital is None:
        total_capital = upbit_client.get_total_capital()
    if total_capital <= 0:
        print("[turtle] 총자본이 0원 → 주문 중단")
        return
    print(f"[turtle] 총 자본: {total_capital:,.0f}원")
    available_krw = upbit_client.get_krw_balance() if krw_balance is None else float(krw_balance)

    # ② 포지션 상태 불러오기
    position_state = load_position_state()
    held_tickers   = list(position_state.keys())

    # ③ 진입 신호 딕셔너리 변환
    entry_signal_map = {s["ticker"]: s["entry_source"] for s in entry_signals}
    signal_tickers   = list(entry_signal_map.keys())

    # ④ 현재가 조회 대상: 진입 신호 코인 + 기존 보유 코인
    watchlist = get_watchlist()
    price_query_tickers = list({
        t for t in (signal_tickers + held_tickers)
        if t in watchlist
    })

    if not price_query_tickers:
        print("[turtle] 처리할 코인 없음")
        return

    prices = upbit_client.get_multi_price(price_query_tickers)

    # ─────────────────────────────────────
    # [A] 신규 진입 처리
    # ─────────────────────────────────────
    for signal in entry_signals:
        ticker       = signal["ticker"]
        entry_source = signal["entry_source"]

        if ticker not in watchlist:
            print(f"[turtle] {ticker} 감시 코인 외 → 진입 스킵")
            continue

        if ticker in position_state:
            print(f"[turtle] {watchlist[ticker]['name']}({ticker}) 이미 보유 중 → 신규 진입 스킵")
            continue

        current_price = prices.get(ticker, 0.0)
        if current_price <= 0:
            print(f"[turtle] {ticker} 현재가 조회 실패 → 진입 스킵")
            continue

        # 지표 계산 (API 속도 제한 방지 대기)
        if indicators_map and ticker in indicators_map:
            indicators = indicators_map[ticker]
        else:
            time.sleep(0.3)
            indicators = indicator_calc.get_all_indicators(ticker)
        atr_n      = indicators.get("atr", 0.0)
        if atr_n <= 0:
            print(f"[turtle] {ticker} ATR(N)=0 → 진입 불가")
            continue

        # Unit 수량 계산 (상한 확인 + effective_risk_factor 산출)
        result = calc_unit_size(ticker, current_price, atr_n, total_capital)
        if result is None:
            continue

        volume, krw_amount, effective_risk_factor = result

        # 포트폴리오 전체 Unit 한도 확인
        fresh_state         = load_position_state()
        current_total_units = get_total_units(fresh_state)
        entry_name          = watchlist.get(ticker, {}).get("name", ticker)

        if current_total_units >= MAX_TOTAL_UNITS:
            print(f"[turtle] 포트폴리오 Unit 한도({MAX_TOTAL_UNITS}) 도달 → "
                  f"{entry_name}({ticker}) 신규 진입 스킵 (현재 {current_total_units} Unit)")
            continue

        # KRW 잔고가 부족하지 않은지 확인
        if available_krw < krw_amount:
            print(f"[turtle] {entry_name}({ticker}) KRW 잔고 부족 "
                  f"({available_krw:,.0f}원 < {krw_amount:,.0f}원) → 진입 스킵")
            continue

        place_entry_order(
            ticker, volume, krw_amount, current_price, atr_n,
            MAX_UNIT_PER_COIN, entry_source, effective_risk_factor,
        )
        # 같은 사이클 내 API 재조회 없이 가용 KRW를 로컬에서 차감 추적
        available_krw -= krw_amount

    # ─────────────────────────────────────
    # [B] 기존 포지션 피라미딩 처리
    # ─────────────────────────────────────
    position_state = load_position_state()

    for ticker, pos in list(position_state.items()):
        if ticker not in watchlist:
            continue

        # 수동 편입 종목(balance_sync 가 MANUAL_SYNC 로 등록)은 추가 매수 없음
        if pos.get("manual", False):
            continue

        current_price = prices.get(ticker, 0.0)
        if current_price <= 0:
            print(f"[turtle] {ticker} 현재가 조회 실패 → 피라미딩 스킵")
            continue

        # 파일 상태로 먼저 빠른 사전 검사
        if pos.get("current_unit", 0) >= pos.get("max_unit", MAX_UNIT_PER_COIN):
            continue
        if current_price < pos.get("next_pyramid_price", 0.0):
            continue

        if indicators_map and ticker in indicators_map:
            indicators = indicators_map[ticker]
        else:
            time.sleep(0.3)
            indicators = indicator_calc.get_all_indicators(ticker)
        atr_n      = indicators.get("atr", 0.0)
        if atr_n <= 0:
            continue

        if not check_pyramid_trigger(ticker, current_price, pos):
            continue

        result = calc_unit_size(ticker, current_price, atr_n, total_capital)
        if result is None:
            continue

        volume, krw_amount, _ = result

        # 포트폴리오 전체 Unit 한도 재확인
        fresh_state         = load_position_state()
        current_total_units = get_total_units(fresh_state)
        pyramid_name        = get_watchlist().get(ticker, {}).get("name", ticker)

        if current_total_units >= MAX_TOTAL_UNITS:
            print(f"[turtle] 포트폴리오 Unit 한도({MAX_TOTAL_UNITS}) 도달 → "
                  f"{pyramid_name}({ticker}) 피라미딩 스킵 (현재 {current_total_units} Unit)")
            continue

        if available_krw < krw_amount:
            print(f"[turtle] {pyramid_name}({ticker}) KRW 잔고 부족 "
                  f"({available_krw:,.0f}원 < {krw_amount:,.0f}원) → 피라미딩 스킵")
            continue

        place_pyramid_order(ticker, volume, krw_amount, current_price, atr_n)
        available_krw -= krw_amount

    print("[turtle] 주문 처리 완료")
