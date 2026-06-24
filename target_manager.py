# target_manager.py
# 터틀 신호 감지 및 코인 상태 관리 모듈
#
# 역할:
#   1. 오리지널 터틀 트레이딩 신호를 체크한다 (20일 / 55일 신고가 돌파)
#   2. 돌파 후 최고값을 추적하고, 눌림→재돌파 진입 조건을 판단한다
#      → timer_agent.py 가 entry_ready 플래그를 읽어 진입 신호를 만든다
#
# unheld_coin_record.json 구조:
# {
#   "KRW-BTC": {
#     "turtle_s1_signal":      false,  ← 시스템1(20일 신고가) 돌파 여부
#     "turtle_s2_signal":      false,  ← 시스템2(55일 신고가) 돌파 여부
#     "turtle_s1_peak_price":  null,   ← S1 돌파 후 추적한 최고값 (null 이면 신호 없음)
#     "turtle_s1_peak_locked": false,  ← True = 눌림 구간 시작 (최고값 잠금)
#     "turtle_s1_entry_ready": false,  ← True = 눌림 후 최고값 재돌파 확인 → 진입 신호
#     "turtle_s2_peak_price":  null,
#     "turtle_s2_peak_locked": false,
#     "turtle_s2_entry_ready": false,
#     "last_updated": "2026-04-27 10:00:00"
#   }
# }
#
# peak 상태 관리 규칙:
#   신호 False → True         : peak_price = 현재가, locked = False (WATCHING 시작)
#   신호 True, WATCHING 중    : 현재가 >= peak → peak 갱신 / 현재가 < peak → locked = True (PULLBACK)
#   신호 True, PULLBACK 중    : 현재가 > peak → entry_ready = True (진입 신호)
#   신호 True  → False        : peak_price = None, locked = False, entry_ready = False (전체 초기화)

import json
import os
import time
from datetime import datetime
from typing import Optional

import pytz

import indicator_calc
import upbit_client
from config import get_watchlist

_DIR = os.path.dirname(os.path.abspath(__file__))
UNHELD_RECORD_FILE = os.path.join(_DIR, "unheld_coin_record.json")

KST = pytz.timezone("Asia/Seoul")


# ─────────────────────────────────────────
# 파일 입출력
# ─────────────────────────────────────────

def load_unheld_record() -> dict:
    """unheld_coin_record.json 을 읽어서 반환한다."""
    if os.path.exists(UNHELD_RECORD_FILE):
        try:
            with open(UNHELD_RECORD_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, IOError):
            print(f"[target_manager] {UNHELD_RECORD_FILE} 읽기 오류 → 새 파일로 시작")
    return {}


def save_unheld_record(record: dict):
    """미보유 코인 상태를 unheld_coin_record.json 에 저장한다."""
    try:
        with open(UNHELD_RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"[target_manager] 파일 저장 오류: {e}")


# ─────────────────────────────────────────
# peak 상태 관리
# ─────────────────────────────────────────

def _update_peak_state(
    prev_signal: bool,
    new_signal: bool,
    prev_peak: Optional[float],
    prev_locked: bool,
    current_price: float,
    prev_peak_time: Optional[str],
    now_str: str,
) -> tuple:
    """신호 변화와 현재가에 따라 peak 상태를 계산해서 반환한다.

    상태 흐름:
      신호 꺼짐             → 전체 초기화 (peak=None, locked=False, ready=False, peak_time=None)
      신호 새로 켜짐        → WATCHING 시작 (peak=현재가, locked=False, ready=False, peak_time=현재시간)
      WATCHING 중 상승      → peak 갱신 (ready=False, peak_time=현재시간)
      WATCHING 중 눌림 시작 → PULLBACK 진입 (locked=True, ready=False, peak_time 유지)
      PULLBACK 중 최고값 재돌파 → 진입 신호 (ready=True, peak_time 유지)
      PULLBACK 중 대기      → 그대로 유지 (ready=False, peak_time 유지)

    Returns:
        (new_peak_price, new_peak_locked, entry_ready, new_peak_time)
    """
    # 신호 꺼지면 전체 초기화
    if not new_signal:
        return None, False, False, None

    # 신호가 새로 켜진 경우 (False→True) 또는 peak 기록이 없는 경우
    if not prev_signal or prev_peak is None:
        return current_price, False, False, now_str

    # WATCHING 상태: 아직 잠금 전
    if not prev_locked:
        if current_price >= prev_peak:
            # 아직 상승 중 → 최고값 갱신
            return current_price, False, False, now_str
        else:
            # 최초 눌림 감지 → 최고값 잠금 (PULLBACK 시작), 고점 도달 시간 유지
            return prev_peak, True, False, prev_peak_time

    # PULLBACK 상태: 최고값 잠금 후
    if current_price > prev_peak:
        # 잠긴 최고값을 재돌파 → 진입 신호, 고점 도달 시간 유지
        return prev_peak, True, True, prev_peak_time
    else:
        # 아직 최고값 아래 → 대기, 고점 도달 시간 유지
        return prev_peak, True, False, prev_peak_time


# ─────────────────────────────────────────
# 메인 실행 함수
# ─────────────────────────────────────────

def run_update(balance: Optional[list] = None, indicators_map: Optional[dict] = None):
    """미보유 코인 터틀 신호 갱신 (메인 실행 함수).

    실행 순서:
    1. 현재 보유 중인 코인 파악
    2. 미보유 코인에 대해:
       a. 터틀 시스템1(20일), 시스템2(55일) 신고가 돌파 여부 기록
       b. 돌파 후 최고값(peak) 추적 → 눌림→재돌파 진입 조건 판단
    3. 보유 중인 코인은 unheld_record 에서 제거
    """
    print("[target_manager] 터틀 신호 갱신 시작")

    # ① 현재 보유 중인 코인 파악
    try:
        if balance is None:
            balance = upbit_client.get_balance()
        held_tickers = {item["ticker"] for item in balance}
    except Exception as e:
        print(f"[target_manager] 잔고 조회 오류: {e}")
        held_tickers = set()

    # ② 미보유 코인 목록
    watchlist      = get_watchlist()
    unheld_tickers = [t for t in watchlist if t not in held_tickers]

    if not unheld_tickers:
        print("[target_manager] 미보유 코인 없음 (모두 보유 중)")
    else:
        # ③ 현재가 한번에 조회
        prices = upbit_client.get_multi_price(unheld_tickers)

        # ④ 기존 상태 파일 불러오기
        unheld_record = load_unheld_record()

        now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

        # ⑤ 코인별 터틀 신호 갱신
        for ticker in unheld_tickers:
            current_price = prices.get(ticker, 0.0)
            if current_price <= 0:
                print(f"[target_manager] {ticker} 현재가 조회 실패 → 스킵")
                continue

            # 지표 계산 (일봉 캐시 활용)
            # API 속도 제한 방지를 위해 코인 간 약간 대기
            if indicators_map and ticker in indicators_map:
                indicators = indicators_map[ticker]
            else:
                time.sleep(0.3)
                indicators = indicator_calc.get_all_indicators(ticker)

            # 터틀 신호 계산
            s1_high   = indicators.get("s1_high", 0.0)
            s2_high   = indicators.get("s2_high", 0.0)
            new_s1    = s1_high > 0 and current_price > s1_high
            new_s2    = s2_high > 0 and current_price > s2_high

            name = watchlist.get(ticker, {}).get("name", ticker)

            # 기존 기록에서 이전 신호 상태·peak 정보 불러오기
            prev = unheld_record.get(ticker, {})
            prev_s1        = prev.get("turtle_s1_signal", False)
            prev_s2        = prev.get("turtle_s2_signal", False)
            prev_s1_peak   = prev.get("turtle_s1_peak_price")
            prev_s1_locked = prev.get("turtle_s1_peak_locked", False)
            prev_s1_time   = prev.get("turtle_s1_peak_time")
            prev_s2_peak   = prev.get("turtle_s2_peak_price")
            prev_s2_locked = prev.get("turtle_s2_peak_locked", False)
            prev_s2_time   = prev.get("turtle_s2_peak_time")

            # peak 상태 갱신 (눌림→재돌파 진입 조건 판단)
            s1_peak, s1_locked, s1_ready, s1_time = _update_peak_state(
                prev_s1, new_s1, prev_s1_peak, prev_s1_locked, current_price, prev_s1_time, now_kst
            )
            s2_peak, s2_locked, s2_ready, s2_time = _update_peak_state(
                prev_s2, new_s2, prev_s2_peak, prev_s2_locked, current_price, prev_s2_time, now_kst
            )

            unheld_record[ticker] = {
                "turtle_s1_signal":      new_s1,
                "turtle_s2_signal":      new_s2,
                "turtle_s1_peak_price":  s1_peak,
                "turtle_s1_peak_locked": s1_locked,
                "turtle_s1_entry_ready": s1_ready,
                "turtle_s1_peak_time":   s1_time,
                "turtle_s2_peak_price":  s2_peak,
                "turtle_s2_peak_locked": s2_locked,
                "turtle_s2_entry_ready": s2_ready,
                "turtle_s2_peak_time":   s2_time,
                "last_updated":           now_kst,
            }

            # 상태 레이블 생성 (로그 출력용)
            def _peak_state_str(signal, high, peak, locked, ready):
                if not signal:
                    return f"미달({high:,.0f}원)"
                state = "진입준비" if ready else ("PULLBACK" if locked else "WATCHING")
                peak_str = f"{peak:,.0f}원" if peak else "?"
                return f"✅ 돌파({high:,.0f}원) [{state} 최고값:{peak_str}]"

            s1_str = _peak_state_str(new_s1, s1_high, s1_peak, s1_locked, s1_ready)
            s2_str = _peak_state_str(new_s2, s2_high, s2_peak, s2_locked, s2_ready)
            print(f"[target_manager] {name}({ticker}) "
                  f"현재가:{current_price:,.0f}원 / S1:{s1_str} / S2:{s2_str}")

        # ⑥ 보유 중인 코인은 unheld_record 에서 제거
        for ticker in list(unheld_record.keys()):
            if ticker in held_tickers:
                print(f"[target_manager] {ticker} 보유 중 → unheld_record 에서 제거")
                del unheld_record[ticker]

        # ⑦ 감시 목록에서 빠진 코인도 제거
        for ticker in list(unheld_record.keys()):
            if ticker not in watchlist:
                print(f"[target_manager] {ticker} 감시 목록 외 → unheld_record 에서 제거")
                del unheld_record[ticker]

        # ⑧ 저장
        save_unheld_record(unheld_record)

    print("[target_manager] 터틀 신호 갱신 완료")
