# run_daily.py
# 하이브리드 터틀 자동매매 — 1일 1회 배치 실행기 (Upbit 버전)
#
# 역할:
#   일봉 기반 지표 갱신과 Google Sheets 기록처럼
#   하루에 한 번만 돌아가면 충분한 작업들을 모아 실행한다.
#   crontab 등의 스케줄러가 KST 09:40 에 1회만 이 스크립트를 호출한다고 가정한다.
#
# 실행 순서:
#   1. Upbit 로그인
#   2. record_portfolio_snapshot()
#        ↳ Upbit 포트폴리오 요약 + 원장 기반 누적 실현손익 계산 →
#          '포트폴리오 추이' 시트에 하루 1회 스냅샷 기록
#   3. pnl_chart.run_pnl_chart()
#        ↳ '포트폴리오 추이' 시트의 실현손익 열을 읽어
#          '손익차트' 시트 업데이트 + 차트(최초 1회) 임베드
#          실현손익이 0 이어도 날짜별 점이 찍힘
#
# 주의:
#   - 이 스크립트는 신규 진입/피라미딩/손절 주문을 발생시키지 않는다
#     (실시간 의사결정은 run_all.py 담당).
#   - target_manager.run_update() 는 run_all.py 가 10분마다 호출하므로
#     일봉 기반 지표도 다음 10분 사이클에 자연스럽게 갱신된다.
#     여기서 중복 호출하지 않는다.
#
# 실행 방법:
#   python run_daily.py

import io
import logging
import logging.handlers
import os
import sys
from datetime import datetime

import pytz
from dotenv import load_dotenv

import indicator_calc
import pnl_chart
import trade_ledger
import upbit_client
from config import get_watchlist
from telegram_alert import SendMessage

# 프로젝트 폴더의 .env 를 명시적으로 로드 — crontab 의 cwd 가 달라도 안전
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

KST = pytz.timezone("Asia/Seoul")

# ─────────────────────────────────────
# 로그 파일 설정 (run_all.py 와 분리)
# ─────────────────────────────────────
_LOG_FILE    = "run_daily.log"
_LOG_MAX_MB  = 5
_LOG_BACKUPS = 3


class _TeeLogger(io.TextIOBase):
    """print() 출력을 콘솔과 로그 파일 두 곳에 동시에 기록하는 중간 다리."""

    def __init__(self, handler: logging.handlers.RotatingFileHandler, original):
        self._handler  = handler
        self._original = original
        self._record = logging.LogRecord(
            "run_daily", logging.INFO, "", 0, "", (), None
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
    """로그 파일 자동 순환(Rotating) 을 설정한다."""
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
    now = datetime.now(KST)
    print(f"\n[run_daily] ▶ {label}  ({now.strftime('%H:%M:%S')})")
    return now


def _step_done(start: datetime, label: str):
    elapsed = (datetime.now(KST) - start).total_seconds()
    print(f"[run_daily]   완료: {label} — {elapsed:.1f}초 소요")


def main():
    """일 1회 배치 실행 메인 함수."""
    _setup_log()

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 55)
    print(f"  하이브리드 터틀 일 1회 배치 (Upbit) — {now_str}")
    print(f"  로그 파일: {_LOG_FILE} (최대 {_LOG_MAX_MB}MB × {_LOG_BACKUPS + 1}개)")
    print("=" * 55)

    # ─────────────────────────────────────
    # STEP 1: Upbit 로그인
    # ─────────────────────────────────────
    t = _step_start("STEP 1: Upbit 로그인")
    if not upbit_client.login():
        msg = "⚠️ [run_daily] 로그인 실패 → 일 배치 중단"
        print(msg)
        SendMessage(msg)
        sys.exit(1)
    print(f"[run_daily] 로그인 성공 (실계좌 모드) ✅")
    _step_done(t, "STEP 1: Upbit 로그인")

    # ─────────────────────────────────────
    # STEP 2: ATR 캐시 갱신
    #   감시 코인 전체의 일봉 지표(ATR·5MA·20MA·10일 신저가)를 계산해
    #   atr_cache.json 에 저장한다.
    #   run_all.py 는 이 캐시를 읽기만 하므로 하루 종일 일봉 API 호출이 없어진다.
    # ─────────────────────────────────────
    t = _step_start("STEP 2: ATR 캐시 갱신")
    try:
        indicator_calc.refresh_atr_cache(list(get_watchlist().keys()))
    except Exception as e:
        msg = f"⚠️ [run_daily] ATR 캐시 갱신 오류(계속 진행): {e}"
        print(msg)
        SendMessage(msg)
    _step_done(t, "STEP 2: ATR 캐시 갱신")

    # ─────────────────────────────────────
    # STEP 3: 포트폴리오 추이 스냅샷 (일 1회)
    #   - 업비트 잔고/평가금액 조회
    #   - 체결 원장에서 누적 실현손익(realized_pnl) 계산 후 주입
    #   - 같은 날 이미 기록돼 있으면 자동 스킵
    # ─────────────────────────────────────
    t = _step_start("STEP 3: 포트폴리오 추이 기록")
    try:
        summary = upbit_client.get_portfolio_summary()
        if summary:
            realized_pnl = trade_ledger.calc_realized_pnl_total()

            try:
                initial_capital = int(os.getenv("UPBIT_INITIAL_CAPITAL", "0") or 0)
            except ValueError:
                initial_capital = 0

            trade_ledger.record_portfolio_snapshot(
                total_value     = summary.get("total_capital",   0),
                coin_value      = summary.get("coin_value",      0),
                cash            = summary.get("cash",            0),
                purchase_amount = summary.get("purchase_amount", 0),
                unrealized_pnl  = summary.get("unrealized_pnl",  0),
                realized_pnl    = realized_pnl,
                holdings_count  = summary.get("holdings_count",  0),
                holdings_names  = summary.get("holdings_names", ""),
                initial_capital = initial_capital,
            )
        else:
            print("[run_daily] 포트폴리오 요약 조회 실패 → 스냅샷 기록 스킵")
    except Exception as e:
        msg = f"⚠️ [run_daily] 포트폴리오 추이 기록 오류(계속 진행): {e}"
        print(msg)
        SendMessage(msg)
    _step_done(t, "STEP 3: 포트폴리오 추이 기록")

    # ─────────────────────────────────────
    # STEP 4: 실현 손익 차트 갱신
    #   '포트폴리오 추이' 시트의 실현손익 열을 소스로 사용
    # ─────────────────────────────────────
    t = _step_start("STEP 4: 실현 손익 차트")
    try:
        pnl_chart.run_pnl_chart()
    except Exception as e:
        msg = f"⚠️ [run_daily] 실현 손익 차트 갱신 오류(계속 진행): {e}"
        print(msg)
        SendMessage(msg)
    _step_done(t, "STEP 4: 실현 손익 차트")

    end_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 55}")
    print(f"  일 1회 배치 실행 완료 — {end_str}")
    print(f"{'=' * 55}")


if __name__ == "__main__":
    main()
