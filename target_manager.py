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

def _update_guard_status(pos: dict, prefix: str, current_price: float, atr: float, guard_seconds: int, now_str: str, name: str):
    """지정된 접두사(turtle_s1 or turtle_s2)의 시간 가드 상태를 갱신한다."""
    signal         = pos.get(f"{prefix}_signal", False)
    breakout_at    = pos.get(f"{prefix}_breakout_at")
    breakout_price = pos.get(f"{prefix}_breakout_price")
    limit_price    = pos.get(f"{prefix}_limit_price")
    target_price   = pos.get(f"{prefix}_target_price") or 0.0
    peak_price     = pos.get(f"{prefix}_peak_price")
    entry_ready    = pos.get(f"{prefix}_entry_ready", False)

    # 1. 미돌파 상태 (대기 중)
    if not signal:
        if current_price > target_price:
            # 최초 돌파 발생
            signal = True
            breakout_at = now_str
            breakout_price = target_price
            limit_price = breakout_price - 0.5 * atr
            peak_price = current_price
            entry_ready = False
            print(f"[target_manager] {name} {prefix.upper()} 최초 돌파! "
                  f"현재가: {current_price:,.0f}원 > 목표가: {target_price:,.0f}원 "
                  f"(마지노선: {limit_price:,.0f}원, 가드 시작)")
        else:
            # 돌파선 미달
            entry_ready = False
    
    # 2. 돌파 상태 (타이머 작동 중)
    else:
        # 최고가 갱신
        if peak_price is None or current_price > peak_price:
            peak_price = current_price

        # A. 마지노선 붕괴 체크
        if limit_price is not None and current_price < limit_price:
            # 붕괴 리셋 발생
            new_target = peak_price + 0.2 * atr
            print(f"[target_manager] {name} {prefix.upper()} 마지노선 붕괴! "
                  f"현재가: {current_price:,.0f}원 < 마지노선: {limit_price:,.0f}원 "
                  f"(타이머 리셋, 새로운 목표가: {new_target:,.0f}원)")
            signal = False
            breakout_at = None
            breakout_price = None
            limit_price = None
            peak_price = None
            entry_ready = False
            target_price = new_target
            
        # B. 시간 가드 체크
        else:
            try:
                # 경과 시간 계산
                fmt = "%Y-%m-%d %H:%M:%S"
                dt_breakout = datetime.strptime(breakout_at, fmt)
                dt_now = datetime.strptime(now_str, fmt)
                elapsed = (dt_now - dt_breakout).total_seconds()
            except Exception:
                elapsed = 0.0

            if elapsed >= guard_seconds:
                # C. 시간 만료 시 최종 판정
                if current_price >= breakout_price:
                    # 최종 안착 성공
                    entry_ready = True
                    print(f"[target_manager] {name} {prefix.upper()} 시간 안착 성공! "
                          f"현재가: {current_price:,.0f}원 >= 돌파선: {breakout_price:,.0f}원 "
                          f"(경과: {elapsed/60:.1f}분 / 가드: {guard_seconds/60:.1f}분)")
                else:
                    # 최종 복구 실패 리셋
                    new_target = peak_price + 0.2 * atr
                    print(f"[target_manager] {name} {prefix.upper()} 시간 만료 후 회복 실패! "
                          f"현재가: {current_price:,.0f}원 < 돌파선: {breakout_price:,.0f}원 "
                          f"(타이머 리셋, 새로운 목표가: {new_target:,.0f}원)")
                    signal = False
                    breakout_at = None
                    breakout_price = None
                    limit_price = None
                    peak_price = None
                    entry_ready = False
                    target_price = new_target
            else:
                # 대기 중
                entry_ready = False
                print(f"[target_manager] {name} {prefix.upper()} 시간 가드 대기 중... "
                      f"(경과: {elapsed/60:.1f}분 / 가드: {guard_seconds/60:.1f}분, "
                      f"현재가: {current_price:,.0f}원, 돌파선: {breakout_price:,.0f}원)")

    # 갱신된 값을 다시 딕셔너리에 덮어쓰기
    pos[f"{prefix}_signal"]         = signal
    pos[f"{prefix}_breakout_at"]     = breakout_at
    pos[f"{prefix}_breakout_price"]  = breakout_price
    pos[f"{prefix}_limit_price"]     = limit_price
    pos[f"{prefix}_target_price"]    = target_price
    pos[f"{prefix}_peak_price"]      = peak_price
    pos[f"{prefix}_entry_ready"]     = entry_ready


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
            atr_val   = indicators.get("atr", 0.0)

            if atr_val <= 0:
                print(f"[target_manager] {ticker} ATR 계산 불가 (0) → 스킵")
                continue

            name = watchlist.get(ticker, {}).get("name", ticker)

            if ticker not in unheld_record:
                # 처음 등록 — 신호 상태로 초기값 결정
                unheld_record[ticker] = {
                    "turtle_s1_signal":          False,
                    "turtle_s1_breakout_at":     None,
                    "turtle_s1_breakout_price":  None,
                    "turtle_s1_limit_price":     None,
                    "turtle_s1_target_price":    s1_high,
                    "turtle_s1_peak_price":      None,
                    "turtle_s1_entry_ready":     False,
                    "turtle_s2_signal":          False,
                    "turtle_s2_breakout_at":     None,
                    "turtle_s2_breakout_price":  None,
                    "turtle_s2_limit_price":     None,
                    "turtle_s2_target_price":    s2_high,
                    "turtle_s2_peak_price":      None,
                    "turtle_s2_entry_ready":     False,
                }
            
            pos = unheld_record[ticker]
            # 구버전 호환: 새 필드가 없는 JSON 대비 기본값으로 초기화
            pos.setdefault("turtle_s1_signal",          False)
            pos.setdefault("turtle_s1_breakout_at",     None)
            pos.setdefault("turtle_s1_breakout_price",  None)
            pos.setdefault("turtle_s1_limit_price",     None)
            pos.setdefault("turtle_s1_target_price",    s1_high)
            pos.setdefault("turtle_s1_peak_price",      None)
            pos.setdefault("turtle_s1_entry_ready",     False)
            
            pos.setdefault("turtle_s2_signal",          False)
            pos.setdefault("turtle_s2_breakout_at",     None)
            pos.setdefault("turtle_s2_breakout_price",  None)
            pos.setdefault("turtle_s2_limit_price",     None)
            pos.setdefault("turtle_s2_target_price",    s2_high)
            pos.setdefault("turtle_s2_peak_price",      None)
            pos.setdefault("turtle_s2_entry_ready",     False)

            # 일봉 갱신 등으로 신고가가 타겟가보다 더 상향되었을 때의 동기화
            if pos["turtle_s1_target_price"] is None or s1_high > pos["turtle_s1_target_price"]:
                pos["turtle_s1_target_price"] = s1_high
            if pos["turtle_s2_target_price"] is None or s2_high > pos["turtle_s2_target_price"]:
                pos["turtle_s2_target_price"] = s2_high

            # 상태 업데이트 (코인 가드: 1시간 = 3600초)
            _update_guard_status(pos, "turtle_s1", current_price, atr_val, 3600, now_kst, name)
            _update_guard_status(pos, "turtle_s2", current_price, atr_val, 3600, now_kst, name)

            # 콘솔 감시 로그 추가 (현재가 / S1돌파값 / S2돌파값 및 가드 진행률)
            s1_tgt = pos.get("turtle_s1_target_price", s1_high)
            s2_tgt = pos.get("turtle_s2_target_price", s2_high)
            
            def _get_status_desc(prefix):
                sig = pos.get(f"{prefix}_signal", False)
                ready = pos.get(f"{prefix}_entry_ready", False)
                if ready:
                    return "안착"
                if sig:
                    b_at = pos.get(f"{prefix}_breakout_at")
                    try:
                        elapsed = (datetime.strptime(now_kst, "%Y-%m-%d %H:%M:%S") - 
                                   datetime.strptime(b_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
                        return f"가드중({elapsed/60:.1f}분)"
                    except Exception:
                        return "가드중"
                return "대기"

            s1_status = _get_status_desc("turtle_s1")
            s2_status = _get_status_desc("turtle_s2")

            print(f"[target_manager] {name}({ticker}) 현재가:{current_price:,.0f}원 | "
                  f"S1목표:{s1_tgt:,.0f}원({s1_status}) | S2목표:{s2_tgt:,.0f}원({s2_status})")

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
