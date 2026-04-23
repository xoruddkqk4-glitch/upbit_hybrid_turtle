# timer_agent.py
# 진입 신호 통합 모듈
#
# 역할:
#   두 가지 진입 경로를 통합해서 진입 신호 목록을 반환한다.
#
#   경로 A — 동적 목표가 + 30분 가드 (TARGET_30MIN):
#     target_manager.py 가 기록한 "above_target_since" 가 30분 이상 경과한 코인
#   경로 B — 오리지널 터틀 시스템1·2 신고가 돌파 (TURTLE_S1 / TURTLE_S2):
#     target_manager.py 가 기록한 turtle_s1_signal / turtle_s2_signal 이 True 인 코인
#     ※ 일봉 전략이므로 30분 가드 없이 즉시 신호 발생
#
# 반환 형식:
#   [{"ticker": "KRW-BTC", "entry_source": "TARGET_30MIN"}, ...]
#   entry_source: "TARGET_30MIN" / "TURTLE_S1" / "TURTLE_S2"
#
# 같은 코인이 여러 경로에 해당하면 우선순위 높은 것 하나만 포함:
#   TURTLE_S2 > TURTLE_S1 > TARGET_30MIN
#
# 사용법:
#   import timer_agent
#   entry_signals = timer_agent.run_timer_check()

from datetime import datetime, timedelta

import pytz

from config import get_watchlist
from target_manager import load_unheld_record

KST = pytz.timezone("Asia/Seoul")

# 가드 시간: 30분 (이 시간 이상 목표가 위에 있어야 진입 신호)
GUARD_MINUTES = 30


# ─────────────────────────────────────────
# 30분 경과 확인
# ─────────────────────────────────────────

def check_30min_passed(ticker: str, unheld_record: dict) -> bool:
    """해당 코인이 목표가 이상에서 30분 이상 머물렀는지 확인한다."""
    coin_data = unheld_record.get(ticker)
    if not coin_data:
        return False

    above_since_str = coin_data.get("above_target_since")
    if not above_since_str:
        return False

    try:
        above_since = KST.localize(
            datetime.strptime(above_since_str, "%Y-%m-%d %H:%M:%S")
        )
        now_kst = datetime.now(KST)
        elapsed = now_kst - above_since

        if elapsed >= timedelta(minutes=GUARD_MINUTES):
            elapsed_min = int(elapsed.total_seconds() / 60)
            name = get_watchlist().get(ticker, {}).get("name", ticker)
            print(f"[timer_agent] {name}({ticker}) ✅ 30분 가드 통과! "
                  f"({elapsed_min}분 동안 목표가 위 유지)")
            return True
        else:
            remaining     = timedelta(minutes=GUARD_MINUTES) - elapsed
            remaining_min = int(remaining.total_seconds() / 60) + 1
            name          = get_watchlist().get(ticker, {}).get("name", ticker)
            print(f"[timer_agent] {name}({ticker}) ⏳ 대기 중 "
                  f"(앞으로 약 {remaining_min}분 더 유지해야 함)")
            return False

    except (ValueError, TypeError) as e:
        print(f"[timer_agent] {ticker} 타이머 시각 파싱 오류: {e}")
        return False


# ─────────────────────────────────────────
# 메인 실행 함수
# ─────────────────────────────────────────

def run_timer_check() -> list:
    """진입 신호 코인 목록을 반환한다 (두 가지 경로 통합).

    같은 코인이 여러 경로에 해당하면 우선순위:
      TURTLE_S2 > TURTLE_S1 > TARGET_30MIN

    Returns:
        [{"ticker": "KRW-BTC", "entry_source": "TURTLE_S1"}, ...]
        진입 신호 없으면 빈 리스트.
    """
    print("[timer_agent] 진입 신호 체크 시작")

    unheld_record = load_unheld_record()
    if not unheld_record:
        print("[timer_agent] 미보유 코인 상태 파일 비어있음 "
              "(target_manager.run_update() 를 먼저 실행하세요)")
        return []

    watchlist = get_watchlist()

    # 코인별로 해당되는 신호 유형 수집 (우선순위 적용)
    # 우선순위: TURTLE_S2(0) > TURTLE_S1(1) > TARGET_30MIN(2)
    signal_priority: dict = {}

    for ticker, data in unheld_record.items():
        if ticker not in watchlist:
            print(f"[timer_agent] {ticker} 감시 코인 외 → 스킵")
            continue

        current_priority = signal_priority.get(ticker, (99, None))[0]

        # 경로 A: 30분 가드 통과
        if check_30min_passed(ticker, unheld_record):
            if 2 < current_priority:
                signal_priority[ticker] = (2, "TARGET_30MIN")
                current_priority = 2

        # 경로 C: 터틀 시스템1 — 20일 신고가 돌파
        if data.get("turtle_s1_signal", False):
            if 1 < current_priority:
                name = watchlist.get(ticker, {}).get("name", ticker)
                print(f"[timer_agent] {name}({ticker}) ✅ 터틀 S1 신호 (20일 신고가 돌파)")
                signal_priority[ticker] = (1, "TURTLE_S1")
                current_priority = 1

        # 경로 B: 터틀 시스템2 — 55일 신고가 돌파 (최우선)
        if data.get("turtle_s2_signal", False):
            if 0 < current_priority:
                name = watchlist.get(ticker, {}).get("name", ticker)
                print(f"[timer_agent] {name}({ticker}) ✅ 터틀 S2 신호 (55일 신고가 돌파)")
                signal_priority[ticker] = (0, "TURTLE_S2")

    entry_signals = [
        {"ticker": ticker, "entry_source": src}
        for ticker, (_, src) in signal_priority.items()
        if src is not None
    ]

    # 정렬: S2 → S1 → TARGET_30MIN(오래된 신호 먼저)
    def _sort_key(s: dict):
        src    = s["entry_source"]
        ticker = s["ticker"]
        if src == "TARGET_30MIN":
            return (2, unheld_record[ticker].get("above_target_since") or "")
        elif src == "TURTLE_S2":
            return (0, "")
        else:
            return (1, "")

    entry_signals.sort(key=_sort_key)

    if entry_signals:
        for s in entry_signals:
            ticker = s["ticker"]
            src    = s["entry_source"]
            name   = watchlist.get(ticker, {}).get("name", ticker)
            if src == "TARGET_30MIN":
                since = unheld_record[ticker].get("above_target_since", "?")
                print(f"[timer_agent]   → {name}({ticker}) [{src}] 안착 시각: {since}")
            else:
                print(f"[timer_agent]   → {name}({ticker}) [{src}]")
        names = [watchlist.get(s["ticker"], {}).get("name", s["ticker"]) for s in entry_signals]
        print(f"[timer_agent] 진입 신호 코인: {', '.join(names)}")
    else:
        print("[timer_agent] 진입 신호 없음")

    return entry_signals
