# timer_agent.py
# 진입 신호 통합 모듈
#
# 역할:
#   터틀 S1(20일 신고가) / S2(55일 신고가) 돌파 후
#   눌림→재돌파 조건이 충족된 코인을 찾아 진입 신호 목록으로 반환한다.
#
#   매수 조건 (AND):
#     ① 터틀 신호 True       — target_manager 가 s1/s2 신호 감지
#     ② entry_ready = True  — target_manager 가 눌림→재돌파 조건 확인
#        (돌파 → 최고값 추적 → 눌림 → 최고값 재돌파 시 True)
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

from config import get_watchlist
from target_manager import load_unheld_record


# ─────────────────────────────────────────
# 눌림→재돌파 진입 조건 확인
# ─────────────────────────────────────────

def check_pullback_rebreak(ticker: str, signal_key: str, data: dict) -> bool:
    """시간 가드 및 안착 진입 조건이 충족됐는지 확인한다."""
    entry_ready    = data.get(f"turtle_{signal_key}_entry_ready", False)
    breakout_price = data.get(f"turtle_{signal_key}_breakout_price")
    signal         = data.get(f"turtle_{signal_key}_signal", False)
    breakout_at    = data.get(f"turtle_{signal_key}_breakout_at")
    name           = get_watchlist().get(ticker, {}).get("name", ticker)
    label          = signal_key.upper()

    if entry_ready and breakout_price:
        print(f"[timer_agent] {name}({ticker}) ✅ {label} 시간 가드 안착 성공! "
              f"(돌파 기준선: {breakout_price:,.0f}원)")
        return True

    # 상태 대기 메시지
    if signal and breakout_at:
        print(f"[timer_agent] {name}({ticker}) ⏳ {label} 시간 가드 대기 중 (돌파시각: {breakout_at})")

    return False


# ─────────────────────────────────────────
# 메인 실행 함수
# ─────────────────────────────────────────

def run_timer_check() -> list:
    """진입 신호 코인 목록을 반환한다.

    조건: 터틀 S1 또는 S2 신호가 True 이고, 눌림→재돌파 조건이 충족된 코인.
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

        current_priority = signal_priority.get(ticker, (99, None, None))[0]

        # S1 신호 + 시간 가드 안착 확인
        if data.get("turtle_s1_signal", False):
            if check_pullback_rebreak(ticker, "s1", data):
                if 1 < current_priority:
                    name = watchlist.get(ticker, {}).get("name", ticker)
                    print(f"[timer_agent] {name}({ticker}) ✅ 터틀 S1 진입 신호 (20일 신고가 시간 가드 통과)")
                    signal_priority[ticker] = (1, "TURTLE_S1", data.get("turtle_s1_breakout_at"))
                    current_priority = 1

        # S2 신호 + 시간 가드 안착 확인 (최우선)
        if data.get("turtle_s2_signal", False):
            if check_pullback_rebreak(ticker, "s2", data):
                if 0 < current_priority:
                    name = watchlist.get(ticker, {}).get("name", ticker)
                    print(f"[timer_agent] {name}({ticker}) ✅ 터틀 S2 진입 신호 (55일 신고가 시간 가드 통과)")
                    signal_priority[ticker] = (0, "TURTLE_S2", data.get("turtle_s2_breakout_at"))

    entry_signals = [
        {"ticker": ticker, "entry_source": src, "peak_time": peak_time}
        for ticker, (_, src, peak_time) in signal_priority.items()
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
