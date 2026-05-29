# pnl_chart.py
# 실현 손익 차트 업데이터 (Upbit Hybrid Turtle 버전)
#
# 구글 스프레드시트의 '포트폴리오 추이' 시트에서 일별 실현손익·누적수익금을 읽어
# '손익차트' 시트에 콤보 차트(파란 막대=일일 / 빨간 선=누적) 1개를 자동으로 그린다.
#
# X축 기간 단위(일/주/월/분기/년)는 손익차트 시트의 F1 드롭다운으로 즉시 전환된다.
#   - 일별 데이터를 5가지 기준으로 미리 집계해 숨김 시트('차트데이터') 에 저장
#   - 손익차트 시트의 A2:C2 한 줄짜리 배열 수식(ARRAYFORMULA + CHOOSE) 이
#     드롭다운에 따라 해당 집계 블록을 가리키도록 작성
#   - 사용자가 F1 을 바꾸면 시트가 즉시 재계산 → 차트가 자동으로 새 단위로 다시 그려짐
#     (Apps Script 등 외부 트리거 불필요, 구글 시트 기본 기능)
#
# 집계 규칙:
#   - 일일 손익  = 그 기간 안의 일별 손익 합계
#   - 누적 손익 = 그 기간 안의 마지막 날짜의 누적값 (누적은 이미 running total)
#
# 호출자:
#   - trade_ledger.refresh_sheets_after_sell()  매도 즉시 갱신
#   - run_daily.py STEP 3                        매일 23:55 갱신
#
# 단독 실행:
#   python pnl_chart.py    (즉시 갱신용)

import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

# 스크립트 절대경로 기준 디렉토리 (crontab 의 cwd 가 달라도 안전)
_DIR = os.path.dirname(os.path.abspath(__file__))

# 프로젝트 폴더의 .env 를 명시적으로 로드
load_dotenv(os.path.join(_DIR, ".env"))

# ─────────────────────────────────────────
# 시트·드롭다운 상수
# ─────────────────────────────────────────

# 구글 시트 탭 이름
CHART_SHEET_NAME     = "손익차트"        # 차트를 그릴 탭
DATA_SHEET_NAME      = "차트데이터"      # 5개 단위 집계를 담는 숨김 탭
PORTFOLIO_SHEET_NAME = "포트폴리오 추이"  # 원본 데이터 탭

# 드롭다운에서 고를 수 있는 기간 단위 (한글 = 사용자 표시 / 영문 = 내부 집계 키)
GRAN_KO = ["일", "주", "월", "분기", "년"]
GRAN_EN = ["day", "week", "month", "quarter", "year"]

# 손익차트 시트의 드롭다운 셀 좌표
DROPDOWN_LABEL_CELL = "E1"   # "기간 단위 선택 ▶" 안내 문구
DROPDOWN_CELL       = "F1"   # 실제 드롭다운 셀


# ─────────────────────────────────────────
# 인증 / 스프레드시트 연결
# ─────────────────────────────────────────

def _resolve_service_account_path() -> str:
    """GOOGLE_SERVICE_ACCOUNT_JSON 환경변수를 절대경로로 변환해 반환한다.

    값이 상대경로면 이 파일이 있는 디렉토리를 기준으로 결합하므로,
    crontab 의 cwd 가 달라도 안전하게 동작한다.
    """
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json") or "service_account.json"
    return raw if os.path.isabs(raw) else os.path.join(_DIR, raw)


def _get_spreadsheet():
    """구글 스프레드시트에 연결하고 Spreadsheet 객체를 반환한다.

    실패 시 None 을 반환해 호출자가 안전하게 스킵할 수 있게 한다.
    (자동매매·매도 흐름이 차트 오류로 차단되면 안 되므로 예외는 모두 흡수)
    """
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
    except ImportError:
        print("[pnl_chart] gspread / oauth2client 미설치 → 차트 갱신 스킵")
        return None

    json_path   = _resolve_service_account_path()
    sheet_title = os.getenv("GOOGLE_SPREADSHEET_TITLE", "Upbit Hybrid Turtle Ledger")

    if not os.path.exists(json_path):
        print(f"[pnl_chart] 서비스 계정 JSON 없음 ({json_path}) → 차트 갱신 스킵")
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
        print(f"[pnl_chart] 스프레드시트 열기 오류(차트 갱신 스킵): {e}")
        return None


def _parse_money(cell_value) -> int:
    """금액 문자열을 정수(원) 로 변환한다.

    예: "+1,200" → 1200, "-500" → -500, "" → 0
    숫자가 아니거나 비어 있으면 0 으로 처리.
    """
    if cell_value is None:
        return 0
    if isinstance(cell_value, (int, float)):
        return int(cell_value)
    s = str(cell_value).strip()
    if not s:
        return 0
    s = s.replace(",", "").replace("+", "").replace("원", "")
    try:
        return int(float(s))
    except ValueError:
        return 0


# ─────────────────────────────────────────
# 일별 데이터 만들기 + 5개 단위 집계
# ─────────────────────────────────────────

def _build_daily_rows(spreadsheet):
    """'포트폴리오 추이' 시트에서 일별 (날짜, 일일손익, 누적손익) 시계열을 만든다.

    같은 날짜에 여러 행(daily snapshot + sell 갱신 행) 이 있어도 마지막 값을 사용.
    데이터가 없는 날도 (일일 0원, 누적 = 직전값) 으로 채워서 시계열을 끊김 없이 만든다.

    Returns:
        [("2026-05-29", 일일손익, 누적손익), ...] 시간 오름차순
    """
    import gspread

    try:
        ws = spreadsheet.worksheet(PORTFOLIO_SHEET_NAME)
    except gspread.WorksheetNotFound:
        print(f"[pnl_chart] '{PORTFOLIO_SHEET_NAME}' 시트가 없어 차트 데이터를 만들 수 없습니다.")
        return []

    try:
        all_rows = ws.get_all_values()
    except Exception as e:
        print(f"[pnl_chart] '{PORTFOLIO_SHEET_NAME}' 시트 읽기 오류: {e}")
        return []

    if not all_rows or len(all_rows) <= 1:
        print(f"[pnl_chart] '{PORTFOLIO_SHEET_NAME}' 데이터가 없습니다.")
        return []

    # 날짜별로 마지막 기록값만 보존
    by_date = {}
    for row in all_rows[1:]:
        if not row:
            continue
        ts = row[0].strip() if len(row) > 0 else ""
        if len(ts) < 10:
            continue
        try:
            day = datetime.strptime(ts[:10], "%Y-%m-%d").date()
        except ValueError:
            continue

        # G열(7번째) = 실현손익(원), J열(10번째) = 누적수익금(원)
        realized   = _parse_money(row[6] if len(row) > 6 else "0")
        cumulative = _parse_money(row[9] if len(row) > 9 else "0")
        by_date[day] = (realized, cumulative)

    if not by_date:
        print(f"[pnl_chart] '{PORTFOLIO_SHEET_NAME}' 에 차트용 손익 데이터가 없습니다.")
        return []

    # 첫 날~마지막 날까지 빈 날은 0원으로 채움 (누적은 직전값 유지)
    start_day = min(by_date.keys())
    end_day   = max(by_date.keys())
    prev_cumulative = 0
    result = []

    day = start_day
    while day <= end_day:
        if day in by_date:
            realized, cumulative = by_date[day]
            prev_cumulative = cumulative
        else:
            realized   = 0
            cumulative = prev_cumulative
        result.append((day.strftime("%Y-%m-%d"), realized, cumulative))
        day += timedelta(days=1)

    return result


def _period_label(day, gran: str) -> str:
    """날짜(date) 를 기간 단위별 라벨 문자열로 변환.

    - day(일):     "2026-05-29"
    - week(주):    "2026-W22"  (ISO 주차, 월요일 시작)
    - month(월):   "2026-05"
    - quarter(분기): "2026-Q2"
    - year(년):    "2026"
    """
    if gran == "day":
        return day.strftime("%Y-%m-%d")
    if gran == "week":
        iso = day.isocalendar()    # (ISO 연도, 주차, 요일)
        return f"{iso[0]}-W{iso[1]:02d}"
    if gran == "month":
        return f"{day.year}-{day.month:02d}"
    if gran == "quarter":
        q = (day.month - 1) // 3 + 1
        return f"{day.year}-Q{q}"
    if gran == "year":
        return f"{day.year}"
    return day.strftime("%Y-%m-%d")


def _aggregate(daily_rows, gran: str):
    """일별 데이터를 기간 단위(gran) 로 집계한다.

    - 일일손익: 기간 내 일별 손익의 합계
    - 누적손익: 기간 내 마지막 날짜의 누적값
                (누적은 이미 running total 이므로 기간 끝의 값이 곧 그 기간의 누적)

    Returns:
        [(라벨, 일일손익합, 기간말누적), ...] 시간순
    """
    buckets = {}   # 라벨 → [일일합, 마지막누적]
    order   = []   # 라벨 등장 순서 (daily_rows 가 이미 오름차순이라 자연스러운 시간순 보장)

    for date_str, daily, cumulative in daily_rows:
        try:
            day = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        label = _period_label(day, gran)
        if label not in buckets:
            buckets[label] = [0, 0]
            order.append(label)
        buckets[label][0] += daily         # 일일손익 누적 합
        buckets[label][1]  = cumulative    # 마지막 값으로 계속 덮어씀 → 기간말 누적

    return [(label, buckets[label][0], buckets[label][1]) for label in order]


def _compute_all_blocks(daily_rows):
    """5가지 기간 단위 집계 결과를 한꺼번에 만든다.

    Returns:
        {"일": [...], "주": [...], "월": [...], "분기": [...], "년": [...]}
    """
    return {ko: _aggregate(daily_rows, en) for ko, en in zip(GRAN_KO, GRAN_EN)}


# ─────────────────────────────────────────
# 숨김 데이터 시트('차트데이터') 작성
# ─────────────────────────────────────────

def _write_data_sheet(spreadsheet, blocks):
    """5개 집계 블록을 숨김 시트('차트데이터') 에 나란히 기록한다.

    블록 배치 (각 블록은 라벨/일일/누적 3열, 블록 사이에 빈 열 1개):
        일   : A B C       (col 0,1,2)
        주   : E F G       (col 4,5,6)
        월   : I J K       (col 8,9,10)
        분기 : M N O       (col 12,13,14)
        년   : Q R S       (col 16,17,18)

    매 호출마다 기존 차트데이터 시트를 삭제 후 새로 만들어 항상 최신 상태를 유지.

    Returns:
        (ws_data, max_len) — 데이터 워크시트 + 가장 긴 블록의 행 수 (보통 '일')
    """
    import gspread

    try:
        old = spreadsheet.worksheet(DATA_SHEET_NAME)
        spreadsheet.del_worksheet(old)
    except gspread.WorksheetNotFound:
        pass

    max_len = max((len(blocks[ko]) for ko in GRAN_KO), default=0)
    height  = max_len + 1   # 헤더 1행 + 데이터
    width   = 19            # A~S (5블록 × 3열 + 사이 빈 열 4개)

    ws_data = spreadsheet.add_worksheet(
        title=DATA_SHEET_NAME,
        rows=max(height + 5, 10),
        cols=width + 1,
    )

    # 빈 격자 준비 (모두 빈 문자열)
    grid = [["" for _ in range(width)] for _ in range(height)]

    # 블록별로 채우기
    for bi, ko in enumerate(GRAN_KO):
        col0 = bi * 4   # 0, 4, 8, 12, 16
        # 헤더
        grid[0][col0]     = f"기간({ko})"
        grid[0][col0 + 1] = "일일손익"
        grid[0][col0 + 2] = "누적손익"
        # 데이터
        for ri, (label, daily, cumulative) in enumerate(blocks[ko], start=1):
            grid[ri][col0]     = label
            grid[ri][col0 + 1] = daily
            grid[ri][col0 + 2] = cumulative

    # RAW: 라벨("2026-W22" 등) 이 날짜/수식으로 오해되지 않도록 그대로 저장
    try:
        ws_data.update(values=grid, range_name="A1", value_input_option="RAW")
        print(f"[pnl_chart] '{DATA_SHEET_NAME}' 집계 데이터 기록 완료 (최대 {max_len}행)")
    except Exception as e:
        print(f"[pnl_chart] '{DATA_SHEET_NAME}' 데이터 쓰기 오류: {e}")

    return ws_data, max_len


# ─────────────────────────────────────────
# 손익차트 시트 (드롭다운 + ARRAYFORMULA + 차트)
# ─────────────────────────────────────────

def _read_prev_selection(spreadsheet) -> str:
    """기존 손익차트 시트의 F1 드롭다운 선택값을 읽어 반환.

    시트가 없거나 값이 이상하면 "일"을 반환 (안전 기본값).
    매 갱신마다 시트를 재생성해도 사용자가 고른 단위가 유지되도록.
    """
    import gspread
    try:
        ws = spreadsheet.worksheet(CHART_SHEET_NAME)
        val = (ws.acell(DROPDOWN_CELL).value or "").strip()
        if val in GRAN_KO:
            return val
    except gspread.WorksheetNotFound:
        pass
    except Exception:
        pass
    return "일"


def _view_formulas(max_len: int):
    """손익차트 A2/B2/C2 에 넣을 ARRAYFORMULA 3개를 만든다.

    드롭다운(F1) 선택값에 따라 '차트데이터' 의 해당 블록(라벨/일일/누적) 을 가리킨다.
    선택 블록의 데이터 개수를 넘는 행은 ""(빈칸)으로 둬 차트에 0이 잘못 찍히지 않게 함.

    동작:
      - F1 = "일" → 차트데이터의 A/B/C 열을 가져옴
      - F1 = "주" → 차트데이터의 E/F/G 열을 가져옴
      - ... 같은 식으로 5개 블록 중 하나를 선택
    """
    sht = DATA_SHEET_NAME
    end = max_len + 1   # 데이터 마지막 행 (헤더 1행 포함)

    label_cols = ["A", "E", "I", "M", "Q"]
    daily_cols = ["B", "F", "J", "N", "R"]
    cum_cols   = ["C", "G", "K", "O", "S"]

    # 드롭다운 F1 → 1~5 번호
    idx = 'MATCH($F$1,{"일","주","월","분기","년"},0)'

    # 선택 블록의 데이터 개수 (라벨 열 COUNTA) — 이 행 수까지만 값 채움
    cnt_args   = ",".join(f"COUNTA('{sht}'!${c}$2:${c}${end})" for c in label_cols)
    choose_cnt = f"CHOOSE({idx},{cnt_args})"

    # 행 번호 드라이버 (2..end → 1..max_len)
    row_drv = f"(ROW('{sht}'!$A$2:$A${end})-1)"

    def choose(cols):
        args = ",".join(f"'{sht}'!${c}$2:${c}${end}" for c in cols)
        return f"CHOOSE({idx},{args})"

    a = f"=ARRAYFORMULA(IF({row_drv}<={choose_cnt},{choose(label_cols)},\"\"))"
    b = f"=ARRAYFORMULA(IF({row_drv}<={choose_cnt},{choose(daily_cols)},\"\"))"
    c = f"=ARRAYFORMULA(IF({row_drv}<={choose_cnt},{choose(cum_cols)},\"\"))"
    return a, b, c


def _build_chart_sheet(spreadsheet, max_len: int, prev_sel: str):
    """손익차트 시트를 새로 만들고 헤더·드롭다운·수식 표를 채운다.

    매 호출마다 기존 시트를 삭제 후 재생성 (이전 차트·잔재가 절대 누적되지 않음).
    F1 의 이전 선택값은 prev_sel 로 전달받아 그대로 복원.
    """
    import gspread

    # 기존 손익차트 시트 삭제 (차트 포함 완전 초기화)
    try:
        old_ws = spreadsheet.worksheet(CHART_SHEET_NAME)
        spreadsheet.del_worksheet(old_ws)
        print(f"[pnl_chart] 기존 '{CHART_SHEET_NAME}' 시트 삭제 완료")
    except gspread.WorksheetNotFound:
        pass

    # 새 시트 생성 (행: 헤더+데이터+여유 / 열: 차트 영역까지 넉넉히)
    num_rows = max(max_len + 11, 50)
    ws = spreadsheet.add_worksheet(title=CHART_SHEET_NAME, rows=num_rows, cols=20)
    print(f"[pnl_chart] '{CHART_SHEET_NAME}' 시트 새로 생성")

    # ① 표 헤더 (A1:C1) — 차트의 범례·축 이름으로 사용됨
    ws.update(
        values=[["기간", "일일 실현손익(원)", "누적 실현손익(원)"]],
        range_name="A1:C1",
        value_input_option="RAW",
    )

    # ② 드롭다운 안내 문구(E1) + 선택값(F1)
    ws.update(
        values=[["기간 단위 선택 ▶", prev_sel]],
        range_name=f"{DROPDOWN_LABEL_CELL}:{DROPDOWN_CELL}",
        value_input_option="RAW",
    )

    # ③ 배열 수식 표 (A2/B2/C2) — 드롭다운에 따라 자동으로 내용이 바뀜
    a, b, c = _view_formulas(max_len)
    ws.update(values=[[a, b, c]], range_name="A2:C2", value_input_option="USER_ENTERED")

    print(f"[pnl_chart] 드롭다운 + 수식 표 작성 완료 (현재 선택: {prev_sel})")
    return ws


def _add_dropdown_validation(requests, sheet_id):
    """F1 셀에 일/주/월/분기/년 드롭다운(데이터 검증) 규칙을 추가."""
    requests.append({
        "setDataValidation": {
            "range": {
                "sheetId":          sheet_id,
                "startRowIndex":    0,   # 1행
                "endRowIndex":      1,
                "startColumnIndex": 5,   # F열
                "endColumnIndex":   6,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in GRAN_KO],
                },
                "showCustomUi": True,   # 셀에 드롭다운 화살표 표시
                "strict":       True,   # 목록 밖 값 입력 차단
            },
        }
    })


def _add_combo_chart_request(requests, sheet_id, max_len: int):
    """콤보 차트(파란 막대=일일 / 빨간 선=누적) 생성 요청 추가.

    데이터 출처: A열(기간 라벨), B열(일일), C열(누적) — 모두 수식으로 채워지는 표.
    드롭다운을 바꾸면 이 셀들이 다시 계산돼 차트가 자동으로 바뀜.
    """
    start_row = 0
    end_row   = max_len + 1   # 헤더 1행 + 데이터 max_len행

    requests.append({
        "addChart": {
            "chart": {
                "spec": {
                    "title": "실현 손익 추이 (기간 단위는 F1 드롭다운으로 선택)",
                    "basicChart": {
                        "chartType":      "COMBO",
                        "legendPosition": "TOP_LEGEND",
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "기간"},
                            {"position": "LEFT_AXIS",   "title": "손익 (원)"},
                        ],
                        # X축: A열 기간 라벨
                        "domains": [{
                            "domain": {"sourceRange": {"sources": [{
                                "sheetId":          sheet_id,
                                "startRowIndex":    start_row,
                                "endRowIndex":      end_row,
                                "startColumnIndex": 0,   # A열
                                "endColumnIndex":   1,
                            }]}}
                        }],
                        "series": [
                            # 파란 막대 — B열 일일 실현손익
                            {
                                "series": {"sourceRange": {"sources": [{
                                    "sheetId":          sheet_id,
                                    "startRowIndex":    start_row,
                                    "endRowIndex":      end_row,
                                    "startColumnIndex": 1,
                                    "endColumnIndex":   2,
                                }]}},
                                "targetAxis": "LEFT_AXIS",
                                "type":       "COLUMN",
                                "colorStyle": {"rgbColor": {"red": 0.44, "green": 0.68, "blue": 0.83}},
                            },
                            # 빨간 선 — C열 누적 실현손익
                            {
                                "series": {"sourceRange": {"sources": [{
                                    "sheetId":          sheet_id,
                                    "startRowIndex":    start_row,
                                    "endRowIndex":      end_row,
                                    "startColumnIndex": 2,
                                    "endColumnIndex":   3,
                                }]}},
                                "targetAxis": "LEFT_AXIS",
                                "type":       "LINE",
                                "colorStyle": {"rgbColor": {"red": 0.84, "green": 0.15, "blue": 0.15}},
                                "lineStyle":  {"width": 2},
                            },
                        ],
                        "headerCount": 1,   # 첫 행을 범례 이름으로 사용
                    }
                },
                # 차트 위치: H2 셀 기준 (드롭다운 E1:F1 과 겹치지 않게 오른쪽 배치)
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId":     sheet_id,
                            "rowIndex":    1,   # 2행
                            "columnIndex": 7,   # H열
                        },
                        "widthPixels":  1000,
                        "heightPixels": 580,
                    }
                },
            }
        }
    })


def _hide_sheet_request(requests, sheet_id):
    """차트데이터 시트를 숨김 처리하는 요청을 추가 (보기 깔끔하게)."""
    requests.append({
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "hidden": True},
            "fields":     "hidden",
        }
    })


# ─────────────────────────────────────────
# 공개 진입점
# ─────────────────────────────────────────

def update_pnl_chart():
    """실현 손익 차트를 구글 스프레드시트에 (재) 작성한다.

    '포트폴리오 추이' → 일별 데이터 → 5단위 집계 → 차트데이터(숨김) + 손익차트(표시).
    드롭다운(F1) 으로 기간 단위 즉시 전환 가능 (Apps Script 불필요).
    매 호출마다 두 시트를 모두 새로 생성하므로 이전 잔재가 누적되지 않는다.
    F1 의 사용자 선택값은 자동으로 유지된다.
    """
    print("[pnl_chart] 실현 손익 차트 갱신 시작 (소스: '포트폴리오 추이' 시트)")

    # ① 구글 시트 연결
    spreadsheet = _get_spreadsheet()
    if spreadsheet is None:
        return

    # ② 일별 데이터 생성
    daily_rows = _build_daily_rows(spreadsheet)
    if not daily_rows:
        print("[pnl_chart] 그릴 데이터가 없습니다. 종료합니다.")
        return
    print(f"[pnl_chart] 일별 {len(daily_rows)}일치 데이터 준비 완료")

    # ③ 5가지 기간 단위로 집계
    blocks = _compute_all_blocks(daily_rows)

    # ④ 기존 드롭다운 선택값 읽어두기 (재생성 후에도 유지)
    prev_sel = _read_prev_selection(spreadsheet)

    # ⑤ 숨김 데이터 시트에 집계 결과 기록
    try:
        ws_data, max_len = _write_data_sheet(spreadsheet, blocks)
    except Exception as e:
        print(f"[pnl_chart] 데이터 시트 작성 오류: {e}")
        return
    if max_len <= 0:
        print("[pnl_chart] 집계된 데이터가 없습니다. 종료합니다.")
        return

    # ⑥ 손익차트 시트 (재)생성 + 헤더 + 드롭다운 값 + ARRAYFORMULA
    try:
        ws_chart = _build_chart_sheet(spreadsheet, max_len, prev_sel)
    except Exception as e:
        print(f"[pnl_chart] 손익차트 시트 작성 오류: {e}")
        return

    # ⑦ 드롭다운 규칙 + 차트 + 데이터시트 숨김을 batch_update 한 번에 적용
    try:
        requests = []
        _add_dropdown_validation(requests, ws_chart.id)
        _add_combo_chart_request(requests, ws_chart.id, max_len)
        _hide_sheet_request(requests, ws_data.id)
        spreadsheet.batch_update({"requests": requests})
        print("[pnl_chart] 드롭다운·콤보 차트 생성 + 데이터 시트 숨김 완료")
    except Exception as e:
        print(f"[pnl_chart] 차트/검증 규칙 적용 오류(무시하고 계속): {e}")

    print(
        f"[pnl_chart] ✅ 완료 — '{spreadsheet.title}' / '{CHART_SHEET_NAME}' 탭에서 "
        f"F1 드롭다운으로 일/주/월/분기/년 전환 가능 (현재: {prev_sel})"
    )


# 기존 호출자(`trade_ledger.refresh_sheets_after_sell`, `run_daily.py`) 호환용 별칭.
# 진입점 이름을 바꾸지 않기 위해 동일 함수를 다른 이름으로도 노출한다.
run_pnl_chart = update_pnl_chart


if __name__ == "__main__":
    update_pnl_chart()
