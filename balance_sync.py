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
from typing import Optional

import indicator_calc
import trade_ledger
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


def _record_manual_trades(ticker: str, coin_name: str, ref_avg_price: float) -> list:
    """그 종목의 Upbit done 주문 중 ledger 에 없는 주문을 '수동 거래'로 시트에 기록한다.

    잔고 불일치 ①②③ 분기 안에서만 호출된다 (잔고 일치 시엔 호출 안 함).
    같은 주문이 반복 기록되지 않도록 ledger 의 기존 order_no 와 대조한다.

    Args:
        ticker:        업비트 티커 (예: "KRW-BTC")
        coin_name:     코인 이름 (예: "비트코인")
        ref_avg_price: 매도 손익 계산용 기준 평균가
                       (held_coin_record.json 의 avg_buy_price)

    Returns:
        시간 오름차순(오래된 것 → 최신순)으로 정렬된 수동 매수 정보 리스트:
            [{"unit_price": float, "volume": float, "created_at": str}, ...]
        평균가 재계산(③ 분기)에 사용된다. 수동 매수가 없으면 빈 리스트.
    """
    # ── 1. Upbit 에서 체결완료 주문 받아오기 ───────────────────────────────
    done_orders = upbit_client.fetch_recent_done_orders(ticker)
    if not done_orders:
        return []

    # ── 2. 이미 ledger 에 기록된 order_no 집합 ─────────────────────────────
    recorded_set = trade_ledger.get_recorded_order_nos()

    manual_buys = []   # ③ 평균가 재계산용으로 모아둠

    # ── 3. ledger 에 없는 주문 = 수동 거래 → 각각 기록 ────────────────────
    # 시간 오름차순 처리를 위해 created_at 기준으로 정렬한다
    sorted_orders = sorted(done_orders, key=lambda o: o.get("created_at", ""))

    for order in sorted_orders:
        uuid_ = order.get("uuid", "")
        if not uuid_ or uuid_ in recorded_set:
            continue

        # 정확한 체결 단가·수량·수수료를 trades 배열로 재계산
        exec_vol, exec_price, paid_fee = upbit_client.get_execution_detail(uuid_)
        if exec_vol <= 0 or exec_price <= 0:
            print(f"[balance_sync] 수동 주문 체결 정보 확인 불가 → 스킵 (uuid={uuid_})")
            continue

        # Upbit side: "bid"=매수, "ask"=매도
        side_raw = order.get("side", "")
        if side_raw == "bid":
            side_kor = "BUY"
        elif side_raw == "ask":
            side_kor = "SELL"
        else:
            print(f"[balance_sync] 알 수 없는 side='{side_raw}' → 스킵 (uuid={uuid_})")
            continue

        # ord_type: "limit"=지정가 / "price"=시장가매수 / "market"=시장가매도
        ord_type_raw = str(order.get("ord_type", "")).lower()
        order_type   = "LIMIT" if ord_type_raw == "limit" else "MARKET"

        created_at = order.get("created_at", "")
        gross      = exec_vol * exec_price

        record = {
            "side":       side_kor,
            "ticker":     ticker,
            "coin_name":  coin_name,
            "volume":     exec_vol,
            "unit_price": exec_price,
            "fee":        paid_fee,
            "order_no":   uuid_,
            "order_type": order_type,
            "source":     "MANUAL_BUY" if side_kor == "BUY" else "MANUAL_SELL",
            "note":       f"수동 거래 (Upbit 주문시각: {created_at})",
        }

        # 매도일 경우: 손익은 기준 평균가(=held_coin_record 의 avg_buy_price) 로 계산
        if side_kor == "SELL":
            if ref_avg_price > 0:
                profit_amount = (exec_price - ref_avg_price) * exec_vol - paid_fee
                profit_rate   = (exec_price - ref_avg_price) * 100.0 / ref_avg_price
                record["profit_rate"]   = profit_rate
                record["profit_amount"] = profit_amount
                record["net_amount"]    = gross - paid_fee

        # ledger 시트에 기록 (텔레그램 알림·시트 갱신 자동 수행)
        trade_ledger.append_trade(record)

        # ③ 평균가 재계산용 — 수동 매수만 모아둔다
        if side_kor == "BUY":
            manual_buys.append({
                "unit_price": exec_price,
                "volume":     exec_vol,
                "created_at": created_at,
            })

    return manual_buys


def _apply_manual_buys_to_record(entry: dict, manual_buys: list, atr_n: float):
    """수동 매수가 발생한 경우, held_coin_record 항목의 평균가·손절가·피라미딩가를 재계산한다.

    호출 전제: ③ 수량 불일치 분기에서 manual_buys 가 비어있지 않을 때만 호출.

    Args:
        entry:       held_coin_record[ticker] dict (직접 수정됨)
        manual_buys: _record_manual_trades 가 반환한 수동 매수 리스트
        atr_n:       해당 종목의 현재 ATR(N) 값 (손절·피라미딩가 재계산용)
    """
    if not manual_buys:
        return

    # 기존 평균가·수량 (수동 매수 반영 전 기준)
    old_avg = float(entry.get("avg_buy_price", 0))
    old_vol = float(entry.get("total_volume", 0))

    # 새 평균가 = (기존 평균가 × 기존 수량 + Σ(수동단가 × 수동수량)) / 새 총수량
    total_cost = old_avg * old_vol
    new_vol    = old_vol
    for mb in manual_buys:
        total_cost += mb["unit_price"] * mb["volume"]
        new_vol    += mb["volume"]

    new_avg = (total_cost / new_vol) if new_vol > 0 else old_avg

    # last_buy_price: 가장 최신(=created_at 가장 큰) 수동 매수의 단가로 갱신
    # manual_buys 는 _record_manual_trades 에서 오름차순 정렬되어 들어옴
    last_buy_price = manual_buys[-1]["unit_price"]

    # 손절가·다음 피라미딩가 재계산 (ATR 정상값이 있을 때만)
    if atr_n > 0:
        entry["stop_loss_price"]    = round(new_avg - 2.0 * atr_n, 8)
        entry["next_pyramid_price"] = round(last_buy_price + 0.5 * atr_n, 8)

    entry["avg_buy_price"]  = new_avg
    entry["last_buy_price"] = last_buy_price


def run_balance_sync(snapshot: Optional[dict] = None) -> bool:
    """실제 잔고와 held_coin_record.json 을 비교해서 불일치를 수정한다.

    Returns:
        True  — 동기화 성공 (불일치가 없거나 수정 완료)
        False — 잔고 조회 자체가 실패했을 때 (자동매매 중단 신호)
    """
    # ── 1. 실제 잔고 조회 ──────────────────────────────────────────────────
    actual_list = (snapshot or {}).get("balance")
    if actual_list is None:
        actual_list = upbit_client.get_balance()

    # get_balance() 가 빈 리스트를 반환하는 두 가지 경우:
    #   (a) API 오류 → 자동매매 중단 필요
    #   (b) 보유 코인이 진짜 없음 → 정상 (계속 진행)
    # 두 경우를 구분하기 위해 KRW 잔고 / 총자본으로 API 생존 여부를 판단한다.
    if not actual_list:
        if snapshot:
            krw = float(snapshot.get("krw_balance", 0.0))
            capital = float(snapshot.get("total_capital", 0.0))
        else:
            krw = upbit_client.get_krw_balance()
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
    # 사용자가 수동으로 전량 매도한 경우가 대표적 → done 주문 중 ledger 에 없는 SELL 을 시트에 기록한다.
    for ticker in list(record.keys()):
        if ticker not in actual:
            coin_name      = ticker.replace("KRW-", "")
            ref_avg_price  = float(record[ticker].get("avg_buy_price", 0))

            # 수동 매도 체결 내역을 시트에 기록 (있다면)
            _record_manual_trades(ticker, coin_name, ref_avg_price)

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
    # 또한 수동 매수 체결 내역을 시트에 기록한다 (기록되지 않은 done 주문이 있다면).
    for ticker, info in actual.items():
        if ticker not in record:
            avg_price = info.get("avg_price", 0.0) or info["current_price"]

            # 수동 매수·매도 체결 내역을 시트에 기록 (있다면)
            # 손익 기준 평균가는 Upbit 가 알려준 avg_price 사용 (편입 직전 기준)
            _record_manual_trades(ticker, info["coin_name"], avg_price)

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
                "next_pyramid_price": avg_price * 10,    # max_unit=1 로 피라미딩 차단; 방어용 높은 값
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
    # 수동 거래로 인한 수량 변화가 대표적이므로 done 주문 중 ledger 에 없는 BUY/SELL 을 시트에 기록한다.
    # 또한 수동 매수가 발견되면 평균가·손절가·다음 피라미딩가도 함께 재계산한다.
    for ticker, info in actual.items():
        if ticker not in record:
            continue  # ②에서 이미 알림 발송 완료

        actual_vol = info["volume"]
        record_vol = float(record[ticker].get("total_volume", 0))

        if abs(actual_vol - record_vol) > _VOLUME_TOLERANCE:
            # 수동 거래 체결 내역을 시트에 기록 + 수동 매수 정보 회수
            ref_avg_price = float(record[ticker].get("avg_buy_price", 0))
            manual_buys = _record_manual_trades(ticker, info["coin_name"], ref_avg_price)

            # 수동 매수가 있었으면 held_coin_record 의 평균가·손절가·피라미딩가도 함께 갱신
            if manual_buys:
                try:
                    indicators = indicator_calc.get_all_indicators(ticker)
                    atr_n = indicators.get("atr", 0.0)
                except Exception:
                    atr_n = 0.0
                _apply_manual_buys_to_record(record[ticker], manual_buys, atr_n)

            msg = (
                f"⚠️ [잔고동기화] {info['coin_name']}({ticker}) 수량 불일치: "
                f"기록 {record_vol:.8f} → 실제 {actual_vol:.8f} 으로 수정."
            )
            if manual_buys:
                msg += (
                    f"\n수동 매수 {len(manual_buys)}건 반영 — "
                    f"평균가 → {record[ticker]['avg_buy_price']:,.0f}원, "
                    f"손절가 → {record[ticker]['stop_loss_price']:,.0f}원"
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
