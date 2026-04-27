# timer_agent.py
# 진입 신호 통합 모듈
#
# 역할:
#   터틀 S1(20일 신고가) / S2(55일 신고가) 돌파 신호가
#   30분 이상 연속 유지된 코인을 찾아 진입 신호 목록으로 반환한다.
#
#   매수 조건 (AND):
#     ① 터틀 신호 True  — target_manager 가 s1/s2 신호 감지
#     ② 30분 이상 유지  — target_manager 가 기록한 _since 시각 기준
#
# 반환 형식:
#   [{"ticker": "KRW-BTC", "entry_source": "TURTLE_S1"}, ...]
#   entry_source: "TURTLE_S1" / "TURTLE_S2"
#
# 같은 코인이 S1·S2 동시 해당하면 S2 우선.
#
# 사용법:
#   import timer_agent
#   entry_signals = timer_agent.run_timer_check()

from datetime import datetime, timedelta

import pytz

from config import get_watchlist
from target_manager import load_unheld_record

KST = pytz.timezone("Asia/Seoul")

# 30분 가드: 신호 발생 후 이 시간 이상 유지돼야 진입 신호 발생
GUARD_MINUTES = 30


# ─────────────────────────────────────────
# 터틀 신호 30분 가드 확인
# ─────────────────────────────────────────

def check_turtle_30min_passed(ticker: str, since_str: str) -> bool:
    """터틀 신호가 since_str 시각부터 30분 이상 유지됐는지 확인한다."""
    if not since_str:
        return False

    try:
        since    = KST.localize(datetime.strptime(since_str, "%Y-%m-%d %H:%M:%S"))
        now_kst  = datetime.now(KST)
        elapsed  = now_kst - since

        if elapsed >= timedelta(minutes=GUARD_MINUTES):
            elapsed_min = int(elapsed.total_seconds() / 60)
            name = get_watchlist().get(ticker, {}).get("name", ticker)
            print(f"[timer_agent] {name}({ticker}) ✅ 30분 가드 통과! "
                  f"({elapsed_min}분 유지)")
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
    """진입 신호 코인 목록을 반환한다.

    조건: 터틀 S1 또는 S2 신호가 True 이고, 해당 신호가 30분 이상 유지된 코인.
    S1·S2 동시 해당 시 S2 우선.

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

    # 우선순위: TURTLE_S2(0) > TURTLE_S1(1)
    signal_priority: dict = {}

    for ticker, data in unheld_record.items():
        if ticker not in watchlist:
            print(f"[timer_agent] {ticker} 감시 코인 외 → 스킵")
            continue

        current_priority = signal_priority.get(ticker, (99, None))[0]

        # S1 신호 + 30분 가드 확인
        if data.get("turtle_s1_signal", False):
            if check_turtle_30min_passed(ticker, data.get("turtle_s1_since")):
                if 1 < current_priority:
                    name = watchlist.get(ticker, {}).get("name", ticker)
                    print(f"[timer_agent] {name}({ticker}) ✅ 터틀 S1 진입 신호 (20일 신고가 + 30분)")
                    signal_priority[ticker] = (1, "TURTLE_S1")
                    current_priority = 1

        # S2 신호 + 30분 가드 확인 (최우선)
        if data.get("turtle_s2_signal", False):
            if check_turtle_30min_passed(ticker, data.get("turtle_s2_since")):
                if 0 < current_priority:
                    name = watchlist.get(ticker, {}).get("name", ticker)
                    print(f"[timer_agent] {name}({ticker}) ✅ 터틀 S2 진입 신호 (55일 신고가 + 30분)")
                    signal_priority[ticker] = (0, "TURTLE_S2")

    entry_signals = [
        {"ticker": ticker, "entry_source": src}
        for ticker, (_, src) in signal_priority.items()
        if src is not None
    ]

    # 정렬: S2 → S1
    entry_signals.sort(key=lambda s: 0 if s["entry_source"] == "TURTLE_S2" else 1)

    if entry_signals:
        for s in entry_signals:
            ticker = s["ticker"]
            src    = s["entry_source"]
            name   = watchlist.get(ticker, {}).get("name", ticker)
            print(f"[timer_agent]   → {name}({ticker}) [{src}]")
        names = [watchlist.get(s["ticker"], {}).get("name", s["ticker"]) for s in entry_signals]
        print(f"[timer_agent] 진입 신호 코인: {', '.join(names)}")
    else:
        print("[timer_agent] 진입 신호 없음")

    return entry_signals
