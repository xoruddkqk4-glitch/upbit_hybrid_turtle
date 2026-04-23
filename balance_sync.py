# balance_sync.py
# 실행 시작 시 실제 Upbit 잔고와 held_coin_record.json 을 비교·동기화한다.
#
# 호출 시점: run_all.py 의 STEP 1(로그인) 직후, STEP 2(손절감시) 전
# 목적: 수동 거래나 시스템 장애로 두 데이터가 어긋나면
#       잘못된 손절가 계산·중복 매수·청산 누락 등이 발생할 수 있어 미리 정정한다.
#
# 동기화 기준: 실제 잔고가 진실. held_coin_record.json 을 맞춘다.
#
# 처리 규칙:
#   ① 기록엔 있는데 실제 없음  → 기록 삭제 + 텔레그램 알림
#   ② 실제 있는데 기록 없음    → 최초 1회 알림 + MANUAL_SYNC 로 자동 편입
#                                 (추가 매수 없이 매도 전략만 적용)
#   ③ 둘 다 있는데 수량 다름   → 기록 수량을 실제로 교체 + 텔레그램 알림
#   ④ 먼지 잔량(평가액 5000원 미만) → ②③ 비교 대상 제외

import json
import os

import indicator_calc
import upbit_client
from telegram_alert import SendMessage

_DIR              = os.path.dirname(os.path.abspath(__file__))
_HELD_RECORD_FILE = os.path.join(_DIR, "held_coin_record.json")

# 먼지 잔량 기준 (원화 평가액) — 이 금액 미만이면 비교 대상에서 제외
# (수수료·에어드랍으로 생긴 아주 작은 잔량은 무시)
_DUST_THRESHOLD_KRW = 5_000

# 수량 허용 오차 — 소수점 8자리 코인의 미세 오차 무시
_VOLUME_TOLERANCE = 1e-8


def _load_held_record() -> dict:
    """held_coin_record.json 을 읽는다. 파일 없으면 빈 dict 반환."""
    if not os.path.exists(_HELD_RECORD_FILE):
        return {}
    try:
        with open(_HELD_RECORD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[balance_sync] held_coin_record.json 읽기 오류: {e}")
        return {}


def _save_held_record(record: dict):
    """held_coin_record.json 에 덮어쓴다."""
    try:
        with open(_HELD_RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[balance_sync] held_coin_record.json 저장 오류: {e}")


def run_balance_sync() -> bool:
    """실제 잔고와 held_coin_record.json 을 비교해서 불일치를 수정한다.

    Returns:
        True  — 동기화 성공 (불일치가 없거나 수정 완료)
        False — 잔고 조회 자체가 실패했을 때 (자동매매 중단 신호)
    """
    # ── 1. 실제 잔고 조회 ──────────────────────────────────────────────────
    actual_list = upbit_client.get_balance()

    # get_balance() 가 빈 리스트를 반환하는 두 가지 경우:
    #   (a) API 오류 → 자동매매 중단 필요
    #   (b) 보유 코인이 진짜 없음 → 정상 (계속 진행)
    # 두 경우를 구분하기 위해 KRW 잔고 / 총자본으로 API 생존 여부를 판단한다.
    if not actual_list:
        krw     = upbit_client.get_krw_balance()
        capital = upbit_client.get_total_capital()
        if krw == 0.0 and capital == 0.0:
            print("[balance_sync] 잔고 조회 실패 (API 오류 의심) → False 반환")
            return False

    # ── 2. 먼지 잔량 제외 후 ticker → 정보 딕셔너리로 정리 ─────────────────
    # 먼지 잔량: 수수료·에어드랍 등으로 쌓인 5000원 미만 평가액 코인
    actual: dict[str, dict] = {}
    for b in actual_list:
        ticker        = b.get("ticker", "")
        volume        = float(b.get("volume", 0))
        current_price = float(b.get("current_price", 0))
        coin_name     = b.get("coin_name", ticker.replace("KRW-", ""))

        eval_krw = volume * current_price
        if eval_krw < _DUST_THRESHOLD_KRW:
            print(
                f"[balance_sync] 먼지 잔량 제외: {ticker} "
                f"{volume:.8f} (평가액 {eval_krw:.0f}원)"
            )
            continue

        actual[ticker] = {
            "volume":        volume,
            "coin_name":     coin_name,
            "current_price": current_price,
            "avg_price":     float(b.get("avg_price", 0)),  # 업비트 평균 매입단가
        }

    # ── 3. 기록 읽기 ───────────────────────────────────────────────────────
    record  = _load_held_record()
    changed = False

    # ── ① 기록엔 있는데 실제로 없는 코인 → 기록 삭제 ─────────────────────
    for ticker in list(record.keys()):
        if ticker not in actual:
            coin_name = ticker.replace("KRW-", "")
            msg = (
                f"⚠️ [잔고동기화] {coin_name}({ticker}) — "
                f"실제 잔고 없음. 기록 삭제함."
            )
            print(msg)
            SendMessage(msg)
            del record[ticker]
            changed = True

    # ── ② 실제로 있는데 기록에 없는 코인 → MANUAL_SYNC 로 자동 편입 ──────
    # 수동 매수 코인을 최초 발견 시 1회만 알림 발송하고 held_coin_record 에 추가한다.
    # 이후 실행에서는 기록이 있으므로 알림이 반복되지 않는다.
    # max_unit = 1 + manual = True 로 피라미딩을 막고, 매도 전략(2N·10일 신저가·5MA)만 적용한다.
    for ticker, info in actual.items():
        if ticker not in record:
            avg_price = info.get("avg_price", 0.0) or info["current_price"]

            # ATR 캐시에서 손절가 계산 (추가 API 호출 없음)
            try:
                indicators = indicator_calc.get_all_indicators(ticker)
                atr_n = indicators.get("atr", 0.0)
            except Exception:
                atr_n = 0.0

            stop_loss     = round(avg_price - 2.0 * atr_n, 8) if atr_n > 0 else 0.0
            stop_loss_str = f"{stop_loss:,.0f}원" if atr_n > 0 else "산출 불가(ATR 없음)"

            # 수동 편입 항목: max_unit=1, current_unit=1 → 피라미딩 조건 자동 탈락
            record[ticker] = {
                "current_unit":       1,
                "last_buy_price":     avg_price,
                "avg_buy_price":      avg_price,
                "stop_loss_price":    stop_loss,
                "next_pyramid_price": 999_999_999_999,  # 절대 도달 못할 값 (2중 안전장치)
                "max_unit":           1,
                "total_volume":       info["volume"],
                "entry_source":       "MANUAL_SYNC",
                "manual":             True,
            }
            changed = True

            msg = (
                f"⚠️ [잔고동기화] {info['coin_name']}({ticker}) 수동 매수 코인 발견 — 전략 편입 완료\n"
                f"수량: {info['volume']:.8f}개\n"
                f"평균 매입가: {avg_price:,.0f}원\n"
                f"손절가: {stop_loss_str}\n"
                f"→ 추가 매수 없이 매도 전략(손절/익절)만 적용합니다."
            )
            print(msg)
            SendMessage(msg)

    # ── ③ 둘 다 있는데 수량이 다른 코인 → total_volume 수정 ──────────────
    for ticker, info in actual.items():
        if ticker not in record:
            continue  # ②에서 이미 알림 발송 완료

        actual_vol = info["volume"]
        record_vol = float(record[ticker].get("total_volume", 0))

        if abs(actual_vol - record_vol) > _VOLUME_TOLERANCE:
            msg = (
                f"⚠️ [잔고동기화] {info['coin_name']}({ticker}) 수량 불일치: "
                f"기록 {record_vol:.8f} → 실제 {actual_vol:.8f} 으로 수정."
            )
            print(msg)
            SendMessage(msg)
            record[ticker]["total_volume"] = actual_vol
            changed = True

    # ── 4. 변경사항 저장 ──────────────────────────────────────────────────
    if changed:
        _save_held_record(record)
        print("[balance_sync] held_coin_record.json 업데이트 완료 ✅")
    else:
        print("[balance_sync] 잔고 일치 — 동기화 불필요 ✅")

    return True
