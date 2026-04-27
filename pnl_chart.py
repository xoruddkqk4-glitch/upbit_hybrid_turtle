# pnl_chart.py
# 실현 손익 누적 차트 — Google Sheets 갱신 모듈
#
# 역할:
#   '포트폴리오 추이' 시트에 일 1회 기록되는 '실현손익(원)' 열을 데이터 소스로 삼아
#   '손익차트' 워크시트에 [날짜 / 당일 실현손익 / 누적 실현손익]
#   세 열을 쓰고, 콤보 그래프(막대+선)를 임베드한다.
#
# 데이터 소스:
#   '포트폴리오 추이' 시트 (trade_ledger.record_portfolio_snapshot 이 하루 1회 기록)
#   ├─ 기록시각(KST): "2026-04-21 09:40:00"
#   ├─ 실현손익(원) : 해당 시점의 **누적** 실현손익 (업비트 API 미제공 → 원장 자체 계산)
#
# 실현손익 차트 계산:
#   당일 실현손익 = 오늘 스냅샷 누적값 − 전일 스냅샷 누적값
#   누적 실현손익 = 오늘 스냅샷 누적값 그대로
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
CHART_TITLE_REAL       = "실현 손익 누적 차트"


def _source_sheet_name() -> str:
    return SOURCE_SHEET_NAME_REAL


def _pnl_sheet_name() -> str:
    return PNL_SHEET_NAME_REAL


def _chart_title() -> str:
    return CHART_TITLE_REAL

# '포트폴리오 추이' 시트 열 이름 (trade_ledger.PORTFOLIO_HEADERS 와 일치해야 함)
COL_TS_KST          = "기록시각(KST)"
COL_REALIZED_PNL    = "실현손익(원)"
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
                   rows: int = 1000, cols: int = 10):
    """워크시트를 찾고 없으면 옵션에 따라 생성한다."""
    import gspread
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        if create_if_missing:
            ws = spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)
            print(f"[pnl_chart] '{title}' 워크시트 생성")
            return ws
        return None


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


def build_series_from_portfolio(rows: list) -> list:
    """'포트폴리오 추이' 시트 행들에서 날짜별 실현손익 시계열을 만든다.

    같은 날 여러 행이 있을 경우 가장 마지막 기록(누적값)을 그 날의 종가로 본다.
    전일 대비 증감을 당일 실현손익으로 계산한다.

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

    daily_last = {}  # date -> {"cumulative": float}

    for r in rows:
        ts_kst = r.get(COL_TS_KST, "")
        date_str = _parse_date(ts_kst)
        if not date_str:
            continue
        cumulative = _to_float(r.get(COL_REALIZED_PNL, 0))
        # 같은 날 여러 행이면 마지막 것으로 덮어쓴다
        daily_last[date_str] = {
            "cumulative": cumulative,
            "ts_kst":     ts_kst,
        }

    sorted_dates = sorted(daily_last.keys())
    if not sorted_dates:
        return []

    series = []
    prev_cumulative = 0.0
    for d in sorted_dates:
        cur = daily_last[d]
        cumulative = cur["cumulative"]
        daily_pnl  = cumulative - prev_cumulative
        series.append({
            "date":           d,
            "daily_pnl":      round(daily_pnl, 2),
            "cumulative_pnl": round(cumulative, 2),
        })
        prev_cumulative = cumulative

    return series


# ─────────────────────────────────────────
# 3) 손익차트 시트 갱신
# ─────────────────────────────────────────

def update_pnl_worksheet(series: list, spreadsheet) -> tuple:
    """실현 손익 시계열을 '손익차트' 워크시트에 덮어쓴다.

    Returns:
        (spreadsheet, worksheet) 튜플 또는 (None, None) (갱신 실패).
    """
    pnl_sheet = _pnl_sheet_name()
    ws = _get_worksheet(spreadsheet, pnl_sheet, create_if_missing=True)
    if ws is None:
        return None, None

    values = [PNL_HEADERS]
    for item in series:
        values.append([
            item["date"],
            item["daily_pnl"],
            item["cumulative_pnl"],
        ])

    try:
        ws.clear()
        ws.update(values=values, range_name="A1", value_input_option="RAW")
        print(f"[pnl_chart] '{pnl_sheet}' 데이터 갱신 완료 ({len(series)}일)")
    except Exception as e:
        print(f"[pnl_chart] 워크시트 데이터 갱신 오류: {e}")
        return None, None

    return spreadsheet, ws


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


def embed_line_chart(spreadsheet, worksheet) -> bool:
    """콤보 그래프(당일=막대, 누적=선)를 임베드한다. A:C 범위 자동 확장."""
    sheet_id    = worksheet.id
    chart_title = _chart_title()
    subtitle    = "Upbit Hybrid Turtle · KRW 기준 (포트폴리오 추이 소스)"

    # 요구사항 반영을 위해 같은 제목 차트가 있으면 삭제 후 재생성
    _delete_chart_if_exists(spreadsheet, sheet_id, chart_title)

    body = {
        "requests": [
            {
                "addChart": {
                    "chart": {
                        "spec": {
                            "title":    chart_title,
                            "subtitle": subtitle,
                            "basicChart": {
                                "chartType":   "COMBO",
                                "legendPosition": "BOTTOM_LEGEND",
                                "headerCount": 1,
                                "axis": [
                                    {"position": "BOTTOM_AXIS", "title": "날짜"},
                                    {"position": "LEFT_AXIS",   "title": "실현 손익(원)"},
                                ],
                                "domains": [{
                                    "domain": {
                                        "sourceRange": {
                                            "sources": [{
                                                "sheetId":          sheet_id,
                                                "startRowIndex":    0,
                                                "startColumnIndex": 0,
                                                "endColumnIndex":   1,
                                            }]
                                        }
                                    }
                                }],
                                "series": [
                                    {
                                        "series": {
                                            "sourceRange": {
                                                "sources": [{
                                                    "sheetId":          sheet_id,
                                                    "startRowIndex":    0,
                                                    "startColumnIndex": 1,
                                                    "endColumnIndex":   2,
                                                }]
                                            }
                                        },
                                        "targetAxis": "LEFT_AXIS",
                                        "type": "COLUMN",
                                    },
                                    {
                                        "series": {
                                            "sourceRange": {
                                                "sources": [{
                                                    "sheetId":          sheet_id,
                                                    "startRowIndex":    0,
                                                    "startColumnIndex": 2,
                                                    "endColumnIndex":   3,
                                                }]
                                            }
                                        },
                                        "targetAxis": "LEFT_AXIS",
                                        "type": "LINE",
                                    },
                                ],
                            },
                        },
                        "position": {
                            "overlayPosition": {
                                "anchorCell": {
                                    "sheetId":     sheet_id,
                                    "rowIndex":    1,
                                    "columnIndex": 5,
                                },
                                "widthPixels":  640,
                                "heightPixels": 380,
                            }
                        },
                    }
                }
            }
        ]
    }

    try:
        spreadsheet.batch_update(body)
        print(f"[pnl_chart] '{chart_title}' 콤보 차트 임베드 완료 (당일=막대, 누적=선)")
        return True
    except Exception as e:
        print(f"[pnl_chart] 차트 임베드 오류(무시하고 계속): {e}")
        return False


# ─────────────────────────────────────────
# 5) 엔트리 포인트
# ─────────────────────────────────────────

def run_pnl_chart():
    """run_daily.py 마지막 단계에서 호출하는 진입점.

    '포트폴리오 추이' 시트의 '실현손익(원)' 열을 시계열 소스로 삼아
    '손익차트' 시트를 갱신하고, 최초 1회 차트를 삽입한다.

    매도가 한 번도 없어 누적 실현손익이 0 이더라도, 일 스냅샷이 쌓인 날짜마다
    (날짜, 0, 0) 로 점이 찍히므로 기준선 형태의 그래프가 정상 생성된다.
    """
    source_sheet = _source_sheet_name()
    print(f"[pnl_chart] 실현 손익 차트 갱신 시작 (소스: '{source_sheet}' 시트)")

    spreadsheet = _get_spreadsheet()
    if spreadsheet is None:
        return

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

    series = build_series_from_portfolio(records)
    if not series:
        print("[pnl_chart] 파싱 가능한 일자 데이터 없음 → 스킵")
        return

    spreadsheet, worksheet = update_pnl_worksheet(series, spreadsheet)
    if spreadsheet is None or worksheet is None:
        return

    embed_line_chart(spreadsheet, worksheet)

    latest = series[-1]
    print(f"[pnl_chart] 최종 — 기간 {series[0]['date']}~{latest['date']} | "
          f"누적 실현손익: {latest['cumulative_pnl']:+,.0f}원 "
          f"({len(series)}일, 최근 당일 {latest['daily_pnl']:+,.0f}원)")


if __name__ == "__main__":
    run_pnl_chart()
