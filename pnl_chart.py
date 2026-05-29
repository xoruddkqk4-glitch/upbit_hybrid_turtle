# pnl_chart.py
# 실현 손익 누적 차트 — Google Sheets 갱신 모듈
#
# 역할:
#   '포트폴리오 추이' 시트의 '실현손익(원)'(당일) / '누적수익금(원)'(누적) 열을 소스로 삼아
#   '손익차트' 워크시트에 [날짜 / 당일 실현손익 / 누적 실현손익]
#   세 열을 쓰고, 콤보 그래프(막대+선)를 임베드한다.
#
# 데이터 소스:
#   '포트폴리오 추이' 시트 (trade_ledger.record_portfolio_snapshot 이 하루 1회 기록)
#   ├─ 기록시각(KST): "2026-04-21 09:40:00"
#   ├─ 실현손익(원) : 해당 시점의 **당일** 실현손익
#   ├─ 누적수익금(원): 해당 시점의 누적 실현손익
#
# 실현손익 차트 계산:
#   당일 실현손익 = 실현손익(원) 열 값
#   누적 실현손익 = 누적수익금(원) 열 값
#
# 특징:
#   - 매도가 한 번도 없어서 실현손익이 0 원이더라도, 스냅샷이 기록되는 날마다
#     (0, 0) 점이 찍혀 0 원 기준선 위에 수평 시계열이 자연스럽게 그려진다.
#
# 외부 의존:
#   gspread, oauth2client (requirements.txt 에 포함)
#
# 사용법:
#   import pnl_chart
#   pnl_chart.run_pnl_chart()

import os
from collections import defaultdict
from datetime import datetime

import pytz
from dotenv import load_dotenv

# 스크립트 절대경로 기준 디렉토리 (cwd 독립)
_DIR = os.path.dirname(os.path.abspath(__file__))

# 프로젝트 폴더의 .env 를 명시적으로 로드 — crontab 의 cwd 가 달라도 안전
load_dotenv(os.path.join(_DIR, ".env"))

KST = pytz.timezone("Asia/Seoul")


def _resolve_service_account_path() -> str:
    """GOOGLE_SERVICE_ACCOUNT_JSON 환경변수를 읽어 절대경로로 반환.

    값이 상대경로면 이 파일(`pnl_chart.py`) 이 있는 디렉토리를 기준으로 결합.
    반환값은 os.path.exists 판정에 그대로 쓸 수 있는 절대경로.
    """
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json") or "service_account.json"
    return raw if os.path.isabs(raw) else os.path.join(_DIR, raw)

# 워크시트·차트 상수
SOURCE_SHEET_NAME_REAL = "포트폴리오 추이"
PNL_SHEET_NAME_REAL    = "손익차트"
PNL_HEADERS            = ["날짜", "당일 실현손익(원)", "누적 실현손익(원)"]


def _source_sheet_name() -> str:
    return SOURCE_SHEET_NAME_REAL


def _pnl_sheet_name() -> str:
    return PNL_SHEET_NAME_REAL


# '포트폴리오 추이' 시트 열 이름 (trade_ledger.PORTFOLIO_HEADERS 와 일치해야 함)
COL_TS_KST          = "기록시각(KST)"
COL_REALIZED_PNL    = "실현손익(원)"
COL_CUMULATIVE_PNL  = "누적수익금(원)"

# ─────────────────────────────────────────
# 1-B) 기간 단위 선택 관련 상수
# ─────────────────────────────────────────
# 손익차트의 X축을 일/주/월/분기/년 중 어떤 단위로 묶을지 표현하는 내부 코드.
# 사용자에게는 시트의 K1 드롭다운에서 한글 라벨로 보여진다.
PERIOD_DAY     = "DAY"
PERIOD_WEEK    = "WEEK"
PERIOD_MONTH   = "MONTH"
PERIOD_QUARTER = "QUARTER"
PERIOD_YEAR    = "YEAR"
VALID_PERIODS  = [PERIOD_DAY, PERIOD_WEEK, PERIOD_MONTH, PERIOD_QUARTER, PERIOD_YEAR]

# 시트의 K1 드롭다운 셀에 보일 한글 라벨 ↔ 내부 영문 코드 매핑
PERIOD_LABEL_KOR = {
    PERIOD_DAY:     "일",
    PERIOD_WEEK:    "주",
    PERIOD_MONTH:   "월",
    PERIOD_QUARTER: "분기",
    PERIOD_YEAR:    "년",
}

# 드롭다운/라벨 셀 좌표 (손익차트 시트 안)
DROPDOWN_CELL_LABEL = "J1"   # "차트 단위" 라벨이 들어가는 셀
DROPDOWN_CELL_VALUE = "K1"   # 사용자가 단위를 고르는 드롭다운 셀

# 보조 단위 데이터의 헤더 (E~G열에 들어감)
PNL_HEADERS_PERIOD = ["기간", "당일 실현손익(원)", "누적 실현손익(원)"]

# 차트 제목 (역할 고정 라벨 — 단위가 바뀌어도 차트가 누적되지 않도록)
CHART_TITLE_DAILY  = "실현 손익 누적 차트 (일 단위)"
CHART_TITLE_PERIOD = "실현 손익 누적 차트 (선택 단위)"
# ─────────────────────────────────────────
# 1) Google Sheets 인증
# ─────────────────────────────────────────

def _get_spreadsheet():
    """서비스 계정으로 gspread 인증 후 스프레드시트를 반환한다.

    인증 실패·설정 미비 시 None 을 반환한다.
    """
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
    except ImportError:
        print("[pnl_chart] gspread 미설치 → 차트 갱신 스킵")
        return None

    json_path   = _resolve_service_account_path()
    sheet_title = os.getenv("GOOGLE_SPREADSHEET_TITLE", "Upbit Hybrid Turtle Ledger")

    if not os.path.exists(json_path):
        print(f"[pnl_chart] 서비스 계정 JSON 없음(resolve: {json_path}) → 차트 갱신 스킵")
        return None

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    try:
        creds  = ServiceAccountCredentials.from_json_keyfile_name(json_path, scope)
        client = gspread.authorize(creds)
        return client.open(sheet_title)
    except Exception as e:
        print(f"[pnl_chart] 스프레드시트 열기 오류(무시하고 계속): {e}")
        return None


def _get_worksheet(spreadsheet, title: str, create_if_missing: bool = False,
                   rows: int = 1000, cols: int = 15):
    """워크시트를 찾고 없으면 옵션에 따라 생성한다.

    cols 기본값 15: 손익차트는 A~G 데이터 + J~K 드롭다운/라벨 + K3/K29 차트 앵커까지
    접근하려면 최소 11열 필요. 안전 마진을 두어 15열로 생성.
    """
    import gspread
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        if create_if_missing:
            ws = spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)
            print(f"[pnl_chart] '{title}' 워크시트 생성 (rows={rows}, cols={cols})")
            return ws
        return None


def _ensure_grid_capacity(worksheet, min_rows: int = 1000, min_cols: int = 15):
    """워크시트의 행/컬럼 수가 기준 미만이면 확장한다.

    이미 충분하면 아무 일도 하지 않는다. 기존에 좁게 만들어진 시트에서
    K1 셀 접근·K3/K29 차트 임베드 오류를 방지한다.
    """
    try:
        cur_rows = worksheet.row_count
        cur_cols = worksheet.col_count
        new_rows = max(cur_rows, min_rows)
        new_cols = max(cur_cols, min_cols)
        if new_rows != cur_rows or new_cols != cur_cols:
            worksheet.resize(rows=new_rows, cols=new_cols)
            print(f"[pnl_chart] '{worksheet.title}' 시트 크기 확장 "
                  f"({cur_rows}x{cur_cols} → {new_rows}x{new_cols})")
    except Exception as e:
        print(f"[pnl_chart] 시트 크기 확장 오류(무시): {e}")


# ─────────────────────────────────────────
# 2) '포트폴리오 추이' 시트 → 날짜별 실현손익 시계열
# ─────────────────────────────────────────

def _parse_date(ts_kst: str) -> str:
    """'2026-04-21 09:40:00' → '2026-04-21'. 파싱 실패 시 빈 문자열."""
    if not ts_kst:
        return ""
    try:
        return datetime.strptime(ts_kst, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
    except Exception:
        return ts_kst.split(" ", 1)[0] if " " in ts_kst else ts_kst


def _to_float(val) -> float:
    """시트 셀 값을 float 로 안전 변환. ',' '+' '원' 등 혼입 가능성 고려."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("원", "").replace("+", "")
    if s in ("", "-"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _to_int(val) -> int:
    return int(round(_to_float(val)))


def _calc_aligned_axis_bounds(series: list):
    """두 Y축의 0 기준선과 눈금 간격을 동일하게 맞추기 위한 min/max 계산.

    두 데이터(당일·누적)를 합쳐 하나의 '보기 좋은' 눈금 간격을 결정하고,
    두 축 모두 같은 min/max 범위를 사용한다.
    → 눈금 간격이 동일하고, 0 원이 같은 가로선에 온다.

    Returns:
        (axis_min, axis_max, axis_min, axis_max) — 두 축 공통 범위
        데이터가 없으면 (None, None, None, None)
    """
    import math

    if not series:
        return None, None, None, None

    left_vals  = [s["daily_pnl"]      for s in series]
    right_vals = [s["cumulative_pnl"] for s in series]

    # 두 데이터 전체 + 0 을 합친 min/max
    data_min = min(min(left_vals), min(right_vals), 0)
    data_max = max(max(left_vals), max(right_vals), 0)

    overall_span = max(data_max - data_min, 1.0)

    # 약 5 구간을 목표로 '보기 좋은' 눈금 간격 산출
    raw_interval = overall_span / 5.0
    magnitude    = 10 ** math.floor(math.log10(raw_interval))
    norm         = raw_interval / magnitude
    if norm < 1.5:
        interval = magnitude
    elif norm < 3.5:
        interval = 2.0 * magnitude
    elif norm < 7.5:
        interval = 5.0 * magnitude
    else:
        interval = 10.0 * magnitude

    # 0 기준 위·아래로 필요한 눈금 칸 수
    n_below = math.ceil(abs(data_min) / interval) if data_min < 0 else 0
    n_above = math.ceil(data_max      / interval) if data_max > 0 else 0
    if n_below == 0 and n_above == 0:
        n_above = 1

    axis_min = float(-n_below * interval)
    axis_max = float( n_above * interval)

    # 두 축에 동일한 범위 적용 → 눈금 간격·0 위치 자동 정렬
    return axis_min, axis_max, axis_min, axis_max


def build_series_from_portfolio(rows: list) -> list:
    """'포트폴리오 추이' 시트 행들에서 날짜별 실현손익 시계열을 만든다.

    같은 날 여러 행이 있을 경우 가장 마지막 기록을 그 날의 종가로 본다.

    Args:
        rows: get_all_records 로 얻은 dict 리스트 (헤더 포함된 첫 행은 제외됨)

    Returns:
        [
            {
                "date":           "2026-04-21",
                "daily_pnl":      0.0,
                "cumulative_pnl": 0.0,
            },
            ...
        ]
    """
    if not rows:
        return []

    daily_last = {}  # date -> {"daily": float, "cumulative": float}

    for r in rows:
        ts_kst = r.get(COL_TS_KST, "")
        date_str = _parse_date(ts_kst)
        if not date_str:
            continue
        daily_pnl = _to_float(r.get(COL_REALIZED_PNL, 0))
        cumulative = _to_float(r.get(COL_CUMULATIVE_PNL, 0))
        # 구버전 호환: 누적수익금 컬럼이 없으면 기존 실현손익(누적) 컬럼을 사용
        if COL_CUMULATIVE_PNL not in r:
            cumulative = _to_float(r.get(COL_REALIZED_PNL, 0))
        # 같은 날 여러 행이면 마지막 것으로 덮어쓴다
        daily_last[date_str] = {
            "daily":      daily_pnl,
            "cumulative": cumulative,
            "ts_kst":     ts_kst,
        }

    sorted_dates = sorted(daily_last.keys())
    if not sorted_dates:
        return []

    series = []
    for d in sorted_dates:
        cur = daily_last[d]
        cumulative = cur["cumulative"]
        daily_pnl  = cur["daily"]
        series.append({
            "date":           d,
            "daily_pnl":      round(daily_pnl, 2),
            "cumulative_pnl": round(cumulative, 2),
        })

    return series


# ─────────────────────────────────────────
# 2-B) 기간 단위 변환 — 일 단위 시계열을 주/월/분기/년 단위로 묶음
# ─────────────────────────────────────────

def _bucket_key(date_str: str, period: str) -> str:
    """날짜 문자열을 기간 단위 키로 변환한다.

    날짜 한 개가 들어오면, 그 날짜가 속한 기간(주/월/분기/년)을 식별하는
    문자열 키를 돌려준다. 같은 기간에 속한 날짜들은 같은 키를 갖게 되므로,
    이 키를 그룹화의 기준으로 사용할 수 있다.

    Args:
        date_str: 'YYYY-MM-DD' 형태의 날짜 문자열 (예: '2026-05-29')
        period:   PERIOD_DAY/WEEK/MONTH/QUARTER/YEAR 중 하나

    Returns:
        단위별 키 문자열. 잘못된 입력이면 빈 문자열.
          - 일:   '2026-05-29'
          - 주:   '2026-W22'  (ISO 표준, 월요일 시작)
          - 월:   '2026-05'
          - 분기: '2026-Q2'   (1~3월=Q1, 4~6월=Q2, 7~9월=Q3, 10~12월=Q4)
          - 년:   '2026'
    """
    if not date_str:
        return ""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return ""

    if period == PERIOD_DAY:
        return date_str
    if period == PERIOD_WEEK:
        # %G = ISO 연도(주가 속한 해), %V = ISO 주차 두 자리(01~53, 월요일 시작)
        return dt.strftime("%G-W%V")
    if period == PERIOD_MONTH:
        return dt.strftime("%Y-%m")
    if period == PERIOD_QUARTER:
        q = (dt.month - 1) // 3 + 1
        return f"{dt.year}-Q{q}"
    if period == PERIOD_YEAR:
        return dt.strftime("%Y")
    # 알 수 없는 단위 → 일 단위로 폴백 (안전 기본값)
    return date_str


def build_period_series(daily_series: list, period: str) -> list:
    """일 단위 시계열을 기간 단위(주/월/분기/년)로 묶은 새 시계열로 변환한다.

    묶음 규칙:
      - 당일 실현손익(daily_pnl) : 그 기간 내 일별 값들의 **합산**
      - 누적 실현손익(cumulative_pnl) : 그 기간 내 **가장 마지막 날짜의 값**
                                       (누적은 항상 가장 최신값을 보여주는 게 맞음)

    Args:
        daily_series: build_series_from_portfolio() 의 출력 형태.
            [{"date": "2026-05-29",
              "daily_pnl":      100.0,
              "cumulative_pnl": 500.0}, ...]
        period: PERIOD_* 상수 중 하나

    Returns:
        기간별 시계열 리스트:
            [{"period_key":     "2026-W22",
              "daily_pnl":      300.0,    # 그 기간 일별 합산
              "cumulative_pnl": 900.0},   # 그 기간 마지막 날짜의 누적
             ...]
        - period == PERIOD_DAY → 입력을 그대로 통일된 키 이름으로 매핑해서 반환
        - 빈 입력 → 빈 리스트
        - 알 수 없는 단위 → 일 단위 폴백
    """
    if not daily_series:
        return []

    # 일 단위면 별도 그룹화 없이 키 이름만 통일해서 반환 (이후 시트 작성 시 형식 일관성 유지)
    if period == PERIOD_DAY:
        return [
            {
                "period_key":     s.get("date", ""),
                "daily_pnl":      float(s.get("daily_pnl", 0) or 0),
                "cumulative_pnl": float(s.get("cumulative_pnl", 0) or 0),
            }
            for s in daily_series
        ]

    # 기간 단위로 그룹화 — key별로 일별 합산 + 마지막 날짜의 누적값 추적
    buckets = {}   # period_key -> {"daily_sum", "last_date", "last_cumulative"}
    for s in daily_series:
        date_str = s.get("date", "")
        key = _bucket_key(date_str, period)
        if not key:
            continue   # 날짜 파싱 실패 행은 건너뜀

        if key not in buckets:
            buckets[key] = {
                "daily_sum":       0.0,
                "last_date":       "",
                "last_cumulative": 0.0,
            }
        b = buckets[key]
        b["daily_sum"] += float(s.get("daily_pnl", 0) or 0)
        # 같은 기간 안에서 가장 늦은 날짜의 누적값을 그 기간의 누적값으로 사용
        if date_str >= b["last_date"]:
            b["last_date"]       = date_str
            b["last_cumulative"] = float(s.get("cumulative_pnl", 0) or 0)

    # 키 사전순 정렬 = 시간순 정렬
    # (일 'YYYY-MM-DD', 주 'YYYY-Www', 월 'YYYY-MM', 분기 'YYYY-Qn', 년 'YYYY'
    #  모든 형식에서 문자열 사전순이 시간순과 일치하도록 설계됨)
    sorted_keys = sorted(buckets.keys())
    return [
        {
            "period_key":     k,
            "daily_pnl":      round(buckets[k]["daily_sum"], 2),
            "cumulative_pnl": round(buckets[k]["last_cumulative"], 2),
        }
        for k in sorted_keys
    ]


# ─────────────────────────────────────────
# 2-C) 드롭다운 셀 제어 — 사용자가 손익차트 시트에서 단위를 고르게 함
# ─────────────────────────────────────────

def _read_dropdown_period(worksheet) -> str:
    """손익차트 시트의 K1 셀에서 사용자가 고른 단위(한글)를 읽어 영문 PERIOD_* 코드로 변환한다.

    K1 이 비었거나 알 수 없는 값(직접 입력 등)이면 안전하게 PERIOD_DAY 로 폴백.
    셀 읽기 자체가 실패해도 PERIOD_DAY 로 폴백하므로 진입점이 안전하게 진행된다.

    Args:
        worksheet: gspread Worksheet 객체 (손익차트 시트)

    Returns:
        PERIOD_DAY/WEEK/MONTH/QUARTER/YEAR 중 하나의 영문 코드 문자열.
    """
    try:
        raw = worksheet.acell(DROPDOWN_CELL_VALUE).value
    except Exception as e:
        print(f"[pnl_chart] K1 셀 읽기 오류(일 단위로 폴백): {e}")
        return PERIOD_DAY

    label = (raw or "").strip()
    if not label:
        return PERIOD_DAY

    # 한글 라벨 → 영문 코드 역매핑
    for code, kor in PERIOD_LABEL_KOR.items():
        if label == kor:
            return code
    # 알 수 없는 값(예: 사용자가 임의로 입력) → 안전하게 일 단위 폴백
    return PERIOD_DAY


def _install_dropdown_and_label(spreadsheet, worksheet, current_value: str):
    """손익차트 시트에 차트 단위 라벨(J1)과 드롭다운(K1) 을 설치한다.

    설치 내용:
      - J1 셀: '차트 단위' 라벨 (안내용 텍스트)
      - K1 셀: current_value 를 한글로 변환해 값으로 채움
      - K1 셀에 데이터 검증(드롭다운) 규칙 부여 → 클릭 시 일/주/월/분기/년 5개 옵션 표시

    같은 규칙을 반복 적용해도 부작용이 없도록(idempotent) 설계되어
    매 갱신마다 안전하게 다시 호출 가능하다.
    각 단계에서 오류가 발생해도 매매·차트 갱신 흐름을 차단하지 않도록 예외는 모두 흡수.

    Args:
        spreadsheet:    gspread Spreadsheet 객체 (batch_update 용)
        worksheet:      gspread Worksheet 객체 (손익차트 시트)
        current_value:  K1 에 복원할 단위 코드 (PERIOD_*).
                        VALID_PERIODS 에 없으면 PERIOD_DAY 로 강제 보정.
    """
    # 1) J1 에 라벨 채우기
    try:
        worksheet.update(values=[["차트 단위"]],
                         range_name=DROPDOWN_CELL_LABEL,
                         value_input_option="RAW")
    except Exception as e:
        print(f"[pnl_chart] J1 라벨 쓰기 오류(무시): {e}")

    # 2) K1 에 한글 단위명 채우기 (clear() 직후 복원 용도)
    if current_value not in VALID_PERIODS:
        current_value = PERIOD_DAY
    kor_label = PERIOD_LABEL_KOR.get(current_value, PERIOD_LABEL_KOR[PERIOD_DAY])
    try:
        worksheet.update(values=[[kor_label]],
                         range_name=DROPDOWN_CELL_VALUE,
                         value_input_option="RAW")
    except Exception as e:
        print(f"[pnl_chart] K1 단위값 쓰기 오류(무시): {e}")

    # 3) K1 셀에 데이터 검증(드롭다운) 규칙 설치
    #    Google Sheets API 의 setDataValidation 을 사용.
    #    K1 좌표 = (행 0, 열 10) — 0-indexed
    sheet_id = worksheet.id
    body = {
        "requests": [{
            "setDataValidation": {
                "range": {
                    "sheetId":          sheet_id,
                    "startRowIndex":    0,  "endRowIndex":    1,
                    "startColumnIndex": 10, "endColumnIndex": 11,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [
                            {"userEnteredValue": PERIOD_LABEL_KOR[PERIOD_DAY]},
                            {"userEnteredValue": PERIOD_LABEL_KOR[PERIOD_WEEK]},
                            {"userEnteredValue": PERIOD_LABEL_KOR[PERIOD_MONTH]},
                            {"userEnteredValue": PERIOD_LABEL_KOR[PERIOD_QUARTER]},
                            {"userEnteredValue": PERIOD_LABEL_KOR[PERIOD_YEAR]},
                        ],
                    },
                    "showCustomUi": True,   # 드롭다운 화살표 UI 표시
                    "strict":       True,   # 다른 값 입력 시 거부
                },
            }
        }]
    }
    try:
        spreadsheet.batch_update(body)
    except Exception as e:
        print(f"[pnl_chart] K1 드롭다운 규칙 설치 오류(무시): {e}")


# ─────────────────────────────────────────
# 3) 손익차트 시트 갱신
# ─────────────────────────────────────────

def update_pnl_worksheet(daily_series: list, period_series: list, period: str,
                         spreadsheet, worksheet) -> bool:
    """'손익차트' 워크시트의 데이터 영역을 갱신한다.

    영역 구성:
      - A~C 열: 일 단위 시계열 (항상 채워짐)
      - E~G 열: 선택 단위 시계열 (period != DAY 일 때만, DAY 면 비움)
      - J1: '차트 단위' 라벨
      - K1: 사용자 선택 단위 (드롭다운) — clear() 직후 자동 복원

    Args:
        daily_series:  일 단위 시계열 (build_series_from_portfolio 출력)
        period_series: 선택 단위 시계열 (build_period_series 출력)
        period:        PERIOD_* 코드 — 현재 선택 단위
        spreadsheet:   gspread Spreadsheet 객체
        worksheet:     gspread Worksheet 객체 ('손익차트' 시트, 호출자가 보유)

    Returns:
        True 성공 / False 실패
    """
    try:
        # 1) 시트 전체 비우기 (셀 값만 지워지고, 데이터 검증 규칙은 일부 유지될 수 있음)
        worksheet.clear()

        # 2) A1~C: 일 단위 데이터
        daily_values = [PNL_HEADERS]
        for item in daily_series:
            daily_values.append([item["date"], item["daily_pnl"], item["cumulative_pnl"]])
        worksheet.update(values=daily_values, range_name="A1", value_input_option="RAW")

        # 3) E1~G: 선택 단위 데이터 (DAY 면 작성 생략 — 영역 빈 상태로 둠)
        if period != PERIOD_DAY and period_series:
            period_values = [PNL_HEADERS_PERIOD]
            for item in period_series:
                period_values.append([item["period_key"], item["daily_pnl"], item["cumulative_pnl"]])
            worksheet.update(values=period_values, range_name="E1", value_input_option="RAW")

        sec_count = 0 if period == PERIOD_DAY else len(period_series)
        kor = PERIOD_LABEL_KOR.get(period, period)
        print(f"[pnl_chart] '{_pnl_sheet_name()}' 데이터 갱신 완료 "
              f"(일 {len(daily_series)}일, {kor} {sec_count}개)")
    except Exception as e:
        print(f"[pnl_chart] 워크시트 데이터 갱신 오류: {e}")
        return False

    # 4) 드롭다운/라벨 재설치 — clear() 로 K1 값이 지워졌으므로 현재 단위로 복원
    _install_dropdown_and_label(spreadsheet, worksheet, period)
    return True


# ─────────────────────────────────────────
# 4) 라인 차트 임베드 (최초 1회만)
# ─────────────────────────────────────────

def _find_chart_id(spreadsheet, sheet_id: int, title: str):
    """주어진 워크시트에서 제목이 일치하는 차트 ID를 찾는다."""
    try:
        meta = spreadsheet.fetch_sheet_metadata()
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("sheetId") != sheet_id:
                continue
            for ch in sheet.get("charts", []):
                chart_id = ch.get("chartId")
                spec = ch.get("spec", {})
                if spec.get("title", "") == title:
                    return chart_id
        return None
    except Exception as e:
        print(f"[pnl_chart] 기존 차트 확인 실패(재생성 시도): {e}")
        return None


def _delete_chart_if_exists(spreadsheet, sheet_id: int, title: str):
    """같은 제목 차트가 있으면 삭제한다."""
    chart_id = _find_chart_id(spreadsheet, sheet_id, title)
    if not chart_id:
        return

    body = {
        "requests": [
            {
                "deleteEmbeddedObject": {
                    "objectId": chart_id
                }
            }
        ]
    }
    try:
        spreadsheet.batch_update(body)
        print(f"[pnl_chart] 기존 '{title}' 차트 삭제 후 재생성")
    except Exception as e:
        print(f"[pnl_chart] 기존 차트 삭제 실패(무시하고 계속): {e}")


def _embed_one_chart(spreadsheet, worksheet, title: str, subtitle: str,
                     bottom_axis_title: str,
                     data_start_col: int, series: list,
                     anchor_row: int, anchor_col: int) -> bool:
    """콤보 차트(당일=파란 막대, 누적=빨간 선) 1개를 워크시트에 임베드한다.

    차트 구성:
      - X축(도메인): data_start_col 컬럼 (날짜 또는 기간 키)
      - 막대 시리즈: data_start_col + 1 컬럼 (당일 실현손익)
      - 선  시리즈: data_start_col + 2 컬럼 (누적 실현손익)
      - 두 시리즈 모두 왼쪽 Y축 공유 → 0 기준선·눈금 간격이 자동 일치

    같은 제목의 차트가 이미 있으면 먼저 삭제 후 재생성 (제목은 역할 고정 라벨).

    Args:
        title:             차트 제목 (CHART_TITLE_DAILY / CHART_TITLE_PERIOD)
        subtitle:          차트 부제
        bottom_axis_title: X축 제목 (예: "날짜" / "기간")
        data_start_col:    데이터 시작 컬럼 (0-indexed). A=0, E=4.
        series:            축 범위 계산용 시계열 (daily_pnl/cumulative_pnl 키 필요)
        anchor_row:        차트 앵커 셀 행 (0-indexed)
        anchor_col:        차트 앵커 셀 컬럼 (0-indexed)

    Returns:
        True 성공 / False 실패 (예외는 모두 흡수되어 호출자 흐름 방해 없음)
    """
    sheet_id = worksheet.id

    # 같은 제목 차트가 있으면 먼저 삭제 (역할 고정 라벨이라 누적되지 않음)
    _delete_chart_if_exists(spreadsheet, sheet_id, title)

    # 왼쪽 축 범위 계산 (두 시리즈 합산)
    l_min, l_max, _, _ = _calc_aligned_axis_bounds(series or [])
    left_axis = {"position": "LEFT_AXIS", "title": "실현손익(원)"}
    if l_min is not None:
        left_axis["viewWindowOptions"] = {
            "viewWindowMode": "EXPLICIT",
            "viewWindowMin":  l_min,
            "viewWindowMax":  l_max,
        }

    # 데이터 컬럼 범위 — 도메인/막대/선 각각 1열씩
    domain_col = data_start_col
    bar_col    = data_start_col + 1
    line_col   = data_start_col + 2

    body = {
        "requests": [{
            "addChart": {
                "chart": {
                    "spec": {
                        "title":    title,
                        "subtitle": subtitle,
                        "basicChart": {
                            "chartType":      "COMBO",
                            "legendPosition": "TOP_LEGEND",
                            "headerCount":    1,
                            "axis": [
                                {"position": "BOTTOM_AXIS", "title": bottom_axis_title},
                                left_axis,
                            ],
                            "domains": [{
                                "domain": {
                                    "sourceRange": {
                                        "sources": [{
                                            "sheetId":          sheet_id,
                                            "startRowIndex":    0,
                                            "startColumnIndex": domain_col,
                                            "endColumnIndex":   domain_col + 1,
                                        }]
                                    }
                                }
                            }],
                            "series": [
                                {
                                    # 당일 실현손익 — 파란 막대
                                    "series": {
                                        "sourceRange": {
                                            "sources": [{
                                                "sheetId":          sheet_id,
                                                "startRowIndex":    0,
                                                "startColumnIndex": bar_col,
                                                "endColumnIndex":   bar_col + 1,
                                            }]
                                        }
                                    },
                                    "targetAxis": "LEFT_AXIS",
                                    "type":  "COLUMN",
                                    "color": {"red": 0.44, "green": 0.68, "blue": 0.84},
                                },
                                {
                                    # 누적 실현손익 — 빨간 선
                                    "series": {
                                        "sourceRange": {
                                            "sources": [{
                                                "sheetId":          sheet_id,
                                                "startRowIndex":    0,
                                                "startColumnIndex": line_col,
                                                "endColumnIndex":   line_col + 1,
                                            }]
                                        }
                                    },
                                    "targetAxis": "LEFT_AXIS",
                                    "type":  "LINE",
                                    "color": {"red": 0.84, "green": 0.18, "blue": 0.18},
                                },
                            ],
                        },
                    },
                    "position": {
                        "overlayPosition": {
                            "anchorCell": {
                                "sheetId":     sheet_id,
                                "rowIndex":    anchor_row,
                                "columnIndex": anchor_col,
                            },
                            "widthPixels":  800,
                            "heightPixels": 480,
                        }
                    },
                }
            }
        }]
    }

    try:
        spreadsheet.batch_update(body)
        print(f"[pnl_chart] '{title}' 콤보 차트 임베드 완료 "
              f"(앵커 row={anchor_row}, col={anchor_col})")
        return True
    except Exception as e:
        print(f"[pnl_chart] 차트 임베드 오류(무시하고 계속): {e}")
        return False


# ─────────────────────────────────────────
# 5) 엔트리 포인트
# ─────────────────────────────────────────

def run_pnl_chart():
    """run_daily.py 와 trade_ledger.refresh_sheets_after_sell() 가 호출하는 진입점.

    동작 흐름:
      ① 스프레드시트 열기
      ② '손익차트' 시트 가져오기 (없으면 생성)
      ③ K1 셀에서 사용자가 고른 단위 읽기 (없으면 PERIOD_DAY)
      ④ '포트폴리오 추이' 시트에서 일별 데이터 읽기
      ⑤ 일 단위 시계열 → 선택 단위 시계열 변환
      ⑥ 손익차트 시트의 A~C(일), E~G(선택) 영역 데이터 갱신 + 드롭다운 복원
      ⑦ 메인 차트(일 단위) 임베드 — 항상
      ⑧ 보조 차트(선택 단위) 임베드 — PERIOD_DAY 면 잔재만 제거하고 생성 안 함
    """
    source_sheet = _source_sheet_name()
    print(f"[pnl_chart] 실현 손익 차트 갱신 시작 (소스: '{source_sheet}' 시트)")

    # ① 스프레드시트
    spreadsheet = _get_spreadsheet()
    if spreadsheet is None:
        return

    # ② 손익차트 시트 (없으면 생성) — 데이터 갱신 전에 시트 존재 보장 + K1 읽기 위함
    pnl_ws = _get_worksheet(spreadsheet, _pnl_sheet_name(), create_if_missing=True)
    if pnl_ws is None:
        return

    # ②-B K1·K3·K29 영역 접근을 위해 시트 그리드 크기 보장 (기존 시트가 좁을 수 있음)
    _ensure_grid_capacity(pnl_ws, min_rows=1000, min_cols=15)

    # ③ K1 셀에서 현재 선택된 단위 읽기 (ws.clear() 전에 먼저 읽어야 함)
    period = _read_dropdown_period(pnl_ws)

    # ④ 소스 시트(포트폴리오 추이) 데이터
    source_ws = _get_worksheet(spreadsheet, source_sheet, create_if_missing=False)
    if source_ws is None:
        print(f"[pnl_chart] '{source_sheet}' 시트 없음 → 차트 갱신 스킵 "
              f"(run_daily STEP 2 가 최소 1번 실행된 뒤 재시도)")
        return

    try:
        records = source_ws.get_all_records()
    except Exception as e:
        print(f"[pnl_chart] '{source_sheet}' 시트 읽기 오류: {e}")
        return

    if not records:
        print(f"[pnl_chart] '{source_sheet}' 시트에 데이터 없음 → 스킵")
        return

    # ⑤ 일 단위 시계열 + 선택 단위 시계열
    daily_series = build_series_from_portfolio(records)
    if not daily_series:
        print("[pnl_chart] 파싱 가능한 일자 데이터 없음 → 스킵")
        return

    period_series = build_period_series(daily_series, period)

    # ⑥ 시트 데이터 갱신 (A~C 일, E~G 선택, K1 드롭다운 복원)
    if not update_pnl_worksheet(daily_series, period_series, period, spreadsheet, pnl_ws):
        return

    # ⑦ 메인 차트 (일 단위) — 항상 임베드
    _embed_one_chart(
        spreadsheet, pnl_ws,
        title             = CHART_TITLE_DAILY,
        subtitle          = "Upbit Hybrid Turtle · KRW 기준 (포트폴리오 추이 소스)",
        bottom_axis_title = "날짜",
        data_start_col    = 0,    # A~C 컬럼
        series            = daily_series,
        anchor_row        = 2,    # K3 부근
        anchor_col        = 10,
    )

    # ⑧ 보조 차트 (선택 단위) — DAY 면 잔재 제거, 그 외엔 임베드
    if period == PERIOD_DAY:
        _delete_chart_if_exists(spreadsheet, pnl_ws.id, CHART_TITLE_PERIOD)
    else:
        unit_kor = PERIOD_LABEL_KOR[period]
        # 차트 도메인은 시트 E열을 읽으므로, 축 범위 계산용 series 만 만들어 전달
        sec_series = [
            {"daily_pnl": s["daily_pnl"], "cumulative_pnl": s["cumulative_pnl"]}
            for s in period_series
        ]
        _embed_one_chart(
            spreadsheet, pnl_ws,
            title             = CHART_TITLE_PERIOD,
            subtitle          = f"{unit_kor} 단위로 묶음 (Upbit Hybrid Turtle · KRW 기준)",
            bottom_axis_title = "기간",
            data_start_col    = 4,    # E~G 컬럼
            series            = sec_series,
            anchor_row        = 28,   # K29 부근 (메인 차트 480px ≈ 24행 아래)
            anchor_col        = 10,
        )

    latest = daily_series[-1]
    print(f"[pnl_chart] 최종 — 기간 {daily_series[0]['date']}~{latest['date']} | "
          f"누적 실현손익: {latest['cumulative_pnl']:+,.0f}원 "
          f"({len(daily_series)}일, 최근 당일 {latest['daily_pnl']:+,.0f}원, 단위={period})")


if __name__ == "__main__":
    run_pnl_chart()
