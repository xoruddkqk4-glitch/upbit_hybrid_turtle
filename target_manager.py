# target_manager.py
# 터틀 신호 감지 및 코인 상태 관리 모듈
#
# 역할:
#   1. 오리지널 터틀 트레이딩 신호를 체크한다 (20일 / 55일 신고가 돌파)
#   2. 신호가 처음 발생한 시각을 기록한다 (_since 필드)
#      → timer_agent.py 가 이 시각을 읽어 30분 가드를 판단한다
#
# unheld_coin_record.json 구조:
# {
#   "KRW-BTC": {
#     "turtle_s1_signal": false,       ← 시스템1(20일 신고가) 돌파 여부
#     "turtle_s2_signal": false,       ← 시스템2(55일 신고가) 돌파 여부
#     "turtle_s1_since":  null,        ← S1 신호가 처음 True 된 시각 (null 이면 신호 없음)
#     "turtle_s2_since":  null,        ← S2 신호가 처음 True 된 시각 (null 이면 신호 없음)
#     "last_updated": "2026-04-27 10:00:00"
#   }
# }
#
# _since 관리 규칙:
#   신호 False → True  : _since = 현재 시각 (타이머 시작)
#   신호 True  → True,  _since 있음 : 유지 (타이머 계속)
#   신호 True  → True,  _since 없음 : _since = 현재 시각 (구버전 파일 호환 — 타이머 재시작)
#   신호 True  → False : _since = None (타이머 초기화)

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
# _since 타임스탬프 관리
# ─────────────────────────────────────────

def _update_since(prev_signal: bool, new_signal: bool,
                  prev_since: Optional[str], now_kst: str) -> Optional[str]:
    """신호 변화에 따라 _since 타임스탬프를 갱신한다.

    False → True        : now_kst 로 새로 시작
    True  → True (있음) : 기존 since 유지
    True  → True (없음) : now_kst 로 시작 (구버전 파일 호환)
    * → False           : None 으로 초기화
    """
    if not new_signal:
        return None
    # new_signal == True
    if prev_signal and prev_since:
        return prev_since   # 기존 타이머 유지
    return now_kst          # 새로 시작 (False→True 또는 since 누락)


# ─────────────────────────────────────────
# 메인 실행 함수
# ─────────────────────────────────────────

def run_update(balance: Optional[list] = None, indicators_map: Optional[dict] = None):
    """미보유 코인 터틀 신호 갱신 (메인 실행 함수).

    실행 순서:
    1. 현재 보유 중인 코인 파악
    2. 미보유 코인에 대해:
       a. 터틀 시스템1(20일), 시스템2(55일) 신고가 돌파 여부 기록
       b. 신호 발생 시각(_since) 관리
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

            # 기존 기록에서 이전 신호 상태·since 불러오기
            prev = unheld_record.get(ticker, {})
            prev_s1       = prev.get("turtle_s1_signal", False)
            prev_s2       = prev.get("turtle_s2_signal", False)
            prev_s1_since = prev.get("turtle_s1_since")
            prev_s2_since = prev.get("turtle_s2_since")

            # _since 갱신
            new_s1_since = _update_since(prev_s1, new_s1, prev_s1_since, now_kst)
            new_s2_since = _update_since(prev_s2, new_s2, prev_s2_since, now_kst)

            unheld_record[ticker] = {
                "turtle_s1_signal": new_s1,
                "turtle_s2_signal": new_s2,
                "turtle_s1_since":  new_s1_since,
                "turtle_s2_since":  new_s2_since,
                "last_updated":     now_kst,
            }

            # 로그 출력
            s1_str = f"✅ S1(since {new_s1_since})" if new_s1 else "S1미달"
            s2_str = f"✅ S2(since {new_s2_since})" if new_s2 else "S2미달"
            print(f"[target_manager] {name}({ticker}) "
                  f"현재가:{current_price:,.0f}원 / {s1_str} / {s2_str}")

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
