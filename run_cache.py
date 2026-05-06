# run_cache.py
# 하이브리드 터틀 자동매매 — 일봉 ATR 캐시 갱신 스크립트
#
# 역할:
#   감시 코인 전체의 일봉 기반 지표(ATR·5MA·20MA·10일 신저가·S1/S2 신고가)를
#   계산해 atr_cache.json 에 저장한다.
#   crontab 에서 KST 09:10 에 1회 호출한다.
#
#   atr_cache.json 이 미리 채워져 있으면 run_all.py 는 하루 종일
#   일봉 API 를 직접 호출하지 않고 캐시에서 지표를 읽어 쓴다.
#
# 실행 방법:
#   python run_cache.py

import io
import logging
import logging.handlers
import os
import sys
from datetime import datetime

import pytz
from dotenv import load_dotenv

import indicator_calc
import upbit_client
from config import get_watchlist
from telegram_alert import SendMessage

# 프로젝트 폴더의 .env 를 명시적으로 로드 — crontab 의 cwd 가 달라도 안전
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

KST = pytz.timezone("Asia/Seoul")

# ─────────────────────────────────────
# 로그 파일 설정
# ─────────────────────────────────────
_LOG_FILE    = "run_cache.log"
_LOG_MAX_MB  = 5
_LOG_BACKUPS = 3


class _TeeLogger(io.TextIOBase):
    """print() 출력을 콘솔과 로그 파일 두 곳에 동시에 기록하는 중간 다리."""

    def __init__(self, handler: logging.handlers.RotatingFileHandler, original):
        self._handler  = handler
        self._original = original
        self._record = logging.LogRecord(
            "run_cache", logging.INFO, "", 0, "", (), None
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


def main():
    """ATR 캐시 갱신 메인 함수."""
    _setup_log()

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 55)
    print(f"  하이브리드 터틀 ATR 캐시 갱신 (Upbit) — {now_str}")
    print(f"  로그 파일: {_LOG_FILE} (최대 {_LOG_MAX_MB}MB × {_LOG_BACKUPS + 1}개)")
    print("=" * 55)

    # ─────────────────────────────────────
    # STEP 1: Upbit 로그인
    # ─────────────────────────────────────
    t = datetime.now(KST)
    print(f"\n[run_cache] ▶ STEP 1: Upbit 로그인  ({t.strftime('%H:%M:%S')})")
    if not upbit_client.login():
        msg = "⚠️ [run_cache] 로그인 실패 → ATR 캐시 갱신 중단"
        print(msg)
        SendMessage(msg)
        sys.exit(1)
    print("[run_cache]   로그인 성공 (실계좌 모드) ✅")

    # ─────────────────────────────────────
    # STEP 2: ATR 캐시 갱신
    #   감시 코인 전체의 일봉 지표(ATR·5MA·20MA·10일 신저가·S1/S2 신고가)를 계산해
    #   atr_cache.json 에 저장한다.
    # ─────────────────────────────────────
    t = datetime.now(KST)
    print(f"\n[run_cache] ▶ STEP 2: ATR 캐시 갱신  ({t.strftime('%H:%M:%S')})")
    try:
        indicator_calc.refresh_atr_cache(list(get_watchlist().keys()))
        elapsed = (datetime.now(KST) - t).total_seconds()
        print(f"[run_cache]   완료: ATR 캐시 갱신 — {elapsed:.1f}초 소요")
    except Exception as e:
        msg = f"⚠️ [run_cache] ATR 캐시 갱신 오류: {e}"
        print(msg)
        SendMessage(msg)

    end_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 55}")
    print(f"  ATR 캐시 갱신 완료 — {end_str}")
    print(f"{'=' * 55}")


if __name__ == "__main__":
    main()
