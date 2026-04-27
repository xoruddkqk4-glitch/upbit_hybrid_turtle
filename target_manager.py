# target_manager.py
# 동적 목표가 산출 및 코인 상태 관리 모듈
#
# 역할:
#   1. 각 코인의 "동적 목표가(pending_target)" 를 계산한다
#      공식: max(현재가 × 1.02, 240분봉 20MA × 1.005)
#   2. 현재가가 목표가를 넘었는지 확인하고, 언제부터 넘었는지 기록한다 (30분 가드)
#   3. 오리지널 터틀 트레이딩 신호를 체크한다 (20일 / 55일 신고가 돌파)
#
# unheld_coin_record.json 구조:
# {
#   "KRW-BTC": {
#     "pending_target":      96000000.0,  ← 동적 목표가
#     "reference_price":     94000000.0,  ← 목표가 기준가 (이 아래로 가면 목표가 하향)
#     "above_target_since":  null,         ← null 이면 목표가 미달
#     "turtle_s1_signal":    false,        ← 시스템1(20일 신고가) 돌파 여부
#     "turtle_s2_signal":    false,        ← 시스템2(55일 신고가) 돌파 여부
#     "last_updated": "2026-04-15 10:00:00"
#   }
# }
#
# 목표가 관리 규칙:
#   처음 등록 시    : 현재가로 목표가와 기준가를 함께 계산해서 저장
#   현재가 ≥ 기준가 : 목표가 고정 — 올라가지 않음, 30분 타이머 계속 진행
#   현재가 < 기준가 : 가격 하락 → 목표가 하향 조정 + 기준가 갱신 + 타이머 초기화

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
# 목표가 계산
# ─────────────────────────────────────────

def calc_pending_target(ticker: str, current_price: float, indicators: dict) -> float:
    """동적 목표가(pending_target) 를 계산한다.

    공식: max(현재가 × 1.02, 240분봉 20MA × 1.005)

    - 현재가 × 1.02: 현재 가격에서 2% 이상 올라야 함
    - 240분봉 20MA × 1.005: 4시간봉 이동평균선보다 0.5% 위에 있어야 함
    둘 다 충족할 때만 추세적 상승으로 판단한다.

    Args:
        ticker:        업비트 티커 (로그 출력용)
        current_price: 현재가
        indicators:    get_all_indicators() 반환 딕셔너리

    Returns:
        동적 목표가 (float). 240분봉 데이터 없으면 현재가 × 1.02 만 사용.
    """
    target_by_price = current_price * 1.02

    ma240 = indicators.get("ma240_20", 0.0)

    if ma240 > 0:
        target_by_ma240 = ma240 * 1.005
        target = max(target_by_price, target_by_ma240)
    else:
        print(f"[target_manager] {ticker} 240분봉 데이터 없음 → 현재가 2% 기준만 사용")
        target = target_by_price

    return target


# ─────────────────────────────────────────
# 타이머 상태 관리
# ─────────────────────────────────────────

def update_above_target_time(ticker: str, current_price: float, coin_record: dict) -> dict:
    """현재가와 목표가를 비교해서 타이머 상태를 업데이트한다.

    현재가 ≥ pending_target → 타이머 시작 또는 유지
    현재가 < pending_target → 타이머 초기화 (null 로 리셋)
    """
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    pending_target = coin_record.get("pending_target", 0.0)

    if current_price >= pending_target:
        if coin_record.get("above_target_since") is None:
            coin_record["above_target_since"] = now_kst
            print(f"[target_manager] {ticker} 목표가 돌파! "
                  f"{current_price:,.0f}원 ≥ {pending_target:,.0f}원 → 30분 카운트 시작")
    else:
        if coin_record.get("above_target_since") is not None:
            print(f"[target_manager] {ticker} 목표가 이탈 "
                  f"({current_price:,.0f}원 < {pending_target:,.0f}원) → 타이머 초기화")
        coin_record["above_target_since"] = None

    coin_record["last_updated"] = now_kst
    return coin_record


# ─────────────────────────────────────────
# 메인 실행 함수
# ─────────────────────────────────────────

def run_update(balance: Optional[list] = None, indicators_map: Optional[dict] = None):
    """미보유 코인 목표가 갱신 + 터틀 신호 체크 (메인 실행 함수).

    실행 순서:
    1. 현재 보유 중인 코인 파악
    2. 미보유 코인에 대해:
       a. 일봉 60개 + 240분봉 조회
       b. 동적 목표가(pending_target) 계산·잠금 로직 적용
       c. 터틀 시스템1(20일), 시스템2(55일) 신고가 돌파 여부 기록
       d. 30분 가드 타이머 상태 업데이트
    3. 보유 중인 코인은 unheld_record 에서 제거
    """
    print("[target_manager] 목표가 갱신 시작")

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

        # ⑤ 코인별 목표가·터틀 신호 갱신
        for ticker in unheld_tickers:
            current_price = prices.get(ticker, 0.0)
            if current_price <= 0:
                print(f"[target_manager] {ticker} 현재가 조회 실패 → 스킵")
                continue

            # 지표 계산 (일봉 캐시 + 240분봉)
            # API 속도 제한 방지를 위해 코인 간 약간 대기
            if indicators_map and ticker in indicators_map:
                indicators = indicators_map[ticker]
            else:
                time.sleep(0.3)
                indicators = indicator_calc.get_all_indicators(ticker)

            # 터틀 신호 계산
            s1_high   = indicators.get("s1_high", 0.0)
            s2_high   = indicators.get("s2_high", 0.0)
            turtle_s1 = s1_high > 0 and current_price > s1_high
            turtle_s2 = s2_high > 0 and current_price > s2_high

            # ─── 목표가 결정 (잠금 + 하향 조정 로직) ────────────────────
            name = watchlist.get(ticker, {}).get("name", ticker)

            if ticker not in unheld_record:
                # 처음 등록
                new_target = calc_pending_target(ticker, current_price, indicators)
                unheld_record[ticker] = {
                    "pending_target":     new_target,
                    "reference_price":    current_price,
                    "above_target_since": None,
                    "turtle_s1_signal":   turtle_s1,
                    "turtle_s2_signal":   turtle_s2,
                    "last_updated":       None,
                }
            else:
                reference_price = unheld_record[ticker].get("reference_price", 0.0)

                # 구버전 호환
                if reference_price <= 0:
                    unheld_record[ticker]["reference_price"] = current_price
                    reference_price = current_price

                if current_price < reference_price:
                    # 현재가가 기준가 아래로 내려온 경우 → 목표가 하향 조정
                    old_target = unheld_record[ticker].get("pending_target", 0.0)
                    new_target = calc_pending_target(ticker, current_price, indicators)
                    unheld_record[ticker]["pending_target"]     = new_target
                    unheld_record[ticker]["reference_price"]    = current_price
                    unheld_record[ticker]["above_target_since"] = None
                    print(f"[target_manager] {name}({ticker}) 기준가 하락 "
                          f"({reference_price:,.0f}원 → {current_price:,.0f}원) "
                          f"목표가: {old_target:,.0f}원 → {new_target:,.0f}원 → 타이머 초기화")
                # 현재가 ≥ 기준가: 목표가 유지

                # 터틀 신호 갱신
                unheld_record[ticker]["turtle_s1_signal"] = turtle_s1
                unheld_record[ticker]["turtle_s2_signal"] = turtle_s2

            # 30분 가드 타이머 상태 업데이트
            unheld_record[ticker] = update_above_target_time(
                ticker, current_price, unheld_record[ticker]
            )

            # 로그 출력
            above_since     = unheld_record[ticker]["above_target_since"]
            pending_target  = unheld_record[ticker]["pending_target"]
            reference_price = unheld_record[ticker].get("reference_price", 0.0)
            s1_str          = "✅ S1" if turtle_s1 else "S1미달"
            s2_str          = "✅ S2" if turtle_s2 else "S2미달"
            status          = f"타이머({above_since}~)" if above_since else "목표가미달"
            print(f"[target_manager] {name}({ticker}) "
                  f"현재가:{current_price:,.0f}원 / 기준가:{reference_price:,.0f}원 / "
                  f"목표가:{pending_target:,.0f}원 / {s1_str} / {s2_str} / {status}")

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

    print("[target_manager] 목표가 갱신 완료")
