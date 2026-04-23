# run_all.py
# 하이브리드 터틀 자동매매 — 상시(인트라데이) 실행기 (Upbit 버전)
#
# 역할:
#   장중에 반복적으로(예: 10분마다) 실행되어야 하는 실시간 판단 로직을
#   올바른 순서로 한 번씩 실행한다.
#   실행 시각·간격은 AWS crontab/스케줄러가 결정한다.
#   이 파일에는 스케줄 정보를 담지 않는다.
#
# 실행 순서 (이 순서를 바꾸면 안 됨):
#   1.   Upbit 로그인
#   1-A. balance_sync — 실제 잔고 ↔ held_coin_record.json 동기화 (수동 매매 반영)
#   2.   risk_guardian  — 기존 포지션 손절·익절 감시 (기존 자산 보호 최우선)
#   3.   target_manager — 미보유 코인 목표가·30분 가드 타이머 갱신
#   4.   timer_agent    — 30분 가드 체크 (진입 신호 코인 목록 생성)
#   5.   turtle_order_logic — 진입·피라미딩 주문 실행
#
# 매수/매도는 체결 시점에 turtle_order_logic · risk_guardian 에서
# trade_ledger.append_trade() 로 '시트1'(체결 원장) 에 즉시 기록된다.
# '포트폴리오 추이'·'손익차트' 시트는 run_daily.py 가 하루 1회 기록한다.
#
# 실행 방법:
#   python run_all.py

import io
import logging
import logging.handlers
import sys
from datetime import datetime

import pytz

import balance_sync
import risk_guardian
import target_manager
import timer_agent
import turtle_order_logic
import upbit_client
from telegram_alert import SendMessage

KST = pytz.timezone("Asia/Seoul")

# ─────────────────────────────────────
# 로그 파일 설정
# ─────────────────────────────────────
_LOG_FILE    = "run_all.log"
_LOG_MAX_MB  = 5
_LOG_BACKUPS = 3


class _TeeLogger(io.TextIOBase):
    """print() 출력을 콘솔과 로그 파일 두 곳에 동시에 기록하는 중간 다리."""

    def __init__(self, handler: logging.handlers.RotatingFileHandler, original):
        self._handler  = handler
        self._original = original
        # 매번 LogRecord 를 새로 만들면 느리므로 템플릿을 재사용
        self._record = logging.LogRecord(
            "run_all", logging.INFO, "", 0, "", (), None
        )

    def write(self, msg: str) -> int:
        self._original.write(msg)
        self._original.flush()
        stripped = msg.rstrip("\n")
        if stripped.strip():
            self._record.msg = stripped
            self._handler.emit(self._record)
        return len(msg)

    def flush(self):
        self._original.flush()
        self._handler.flush()


def _setup_log():
    """로그 파일 자동 순환(Rotating) 을 설정한다.

    파일 크기가 5MB 를 넘으면 자동으로 새 파일로 교체하고,
    이전 파일은 .1/.2/.3 으로 최대 3개까지 보관한다.
    """
    handler = logging.handlers.RotatingFileHandler(
        _LOG_FILE,
        maxBytes=_LOG_MAX_MB * 1024 * 1024,
        backupCount=_LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    sys.stdout = _TeeLogger(handler, sys.__stdout__)
    sys.stderr = _TeeLogger(handler, sys.__stderr__)


def _step_start(label: str) -> datetime:
    """단계 시작 시각을 출력하고 반환한다."""
    now = datetime.now(KST)
    print(f"\n[run_all] ▶ {label}  ({now.strftime('%H:%M:%S')})")
    return now


def _step_done(start: datetime, label: str):
    """단계 종료 시각과 소요 시간을 출력한다."""
    elapsed = (datetime.now(KST) - start).total_seconds()
    print(f"[run_all]   완료: {label} — {elapsed:.1f}초 소요")


def main():
    """자동매매 배치 실행 메인 함수."""
    _setup_log()

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 55)
    print(f"  하이브리드 터틀 자동매매 실행 (Upbit) — {now_str}")
    print(f"  로그 파일: {_LOG_FILE} (최대 {_LOG_MAX_MB}MB × {_LOG_BACKUPS + 1}개)")
    print("=" * 55)

    # ─────────────────────────────────────
    # STEP 1: Upbit 로그인
    # ─────────────────────────────────────
    t = _step_start("STEP 1: Upbit 로그인")
    login_ok = upbit_client.login()

    if not login_ok:
        msg = "⚠️ [run_all] 로그인 실패 → 자동매매 중단"
        print(msg)
        SendMessage(msg)
        sys.exit(1)

    print(f"[run_all] 로그인 성공 (실계좌 모드) ✅")
    _step_done(t, "STEP 1: Upbit 로그인")

    # ─────────────────────────────────────
    # STEP 1-A: 잔고 동기화 (수동 매매 반영)
    # 실제 잔고와 기록이 다르면 손절·주문 계산이 틀어지므로 가장 먼저 정정한다.
    # ─────────────────────────────────────
    t = _step_start("STEP 1-A: 잔고 동기화")
    sync_ok = balance_sync.run_balance_sync()
    if not sync_ok:
        msg = "⚠️ [run_all] 잔고 조회 실패 → 자동매매 중단"
        print(msg)
        SendMessage(msg)
        sys.exit(1)
    _step_done(t, "STEP 1-A: 잔고 동기화")

    # ─────────────────────────────────────
    # STEP 2: 기존 포지션 손절·익절 감시 (최우선)
    # ─────────────────────────────────────
    t = _step_start("STEP 2: 손절·익절 감시")
    try:
        risk_guardian.run_guardian()
    except Exception as e:
        msg = f"⚠️ [run_all] 손절·익절 감시 오류: {e}"
        print(msg)
        SendMessage(msg)
        sys.exit(1)
    _step_done(t, "STEP 2: 손절·익절 감시")

    # ─────────────────────────────────────
    # STEP 3: 미보유 코인 목표가 갱신
    # ─────────────────────────────────────
    t = _step_start("STEP 3: 목표가 갱신")
    try:
        target_manager.run_update()
    except Exception as e:
        msg = f"⚠️ [run_all] 목표가 갱신 오류 (계속 진행): {e}"
        print(msg)
        SendMessage(msg)
    _step_done(t, "STEP 3: 목표가 갱신")

    # ─────────────────────────────────────
    # STEP 4: 30분 가드 체크 (진입 신호 파악)
    # ─────────────────────────────────────
    t = _step_start("STEP 4: 30분 가드 체크")
    entry_signals = []
    try:
        entry_signals = timer_agent.run_timer_check()
    except Exception as e:
        msg = f"⚠️ [run_all] 타이머 체크 오류 (계속 진행): {e}"
        print(msg)
        SendMessage(msg)
    _step_done(t, "STEP 4: 30분 가드 체크")

    # ─────────────────────────────────────
    # STEP 5: 진입·피라미딩 주문 실행
    # ─────────────────────────────────────
    t = _step_start("STEP 5: 주문 실행")
    try:
        turtle_order_logic.run_orders(entry_signals)
    except Exception as e:
        msg = f"⚠️ [run_all] 주문 실행 오류: {e}"
        print(msg)
        SendMessage(msg)
    _step_done(t, "STEP 5: 주문 실행")

    end_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 55}")
    print(f"  자동매매 배치 실행 완료 — {end_str}")
    print(f"{'=' * 55}")


if __name__ == "__main__":
    main()
