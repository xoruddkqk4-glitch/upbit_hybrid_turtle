# trade_ledger.py
# 체결 원장 기록 모듈 (단일 진입점)
#
# 모든 매매 체결 내역은 반드시 이 모듈의 append_trade() 를 통해 기록한다.
# 로컬 JSON 파일(trade_ledger.json) 과 Google Sheets 에 동시 저장한다.
#
# 사용법:
#   from trade_ledger import append_trade
#   append_trade({
#       "side": "BUY", "ticker": "KRW-BTC", "coin_name": "비트코인",
#       "volume": 0.001, "unit_price": 95000000, "order_no": "...",
#       "order_type": "MARKET", "source": "ENTRY_30MIN",
#   })

import json
import os
import uuid
from datetime import datetime

import pytz
from dotenv import load_dotenv

from telegram_alert import SendMessage

_DIR        = os.path.dirname(os.path.abspath(__file__))
LEDGER_FILE = os.path.join(_DIR, "trade_ledger.json")

# 프로젝트 폴더의 .env 를 명시적으로 로드 — crontab 의 cwd 가 달라도 안전
load_dotenv(os.path.join(_DIR, ".env"))


def _resolve_service_account_path() -> str:
    """GOOGLE_SERVICE_ACCOUNT_JSON 환경변수를 읽어 절대경로로 반환.

    값이 상대경로면 이 파일(`trade_ledger.py`) 이 있는 디렉토리를 기준으로 결합.
    """
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json") or "service_account.json"
    return raw if os.path.isabs(raw) else os.path.join(_DIR, raw)

# 한국 표준시 (KST, UTC+9) — 업비트도 기본 한국 시간 기준으로 운영
KST = pytz.timezone("Asia/Seoul")

# source 허용 값 목록
# 매수: ENTRY_30MIN(목표가30분) / ENTRY_S1(20일신고가) / ENTRY_S2(55일신고가) / PYRAMID(피라미딩)
# 매도: EXIT_STOP(2N하드손절) / EXIT_10LOW(10일신저가익절) / EXIT_5MA(5MA익절)
VALID_SOURCES = {
    "ENTRY_30MIN", "ENTRY_S1", "ENTRY_S2",
    "PYRAMID",
    "EXIT_STOP", "EXIT_10LOW", "EXIT_5MA",
    "MANUAL_SYNC",
}

# 포트폴리오 추이 시트 이름 (모드별 분리)
#   실투자 : "포트폴리오 추이"
#   모의투자: "포트폴리오 추이 (모의)"
PORTFOLIO_SHEET_NAME_REAL = "포트폴리오 추이"

# 하루 1회 스냅샷 기록 여부 저장 (로컬, 커밋 제외 대상)
DAILY_SNAPSHOT_FILE = os.path.join(_DIR, "daily_snapshot.json")

# 체결 원장 열제목 (한글)
SHEET_HEADERS = [
    "기록ID",           # record_id
    "기록시각(KST)",    # ts_kst
    "계좌",             # account_id
    "매수/매도",        # side
    "티커",             # ticker (예: KRW-BTC)
    "코인명",           # coin_name (예: 비트코인)
    "주문번호",         # order_no (Upbit uuid)
    "체결번호",         # exec_no
    "수량(코인)",       # volume
    "단가(원)",         # unit_price
    "거래금액(원)",     # gross_amount (volume × unit_price)
    "수수료(원)",       # fee
    "실수령금액(원)",   # net_amount
    "주문유형",         # order_type (MARKET / LIMIT)
    "매매구분",         # source (ENTRY_30MIN / EXIT_STOP 등)
    "수익률(%)",        # profit_rate — SELL 일 때만 기록
    "수익금(원)",       # profit_amount — SELL 일 때만 기록
    "비고",             # note
]

# 포트폴리오 추이 시트 열제목
PORTFOLIO_HEADERS = [
    "기록시각(KST)",
    "총평가금액(원)",
    "코인평가액(원)",
    "예수금(원)",
    "매입금액(원)",
    "평가손익(원)",
    "실현손익(원)",   # 당일 실현손익
    "보유종목수",
    "보유종목목록",
    "누적수익금(원)", # 누적 실현손익
]


# ─────────────────────────────────────────
# 내부 함수
# ─────────────────────────────────────────

def _generate_record_id(ticker: str) -> str:
    """체결 원장 고유 ID 를 생성한다.

    형식: YYYYMMDD_HHMMSS_티커_랜덤4자리
    예시: 20260413_103045_KRW-BTC_a3f2
    """
    now_str     = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    random_part = uuid.uuid4().hex[:4]
    # 티커 안의 '-' 은 파일명 호환을 위해 그대로 둔다 (Sheet 저장 시에도 문제 없음)
    return f"{now_str}_{ticker}_{random_part}"


def _save_to_json(record: dict):
    """체결 내역을 로컬 JSON 파일(trade_ledger.json) 에 저장한다."""
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = []
        except (json.JSONDecodeError, IOError):
            print(f"[원장] {LEDGER_FILE} 읽기 오류 → 새 파일로 시작")
            data = []
    else:
        data = []

    data.append(record)

    with open(LEDGER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _save_to_sheets(record: dict) -> bool:
    """체결 내역을 Google Sheets 에 저장한다.

    서비스 계정 JSON 파일이 없거나 오류 발생 시 경고만 남기고 계속 진행한다.
    """
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials

        json_path   = _resolve_service_account_path()
        sheet_title = os.getenv("GOOGLE_SPREADSHEET_TITLE", "Upbit Hybrid Turtle Ledger")
        folder_id   = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")

        if not os.path.exists(json_path):
            print(f"[원장] Google 서비스 계정 파일 없음 (resolve: {json_path}) → Sheets 저장 스킵")
            return False

        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds  = ServiceAccountCredentials.from_json_keyfile_name(json_path, scope)
        client = gspread.authorize(creds)

        try:
            spreadsheet = client.open(sheet_title)
            sheet = spreadsheet.sheet1
        except gspread.SpreadsheetNotFound:
            spreadsheet = client.create(sheet_title)
            if folder_id:
                spreadsheet.share(None, perm_type="anyone", role="reader")
            sheet = spreadsheet.sheet1

        # 첫 행이 열제목이 아니거나 컬럼 수가 달라졌으면 업데이트
        first_row = sheet.row_values(1)
        if not first_row or first_row[0] != "기록ID":
            sheet.insert_row(SHEET_HEADERS, 1)
            print("[원장] Google Sheets 열제목 추가 완료")
        elif len(first_row) < len(SHEET_HEADERS) or first_row[len(SHEET_HEADERS) - 1] != SHEET_HEADERS[-1]:
            sheet.update("A1", [SHEET_HEADERS])
            print("[원장] Google Sheets 열제목 업데이트 완료 (수익금 컬럼 추가)")

        # 수익률: SELL 이고 profit_rate 필드가 있을 때만 표시
        profit_rate_val = record.get("profit_rate", "")
        if profit_rate_val != "" and isinstance(profit_rate_val, (int, float)):
            profit_rate_str = f"{profit_rate_val:+.2f}"
        else:
            profit_rate_str = ""

        # 수익금: SELL 이고 profit_amount 필드가 있을 때만 표시
        profit_amount_val = record.get("profit_amount", "")
        if profit_amount_val != "" and isinstance(profit_amount_val, (int, float)):
            profit_amount_str = f"{int(profit_amount_val):+,}"
        else:
            profit_amount_str = ""

        # 실수령금액: SELL 이고 net_amount 미지정이면 gross_amount 로 자동 채우기
        net_amount_val = record.get("net_amount", "")
        if (net_amount_val == "" or net_amount_val == 0) and record.get("side") == "SELL":
            net_amount_val = record.get("gross_amount", 0)

        row = [
            record.get("record_id",    ""),
            record.get("ts_kst",       ""),
            record.get("account_id",   ""),
            record.get("side",         ""),
            record.get("ticker",       ""),
            record.get("coin_name",    ""),
            record.get("order_no",     ""),
            record.get("exec_no",      ""),
            record.get("volume",       0),
            record.get("unit_price",   0),
            record.get("gross_amount", 0),
            record.get("fee",          0),
            net_amount_val,
            record.get("order_type",   ""),
            record.get("source",       ""),
            profit_rate_str,
            profit_amount_str,
            record.get("note",         ""),
        ]
        sheet.append_row(row)
        print("[원장] Google Sheets 저장 완료")
        return True

    except ImportError:
        print("[원장] gspread 미설치 → Sheets 저장 스킵 (pip install gspread oauth2client)")
    except Exception as e:
        print(f"[원장] Google Sheets 저장 오류 (무시하고 계속): {e}")

    return False


# ─────────────────────────────────────────
# 공개 함수 (단일 진입점)
# ─────────────────────────────────────────

def append_trade(record: dict):
    """체결 원장에 새 거래를 기록한다 (단일 진입점).

    로컬 JSON 파일과 Google Sheets 에 동시 저장한다.

    필수 필드:
        side (str):       "BUY" 또는 "SELL"
        ticker (str):     업비트 티커 (예: "KRW-BTC")
        volume (float):   체결 수량 (코인)
        unit_price (int|float): 체결 단가 (원)
        order_type (str): "MARKET" 또는 "LIMIT"
        source (str):     "ENTRY_30MIN" / "ENTRY_S1" / "ENTRY_S2" /
                          "PYRAMID" / "EXIT_STOP" / "EXIT_10LOW" / "EXIT_5MA"

    자동으로 채워지는 필드:
        record_id:    중복 방지 고유 ID
        ts_kst:       KST 기준 기록 시각
        gross_amount: volume × unit_price
        account_id:   .env 의 UPBIT_ACCOUNT_LABEL
    """
    source = record.get("source", "")
    if source not in VALID_SOURCES:
        print(f"[원장] 경고: source 값이 올바르지 않음 → '{source}'. 허용: {VALID_SOURCES}")

    ticker = record.get("ticker", "UNKNOWN")
    record.setdefault("record_id",    _generate_record_id(ticker))
    record.setdefault("ts_kst",       datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"))
    record.setdefault("gross_amount", record.get("volume", 0) * record.get("unit_price", 0))
    record.setdefault("account_id",   os.getenv("UPBIT_ACCOUNT_LABEL", "upbit_main"))

    _save_to_json(record)
    sheets_ok = _save_to_sheets(record)

    # 콘솔 확인 로그
    side_kor = "매수" if record.get("side") == "BUY" else "매도"
    name     = record.get("coin_name", ticker)
    vol      = record.get("volume", 0)
    price    = record.get("unit_price", 0)
    src      = record.get("source", "")
    gross    = record.get("gross_amount", vol * price)
    print(f"[원장] 기록 완료 | {side_kor} {name}({ticker}) {vol:.8f}개 "
          f"@{price:,.0f}원 = {gross:,.0f}원 [{src}]")

    if sheets_ok:
        # 수익률이 음수이면 손절, 그 외(양수·0·미기록)이면 익절로 표시
        _pr_raw = record.get("profit_rate", "")
        _exit_pfx = "손절" if isinstance(_pr_raw, (int, float)) and _pr_raw < 0 else "익절"
        source_kor = {
            "ENTRY_30MIN": "진입(목표가30분)",
            "ENTRY_S1":    "진입(20일신고가)",
            "ENTRY_S2":    "진입(55일신고가)",
            "PYRAMID":     "피라미딩",
            "EXIT_STOP":   "손절(2N하드)",
            "EXIT_10LOW":  f"{_exit_pfx}(10일신저가)",
            "EXIT_5MA":    f"{_exit_pfx}(5MA)",
            "MANUAL_SYNC": "수동 동기화",
        }.get(src, src)

        msg_lines = [
            f"📋 [체결 기록] Google Sheets 저장 완료",
            f"코인: {name}({ticker})",
            f"구분: {side_kor} / {source_kor}",
            f"수량: {vol:.8f}개 @{price:,.0f}원",
            f"거래금액: {gross:,.0f}원",
        ]
        if record.get("side") == "SELL":
            net_val = record.get("net_amount", gross)
            if isinstance(net_val, (int, float)) and net_val > 0:
                msg_lines.append(f"실수령금액: {int(net_val):,}원")
            pr = record.get("profit_rate", "")
            pa = record.get("profit_amount", "")
            if pr != "" and isinstance(pr, (int, float)) and pa != "" and isinstance(pa, (int, float)):
                sign = "+" if pr >= 0 else ""
                msg_lines.append(f"수익률: {sign}{pr:.2f}% / 수익금: {int(pa):+,}원")
        msg_lines.append(f"기록시각: {record.get('ts_kst', '')}")
        SendMessage("\n".join(msg_lines))

    # 매도 체결 시 포트폴리오 추이·손익차트 즉시 갱신
    if record.get("side") == "SELL":
        refresh_sheets_after_sell()


def calc_realized_pnl_total() -> int:
    """체결 원장 전체를 훑어 누적 실현손익(원) 을 정수로 반환한다.

    가중평균 매입단가 방식:
        BUY  : 보유수량·가중평균 매입단가 갱신 (수수료가 있으면 당일 손익 차감)
        SELL : (매도가 − 현재 가중평균 매입단가) × 매도수량 − fee 가 실현손익
    Upbit API 는 realized_pnl 을 제공하지 않으므로 원장 기반으로 자체 계산한다.
    원장이 비어있거나 SELL 이 없으면 0 을 반환한다.
    """
    if not os.path.exists(LEDGER_FILE):
        return 0
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            return 0
    except (json.JSONDecodeError, IOError):
        return 0

    def _ts_key(row):
        ts = row.get("ts_kst", "")
        try:
            return (0, datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return (1, ts)

    rows = sorted([r for r in rows if isinstance(r, dict)], key=_ts_key)

    position = {}  # ticker -> {"qty", "avg_cost"}
    total    = 0.0

    for row in rows:
        side   = str(row.get("side", "")).upper()
        ticker = row.get("ticker", "")
        if not ticker:
            continue
        try:
            volume     = float(row.get("volume",     0) or 0)
            unit_price = float(row.get("unit_price", 0) or 0)
            fee        = float(row.get("fee",        0) or 0)
        except (TypeError, ValueError):
            continue
        if volume <= 0 or unit_price <= 0:
            continue

        pos = position.setdefault(ticker, {"qty": 0.0, "avg_cost": 0.0})

        if side == "BUY":
            old_qty  = pos["qty"]
            old_cost = pos["avg_cost"]
            new_qty  = old_qty + volume
            if new_qty > 0:
                pos["avg_cost"] = (old_qty * old_cost + volume * unit_price) / new_qty
            pos["qty"] = new_qty
            if fee > 0:
                total -= fee
        elif side == "SELL":
            avg_cost = pos["avg_cost"]
            sell_qty = min(volume, pos["qty"]) if pos["qty"] > 0 else volume
            if avg_cost > 0 and sell_qty > 0:
                total += (unit_price - avg_cost) * sell_qty - fee
            else:
                total -= fee
            pos["qty"] = max(0.0, pos["qty"] - volume)
            if pos["qty"] == 0.0:
                pos["avg_cost"] = 0.0

    return int(round(total))


def calc_realized_pnl_today() -> int:
    """체결 원장 기준 당일 실현손익(원) 을 계산해 반환한다."""
    if not os.path.exists(LEDGER_FILE):
        return 0
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            return 0
    except (json.JSONDecodeError, IOError):
        return 0

    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = str(row.get("ts_kst", ""))
        if not ts.startswith(today_str):
            continue
        side = str(row.get("side", "")).upper()
        if side != "SELL":
            continue
        try:
            pa = row.get("profit_amount", "")
            if isinstance(pa, (int, float)):
                total += float(pa)
        except Exception:
            continue
    return int(round(total))


def _load_daily_snapshot() -> dict:
    """daily_snapshot.json 을 읽어 반환한다."""
    if os.path.exists(DAILY_SNAPSHOT_FILE):
        try:
            with open(DAILY_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_daily_snapshot(data: dict):
    """daily_snapshot.json 에 오늘 날짜를 기록한다."""
    try:
        with open(DAILY_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"[원장] daily_snapshot.json 저장 오류: {e}")


def _upsert_portfolio_row(ws, today_str: str, row_values: list):
    """'포트폴리오 추이' 시트에서 오늘 날짜 행을 찾아 덮어쓴다.

    오늘 날짜로 시작하는 행이 이미 있으면 해당 행을 최신 값으로 업데이트하고,
    없으면 새 줄을 추가한다. 같은 날 여러 번 호출해도 항상 1줄만 유지된다.
    """
    # 시트 전체 값을 읽어 오늘 날짜 행 위치를 찾는다 (헤더 행 제외)
    all_values = ws.get_all_values()
    today_row_idx = None
    for i, row in enumerate(all_values[1:], start=2):   # 시트는 1-based, 헤더는 1행
        if row and str(row[0]).startswith(today_str):
            today_row_idx = i   # 오늘 날짜 행 발견 (같은 날 여러 행이면 마지막 것 사용)
    if today_row_idx:
        # 오늘 행이 있으면 해당 위치를 덮어씀
        ws.update(f"A{today_row_idx}", [row_values])
        print(f"[원장] 포트폴리오 추이 {today_str} 행 업데이트 (row {today_row_idx})")
    else:
        # 오늘 행이 없으면 새 줄 추가
        ws.append_row(row_values)
        print(f"[원장] 포트폴리오 추이 {today_str} 행 신규 추가")



def record_portfolio_snapshot(
    total_value: int,
    coin_value: int = 0,
    cash: int = 0,
    purchase_amount: int = 0,
    unrealized_pnl: int = 0,
    realized_pnl_daily: int = 0,
    cumulative_profit: int = 0,
    holdings_count: int = 0,
    holdings_names: str = "",
    initial_capital: int = 0,
):
    """포트폴리오 추이를 '포트폴리오 추이' 시트에 기록한다 (하루 1회).

    같은 날 두 번 이상 호출해도 첫 번째만 기록되고 이후는 건너뛴다.
    (daily_snapshot.json 으로 중복 방지)
    """
    # ① 오늘 이미 기록했는지 확인 (하루 1회 가드)
    today_str     = datetime.now(KST).strftime("%Y-%m-%d")
    snapshot_data = _load_daily_snapshot()
    if snapshot_data.get("last_recorded_date") == today_str:
        print(f"[원장] 오늘({today_str}) 포트폴리오 추이 이미 기록됨 → 스킵")
        return

    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials

        json_path   = _resolve_service_account_path()
        sheet_title = os.getenv("GOOGLE_SPREADSHEET_TITLE", "Upbit Hybrid Turtle Ledger")

        if not os.path.exists(json_path):
            print(f"[원장] Google 서비스 계정 파일 없음 (resolve: {json_path}) → 포트폴리오 추이 저장 스킵")
            return

        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds  = ServiceAccountCredentials.from_json_keyfile_name(json_path, scope)
        client = gspread.authorize(creds)

        try:
            spreadsheet = client.open(sheet_title)
        except gspread.SpreadsheetNotFound:
            print(f"[원장] 스프레드시트 '{sheet_title}' 없음 → 체결 원장을 먼저 기록하세요")
            return

        try:
            ws = spreadsheet.worksheet(PORTFOLIO_SHEET_NAME_REAL)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=PORTFOLIO_SHEET_NAME_REAL, rows=1000, cols=9)
            print(f"[원장] '{PORTFOLIO_SHEET_NAME_REAL}' 시트 새로 생성")

        # 첫 행이 비어 있거나 헤더가 달라졌으면 업데이트 (컬럼 증감 모두 감지)
        first_row = ws.row_values(1)
        if not first_row or first_row[0] != "기록시각(KST)":
            ws.insert_row(PORTFOLIO_HEADERS, 1)
            print("[원장] 포트폴리오 추이 열제목 추가 완료")
        elif first_row != PORTFOLIO_HEADERS:
            ws.update("A1", [PORTFOLIO_HEADERS])
            print("[원장] 포트폴리오 추이 열제목 업데이트 완료")

        # ③ Google Sheets 에 오늘 자산 현황 기록 (같은 날 중복 방지 upsert)
        ts_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        _upsert_portfolio_row(ws, today_str, [
            ts_kst,
            total_value,
            coin_value,
            cash,
            purchase_amount,
            unrealized_pnl,
            realized_pnl_daily,
            holdings_count,
            holdings_names,
            cumulative_profit,
        ])

        # ④ 기록 성공 → daily_snapshot.json 에 오늘 날짜 저장 (중복 방지)
        _save_daily_snapshot({"last_recorded_date": today_str})

        print(f"[원장] 포트폴리오 추이 기록 완료 "
              f"— 총평가금액: {total_value:,}원, 보유코인: {holdings_count}개, "
              f"당일 실현손익: {realized_pnl_daily:+,}원, 누적수익금: {cumulative_profit:+,}원")

        # 텔레그램 알림 발송
        tg_names = f"\n보유코인: {holdings_names}" if holdings_names else "\n보유코인: 없음"
        msg = (
            f"📊 [포트폴리오 추이] Google Sheets 저장 완료\n"
            f"총평가금액: {total_value:,}원\n"
            f"코인평가액: {coin_value:,}원\n"
            f"예수금: {cash:,}원\n"
            f"평가손익: {unrealized_pnl:+,}원\n"
            f"당일 실현손익: {realized_pnl_daily:+,}원\n"
            f"누적수익금: {cumulative_profit:+,}원\n"
            f"보유코인수: {holdings_count}개"
            f"{tg_names}\n"
            f"기록시각: {ts_kst}"
        )
        SendMessage(msg)

    except ImportError:
        print("[원장] gspread 미설치 → 포트폴리오 추이 저장 스킵")
    except Exception as e:
        print(f"[원장] 포트폴리오 추이 저장 오류 (무시하고 계속): {e}")


def refresh_sheets_after_sell():
    """매도 체결 직후 '포트폴리오 추이'·'손익차트' 시트를 즉시 갱신한다.

    append_trade() 에서 SELL 기록 완료 후 자동 호출된다.
    - daily_snapshot.json 은 건드리지 않으므로 run_daily.py 의 하루 1회 흐름이 유지된다.
    - 오류가 발생해도 try/except 로 감싸 매매 흐름을 차단하지 않는다.
    - 텔레그램 알림은 발송하지 않는다 (append_trade 에서 이미 발송됨).
    """
    try:
        # 로컬 임포트 — 모듈 최상단에서 임포트하면 순환참조 위험이 있어 여기서만 불러온다
        import upbit_client  # noqa: PLC0415
        import pnl_chart    # noqa: PLC0415

        # ① Upbit 에서 현재 포트폴리오 요약 조회
        summary = upbit_client.get_portfolio_summary()
        if not summary:
            print("[원장] 포트폴리오 조회 실패 → 즉시 갱신 스킵")
            return

        # ② 원장에서 실현손익 재계산 (당일/누적)
        realized_pnl_daily = calc_realized_pnl_today()
        cumulative_profit = calc_realized_pnl_total()

        try:
            initial_capital = int(os.getenv("UPBIT_INITIAL_CAPITAL", "0") or 0)
        except ValueError:
            initial_capital = 0

        # ③ '포트폴리오 추이' 시트 — 실현손익·누적수익금 갱신
        #    run_daily.py 행은 잠금, 이후 매도는 별도 행에 기록
        _upsert_portfolio_direct(
            total_value     = summary.get("total_capital",   0),
            coin_value      = summary.get("coin_value",      0),
            cash            = summary.get("cash",            0),
            purchase_amount = summary.get("purchase_amount", 0),
            unrealized_pnl  = summary.get("unrealized_pnl",  0),
            realized_pnl_daily = realized_pnl_daily,
            cumulative_profit = cumulative_profit,
            holdings_count  = summary.get("holdings_count",  0),
            holdings_names  = summary.get("holdings_names", ""),
            initial_capital = initial_capital,
            intraday_minimal = True,
        )

        # ④ '손익차트' 시트 갱신
        pnl_chart.run_pnl_chart()
        print("[원장] 매도 후 포트폴리오 추이·손익차트 즉시 갱신 완료")

    except Exception as e:
        print(f"[원장] 매도 후 즉시 갱신 오류 (무시하고 계속): {e}")


def _upsert_portfolio_direct(
    total_value: int,
    coin_value: int = 0,
    cash: int = 0,
    purchase_amount: int = 0,
    unrealized_pnl: int = 0,
    realized_pnl_daily: int = 0,
    cumulative_profit: int = 0,
    holdings_count: int = 0,
    holdings_names: str = "",
    initial_capital: int = 0,
    intraday_minimal: bool = False,
):
    """'포트폴리오 추이' 시트에 upsert 만 수행한다.

    record_portfolio_snapshot() 과 달리 daily_snapshot.json 을 확인·갱신하지 않는다.
    매도 즉시 갱신 경로(refresh_sheets_after_sell)에서만 사용한다.
    """
    today_str = datetime.now(KST).strftime("%Y-%m-%d")

    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials

        json_path   = _resolve_service_account_path()
        sheet_title = os.getenv("GOOGLE_SPREADSHEET_TITLE", "Upbit Hybrid Turtle Ledger")

        if not os.path.exists(json_path):
            print(f"[원장] Google 서비스 계정 파일 없음 → 즉시 갱신 스킵")
            return

        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds  = ServiceAccountCredentials.from_json_keyfile_name(json_path, scope)
        client = gspread.authorize(creds)

        try:
            spreadsheet = client.open(sheet_title)
        except gspread.SpreadsheetNotFound:
            print(f"[원장] 스프레드시트 '{sheet_title}' 없음 → 즉시 갱신 스킵")
            return

        try:
            ws = spreadsheet.worksheet(PORTFOLIO_SHEET_NAME_REAL)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=PORTFOLIO_SHEET_NAME_REAL, rows=1000, cols=9)
            print(f"[원장] '{PORTFOLIO_SHEET_NAME_REAL}' 시트 새로 생성")

        # 헤더 확인 및 추가
        first_row = ws.row_values(1)
        if not first_row or first_row[0] != "기록시각(KST)":
            ws.insert_row(PORTFOLIO_HEADERS, 1)
        elif first_row != PORTFOLIO_HEADERS:
            ws.update("A1", [PORTFOLIO_HEADERS])

        ts_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        if intraday_minimal:
            # run_daily.py 가 오늘 이미 실행됐는지 확인한다.
            # daily_snapshot.json 의 last_recorded_date 가 오늘이면 실행된 것이다.
            daily_ran_today = (_load_daily_snapshot().get("last_recorded_date") == today_str)

            # 오늘 행을 두 종류로 구분한다.
            #   D행(daily snapshot) : B열(총평가금액)에 값이 있음 — run_daily.py 가 기록한 행
            #   S행(sell 갱신행)    : B열이 비어 있음           — 매도 직후 기록한 행
            all_values = ws.get_all_values()
            daily_row_idx = None   # D행 위치
            sell_row_idx  = None   # S행 위치
            for i, row in enumerate(all_values[1:], start=2):
                if row and str(row[0]).startswith(today_str):
                    b_col = row[1] if len(row) > 1 else ""
                    if b_col:   # B열에 값 있음 → D행
                        daily_row_idx = i
                    else:       # B열이 빈칸 → S행
                        sell_row_idx = i

            if daily_ran_today:
                # run_daily.py 행(D행)은 건드리지 않는다.
                # S행이 있으면 해당 셀만 업데이트, 없으면 새 S행을 추가한다.
                if sell_row_idx:
                    ws.update(f"A{sell_row_idx}", [[ts_kst]])
                    ws.update(f"G{sell_row_idx}", [[realized_pnl_daily]])
                    ws.update(f"J{sell_row_idx}", [[cumulative_profit]])
                    print(f"[원장] 포트폴리오 추이 {today_str} 매도 갱신 행 업데이트 (row {sell_row_idx})")
                else:
                    ws.append_row([ts_kst, "", "", "", "", "", realized_pnl_daily, "", "", cumulative_profit])
                    print(f"[원장] 포트폴리오 추이 {today_str} 매도 갱신 행 신규 추가")
            else:
                # run_daily.py 가 아직 실행 전 → 오늘 기존 행(D행 또는 S행)에 A·G·J만 업데이트
                target_row_idx = daily_row_idx or sell_row_idx
                if target_row_idx:
                    ws.update(f"A{target_row_idx}", [[ts_kst]])
                    ws.update(f"G{target_row_idx}", [[realized_pnl_daily]])
                    ws.update(f"J{target_row_idx}", [[cumulative_profit]])
                    print(f"[원장] 포트폴리오 추이 {today_str} 실현손익·누적수익금 업데이트 (row {target_row_idx})")
                else:
                    ws.append_row([ts_kst, "", "", "", "", "", realized_pnl_daily, "", "", cumulative_profit])
                    print(f"[원장] 포트폴리오 추이 {today_str} 실현손익·누적수익금 신규 행 추가")
        else:
            _upsert_portfolio_row(ws, today_str, [
                ts_kst,
                total_value,
                coin_value,
                cash,
                purchase_amount,
                unrealized_pnl,
                realized_pnl_daily,
                holdings_count,
                holdings_names,
                cumulative_profit,
            ])

    except ImportError:
        print("[원장] gspread 미설치 → 즉시 갱신 스킵")
    except Exception as e:
        print(f"[원장] 즉시 갱신 오류 (무시하고 계속): {e}")
