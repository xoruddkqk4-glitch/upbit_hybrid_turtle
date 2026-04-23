# upbit_client.py
# Upbit API 전담 래퍼 모듈
#
# 규칙: 다른 전략 파일들은 Upbit API(pyupbit)를 직접 호출하지 말고,
#       반드시 이 파일(upbit_client.py)의 함수를 통해서만 접근한다.
#
# 참고: 지표 계산(GetMA, GetRSI 등)이나 시장가 매수/매도 저수준 로직은
#       myUpbit.py 를 그대로 재사용한다 (본 파일은 myUpbit 의 기능을
#       전략 모듈이 쉽게 쓸 수 있도록 한번 더 감싼 얇은 래퍼이다).
#
# 사용법:
#   import upbit_client
#   upbit_client.login()
#   prices = upbit_client.get_multi_price(["KRW-BTC", "KRW-ETH"])

import os
import time

import pyupbit
from dotenv import load_dotenv

import myUpbit  # pyupbit 기반 저수준 함수 모음 (GetMA, BuyCoinMarket, SellCoinMarket 등)

# 프로젝트 폴더의 .env 를 명시적으로 로드 — crontab 의 cwd 가 달라도 안전
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# 모듈 내부에서 사용하는 Upbit 인스턴스
_upbit = None


# ─────────────────────────────────────────
# 로그인
# ─────────────────────────────────────────

def login() -> bool:
    """Upbit Open API 에 실계좌로 로그인한다.

    Returns:
        True:  로그인 성공
        False: API 키가 없거나 잘못된 경우
    """
    global _upbit

    access_key = os.getenv("UPBIT_ACCESS_KEY", "").strip()
    secret_key = os.getenv("UPBIT_SECRET_KEY", "").strip()

    if not access_key or not secret_key:
        print("[upbit_client] 오류: .env 에 UPBIT_ACCESS_KEY/UPBIT_SECRET_KEY 가 없습니다.")
        return False

    try:
        _upbit = pyupbit.Upbit(access_key, secret_key)
        # 간단한 연결 테스트: 잔고를 한번 조회해본다
        _ = _upbit.get_balances()
        print("[upbit_client] 로그인 성공 (실계좌 모드)")
        return True
    except Exception as e:
        print(f"[upbit_client] 로그인 오류: {e}")
        return False


def _check_login():
    """로그인 여부를 확인하고 로그인되지 않았으면 예외를 발생시킨다."""
    if _upbit is None:
        raise RuntimeError("[upbit_client] 로그인 먼저 하세요: upbit_client.login()")


def get_upbit():
    """내부 Upbit 객체를 반환한다 (myUpbit 함수에 전달할 때 사용)."""
    return _upbit


# ─────────────────────────────────────────
# 시세 조회
# ─────────────────────────────────────────

def get_multi_price(tickers: list) -> dict:
    """여러 코인의 현재가를 한번에 조회한다.

    pyupbit.get_current_price 는 리스트를 받으면 내부적으로 한 번의 요청으로
    여러 종목을 가져올 수 있다.

    Args:
        tickers: 업비트 티커 리스트 (예: ["KRW-BTC", "KRW-ETH", "KRW-XRP"])

    Returns:
        티커 → 현재가 딕셔너리 (예: {"KRW-BTC": 95000000.0, "KRW-ETH": 5000000.0})
        조회 실패 종목은 결과에서 제외된다.
    """
    if not tickers:
        return {}

    try:
        time.sleep(0.1)
        raw = pyupbit.get_current_price(tickers)
        if raw is None:
            return {}

        # pyupbit 은 티커가 1개면 float, 여러 개면 dict 를 반환한다
        if isinstance(raw, (int, float)):
            return {tickers[0]: float(raw)}

        # dict 케이스: None 인 항목은 제외
        result = {}
        for t, p in raw.items():
            if p is not None:
                result[t] = float(p)
        return result

    except Exception as e:
        print(f"[upbit_client] 다중 현재가 조회 오류: {e}")
        return {}


# ─────────────────────────────────────────
# 차트 조회 (pyupbit.get_ohlcv 래퍼)
# ─────────────────────────────────────────

def get_daily_chart(ticker: str, count: int = 25) -> list:
    """일봉 OHLCV 데이터를 조회한다.

    ATR(N), 이동평균선(5MA/20MA), 10일 신저가 계산에 사용한다.
    최소 21개(20일 ATR + 1일 여유) 이상 요청을 권장한다.

    Args:
        ticker: 업비트 티커 (예: "KRW-BTC")
        count:  요청 건수 (기본 25)

    Returns:
        날짜 오름차순(오래된 것 먼저) 정렬된 OHLCV 딕셔너리 리스트
        [{"date": "20260413", "open": 9.5e7, "high": 9.6e7,
          "low": 9.3e7, "close": 9.55e7, "volume": 123.45}, ...]
        조회 실패 시 빈 리스트.
    """
    _MAX_RETRIES = 3
    _RETRY_WAIT  = 5.0

    for attempt in range(_MAX_RETRIES):
        try:
            time.sleep(0.1)
            df = pyupbit.get_ohlcv(ticker, interval="day", count=count)
            if df is None or df.empty:
                print(f"[upbit_client] 일봉 데이터 없음: {ticker}")
                return []

            result = []
            for idx, row in df.iterrows():
                result.append({
                    "date":   idx.strftime("%Y%m%d"),
                    "open":   float(row["open"]),
                    "high":   float(row["high"]),
                    "low":    float(row["low"]),
                    "close":  float(row["close"]),
                    "volume": float(row["volume"]),
                })
            return result

        except Exception as e:
            err_str = str(e)
            if attempt < _MAX_RETRIES - 1:
                print(f"[upbit_client] 일봉 차트 조회 재시도 ({ticker}, {attempt + 1}/{_MAX_RETRIES}): {err_str}")
                time.sleep(_RETRY_WAIT)
            else:
                print(f"[upbit_client] 일봉 차트 조회 오류 ({ticker}): {e}")
                return []

    return []


def get_minute_chart(ticker: str, minute: int = 240, count: int = 25) -> list:
    """분봉 OHLCV 데이터를 조회한다.

    240분봉(4시간봉) 20MA 계산에 사용한다. 기본값은 240분봉.

    Args:
        ticker: 업비트 티커 (예: "KRW-BTC")
        minute: 분봉 단위 (1, 3, 5, 15, 30, 60, 240 중 하나; 기본 240)
        count:  요청 건수 (기본 25)

    Returns:
        시각 오름차순 정렬된 OHLCV 딕셔너리 리스트
        [{"date": "20260413", "time": "160000", "open": ..., "high": ...,
          "low": ..., "close": ..., "volume": ...}, ...]
        조회 실패 시 빈 리스트.
    """
    _MAX_RETRIES = 3
    _RETRY_WAIT  = 5.0

    # pyupbit interval 문자열 변환 (minute240 등)
    interval = f"minute{minute}"

    for attempt in range(_MAX_RETRIES):
        try:
            time.sleep(0.1)
            df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
            if df is None or df.empty:
                print(f"[upbit_client] 분봉 데이터 없음: {ticker} ({interval})")
                return []

            result = []
            for idx, row in df.iterrows():
                result.append({
                    "date":   idx.strftime("%Y%m%d"),
                    "time":   idx.strftime("%H%M%S"),
                    "open":   float(row["open"]),
                    "high":   float(row["high"]),
                    "low":    float(row["low"]),
                    "close":  float(row["close"]),
                    "volume": float(row["volume"]),
                })
            return result

        except Exception as e:
            err_str = str(e)
            if attempt < _MAX_RETRIES - 1:
                print(f"[upbit_client] 분봉 차트 조회 재시도 "
                      f"({ticker}, {minute}분, {attempt + 1}/{_MAX_RETRIES}): {err_str}")
                time.sleep(_RETRY_WAIT)
            else:
                print(f"[upbit_client] 분봉 차트 조회 오류 ({ticker}, {minute}분): {e}")
                return []

    return []


# ─────────────────────────────────────────
# 계좌 조회
# ─────────────────────────────────────────

def _sanitize_balances(raw) -> list:
    """Upbit API 원본 응답을 표준 list[dict] 로 정제한다.

    pyupbit.get_balances 는 정상 시 list[dict] 를 반환하지만, 인증 실패나
    Rate Limit 초과 시 문자열/에러 dict 를 반환할 수 있어 이후
    `value.get(...)` 접근에서 `'str' object has no attribute 'get'`
    에러가 발생한다. 이 함수는 이를 한 지점에서 차단한다.
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        # {"error": {...}} 같은 에러 응답
        if "error" in raw:
            err = raw.get("error", {})
            if isinstance(err, dict):
                print(f"[upbit_client] 잔고 API 에러: {err.get('message', err)}")
            else:
                print(f"[upbit_client] 잔고 API 에러: {err}")
        else:
            print(f"[upbit_client] 잔고 응답이 list 가 아님(dict): {raw}")
        return []
    if not isinstance(raw, list):
        print(f"[upbit_client] 잔고 응답이 list 가 아님({type(raw).__name__}): {raw}")
        return []
    return [v for v in raw if isinstance(v, dict)]


def _get_raw_balances() -> list:
    """pyupbit 원본 balances 리스트를 반환한다 (내부용).

    어떤 경로에서도 list[dict] 이외의 값은 반환하지 않는다.
    """
    _check_login()
    if _upbit is None:
        return []
    try:
        time.sleep(0.1)
        raw = _upbit.get_balances()
        return _sanitize_balances(raw)
    except Exception as e:
        print(f"[upbit_client] 잔고 원본 조회 오류: {e}")
        return []


def get_total_capital() -> float:
    """계좌의 총 자본(추정순자산)을 조회한다.

    터틀 트레이딩의 Unit 수량 계산 시 '총 자본' 값으로 사용한다.
    총 자본 = 현금(KRW) + 보유 코인 현재가 평가금액 합계

    Returns:
        총 자본 (원, float). 조회 실패 시 0.0.
    """
    balances = _get_raw_balances()
    if not balances:
        return 0.0

    try:
        # myUpbit.GetTotalRealMoney: KRW + 코인별 (현재가 × 수량) 합계
        return float(myUpbit.GetTotalRealMoney(balances))
    except Exception as e:
        print(f"[upbit_client] 총자본 계산 오류: {e}")
        return 0.0


def get_krw_balance() -> float:
    """사용 가능한 KRW 현금 잔고를 반환한다 (locked 제외)."""
    balances = _get_raw_balances()
    for v in balances:
        if not isinstance(v, dict):
            continue
        try:
            if v.get("currency") == "KRW":
                return float(v.get("balance", 0))
        except Exception:
            pass
    return 0.0


def get_balance() -> list:
    """보유 코인별 잔고를 조회한다.

    risk_guardian.py 에서 보유 코인 현황 확인에 사용한다.
    KRW(현금)는 제외한다.

    Returns:
        [
            {
                "ticker":        "KRW-BTC",
                "coin_name":     "BTC",
                "volume":         0.01234,    # 보유 수량 (balance + locked)
                "avg_price":     95000000.0,  # 평균 매입단가
                "current_price": 96000000.0,  # 현재가
                "sellable_qty":   0.01234,    # 매도 가능 수량 (balance 만)
            },
            ...
        ]
        조회 실패 또는 보유 코인 없으면 빈 리스트.
    """
    balances = _get_raw_balances()
    if not balances:
        return []

    result = []
    for v in balances:
        if not isinstance(v, dict):
            continue
        try:
            currency = v.get("currency", "")
            unit     = v.get("unit_currency", "KRW")

            # KRW 자체는 제외
            if currency == "KRW":
                continue

            avg_buy_price = float(v.get("avg_buy_price", 0))
            balance       = float(v.get("balance", 0))
            locked        = float(v.get("locked", 0))
            qty           = balance + locked

            # 평균매입단가 0(드랍 받은 코인 등) 또는 수량 0은 제외
            if avg_buy_price <= 0 or qty <= 0:
                continue

            ticker = f"{unit}-{currency}"

            # 현재가 조회 (1종목씩)
            time.sleep(0.05)
            try:
                current_price = float(pyupbit.get_current_price(ticker) or 0.0)
            except Exception:
                current_price = 0.0

            result.append({
                "ticker":        ticker,
                "coin_name":     currency,
                "volume":        qty,
                "avg_price":     avg_buy_price,
                "current_price": current_price,
                "sellable_qty":  balance,   # locked(거래 대기) 제외한 매도 가능 수량
            })
        except Exception as e:
            print(f"[upbit_client] 잔고 파싱 오류: {e}")

    return result


def get_portfolio_summary() -> dict:
    """포트폴리오 전체 요약 정보를 조회한다.

    trade_ledger.record_portfolio_snapshot 용으로 제공한다.

    Returns:
        {
            "total_capital":    총자본 (KRW + 코인평가, 원),
            "coin_value":       코인평가액 (원),
            "cash":             원화 현금 (원),
            "purchase_amount":  매입금액 (평균단가 × 수량 합계, 원),
            "unrealized_pnl":   평가손익 (원),
            "realized_pnl":     실현손익 (Upbit API 미제공 → 0),
            "holdings_count":   보유 코인 수,
            "holdings_names":   보유 코인명 (수익률 포함, 쉼표 구분),
        }
        조회 실패 시 빈 딕셔너리.
    """
    balances = _get_raw_balances()
    if not balances:
        return {}

    try:
        # 현금(KRW) 잔고
        cash = 0.0
        for v in balances:
            if not isinstance(v, dict):
                continue
            if v.get("currency") == "KRW":
                cash = float(v.get("balance", 0)) + float(v.get("locked", 0))
                break

        purchase_amount = 0.0
        coin_value      = 0.0
        names = []
        holdings_count = 0

        for v in balances:
            if not isinstance(v, dict):
                continue
            try:
                currency = v.get("currency", "")
                if currency == "KRW":
                    continue

                avg_buy_price = float(v.get("avg_buy_price", 0))
                qty           = float(v.get("balance", 0)) + float(v.get("locked", 0))
                if avg_buy_price <= 0 or qty <= 0:
                    continue

                unit   = v.get("unit_currency", "KRW")
                ticker = f"{unit}-{currency}"

                time.sleep(0.05)
                try:
                    now_price = float(pyupbit.get_current_price(ticker) or 0.0)
                except Exception:
                    now_price = 0.0

                purchase_amount += avg_buy_price * qty
                coin_value      += now_price * qty
                holdings_count  += 1

                if avg_buy_price > 0 and now_price > 0:
                    rate = (now_price - avg_buy_price) * 100.0 / avg_buy_price
                    names.append(f"{currency}({rate:+.2f}%)")
                else:
                    names.append(currency)
            except Exception:
                pass

        total_capital = cash + coin_value
        unrealized_pnl = coin_value - purchase_amount

        return {
            "total_capital":   int(total_capital),
            "coin_value":      int(coin_value),
            "cash":            int(cash),
            "purchase_amount": int(purchase_amount),
            "unrealized_pnl":  int(unrealized_pnl),
            "realized_pnl":    0,                       # Upbit API 미제공
            "holdings_count":  holdings_count,
            "holdings_names":  ", ".join(names),
        }

    except Exception as e:
        print(f"[upbit_client] 포트폴리오 요약 조회 오류: {e}")
        return {}


# ─────────────────────────────────────────
# 주문
# ─────────────────────────────────────────

def place_order(ticker: str, volume: float, side: str,
                order_type: str = "MARKET",
                krw_amount: float = 0.0) -> dict:
    """매수 또는 매도 주문을 실행한다.

    - 매수(BUY): krw_amount 원으로 시장가 매수 (volume 은 참고값)
    - 매도(SELL): volume 코인 시장가 매도

    Args:
        ticker:      업비트 티커 (예: "KRW-BTC")
        volume:      수량 (코인 개수, 매도 시 필수)
        side:        "BUY" 또는 "SELL"
        order_type:  "MARKET" (현재 시장가만 지원)
        krw_amount:  매수 시 사용할 원화 (volume × 예상가로 자동 계산됨)

    Returns:
        {"success": True,  "order_no": "...", "message": "...",
         "executed_volume": float, "executed_price": float}
        {"success": False, "order_no": "",    "message": "오류 메시지",
         "executed_volume": 0,     "executed_price": 0}
    """
    _check_login()

    # 안전장치: 수량이 0 이하이거나, 매수인데 금액이 0 이하인 경우 차단
    if side == "BUY" and krw_amount <= 0:
        return {
            "success": False, "order_no": "",
            "message": "매수 금액(krw_amount)이 0 이하입니다.",
            "executed_volume": 0, "executed_price": 0,
        }
    if side == "SELL" and volume <= 0:
        return {
            "success": False, "order_no": "",
            "message": "매도 수량(volume)이 0 이하입니다.",
            "executed_volume": 0, "executed_price": 0,
        }

    # pyupbit 으로 시장가 주문
    if _upbit is None:
        return {
            "success": False, "order_no": "",
            "message": "Upbit 인스턴스 없음(로그인 재시도 필요)",
            "executed_volume": 0, "executed_price": 0,
        }

    try:
        if side == "BUY":
            # myUpbit.BuyCoinMarket 는 잔고까지 반환하지만 주문 정보를 직접 반환하지 않음
            # 여기서는 upbit.buy_market_order 를 직접 호출해서 주문 응답을 받는다
            time.sleep(0.05)
            resp = _upbit.buy_market_order(ticker, krw_amount)
            time.sleep(2.0)
        else:
            time.sleep(0.05)
            resp = _upbit.sell_market_order(ticker, volume)
            time.sleep(2.0)

        # 응답 형식 예시: {"uuid": "...", "side": "bid", ...}
        if not isinstance(resp, dict):
            return {
                "success": False, "order_no": "",
                "message": f"알 수 없는 응답: {resp}",
                "executed_volume": 0, "executed_price": 0,
            }

        # 업비트가 에러를 반환한 경우 {"error": {...}} 형식
        if "error" in resp:
            err_msg = resp.get("error", {}).get("message", str(resp))
            print(f"[upbit_client] 주문 실패 ({ticker} {side}): {err_msg}")
            return {
                "success": False, "order_no": "",
                "message": err_msg,
                "executed_volume": 0, "executed_price": 0,
            }

        order_no = resp.get("uuid", "")

        # 체결가·체결수량은 주문 직후에는 빈 값일 수 있으므로 현재가로 근사
        try:
            exec_price = float(pyupbit.get_current_price(ticker) or 0.0)
        except Exception:
            exec_price = 0.0

        if side == "BUY":
            exec_vol = (krw_amount / exec_price) if exec_price > 0 else 0.0
        else:
            exec_vol = volume

        # Upbit API 응답에 포함된 실제 납부 수수료 추출
        paid_fee = float(resp.get("paid_fee", 0) or 0)

        print(f"[upbit_client] 실계좌 {side} 주문 성공: "
              f"{ticker} 수량 {exec_vol:.8f} @{exec_price:,.0f}원 "
              f"수수료 {paid_fee:.2f}원 (uuid={order_no})")
        return {
            "success": True,
            "order_no": order_no,
            "message": "주문 접수 완료",
            "executed_volume": exec_vol,
            "executed_price": exec_price,
            "paid_fee": paid_fee,
        }

    except Exception as e:
        print(f"[upbit_client] 주문 오류 ({ticker} {side}): {e}")
        return {
            "success": False, "order_no": "",
            "message": str(e),
            "executed_volume": 0, "executed_price": 0,
        }
