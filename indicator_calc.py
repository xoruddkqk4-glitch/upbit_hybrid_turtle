# indicator_calc.py
# 기술 지표 계산 모듈
#
# 터틀 트레이딩 전략에 필요한 지표들을 계산한다:
#   - ATR(N): 평균 실제 범위 → Unit 수량 계산, 손절가, 피라미딩 트리거에 사용
#   - 이동평균선(5MA, 20MA): 트레일링 스탑 판단에 사용
#   - 10일 신저가: 트레일링 스탑 판단에 사용
#
# 본 모듈은 upbit_client.get_daily_chart 를 통해
# OHLCV 데이터를 가져온다. 암호화폐는 24시간 연속 거래되므로
# 장중·장외 구분 없이 항상 동일한 지표 계산이 가능하다.
#
# [캐싱] ATR·5MA·20MA·10일 신저가는 일봉 데이터로 계산되므로 하루 1회만 계산하면 충분하다.
# atr_cache.json 에 KST 날짜와 함께 저장해 두고, 같은 날 재실행 시 일봉 API 호출을 생략한다.

import json
import os
from datetime import datetime

import pytz

import upbit_client

# atr_cache.json 저장 경로 (이 파일과 같은 폴더)
_DIR           = os.path.dirname(os.path.abspath(__file__))
ATR_CACHE_FILE = os.path.join(_DIR, "atr_cache.json")

# KST 시간대 (날짜 판단 기준)
_KST = pytz.timezone("Asia/Seoul")


def _load_atr_cache() -> dict:
    """atr_cache.json 을 읽어 반환한다.

    파일이 없거나 내용이 손상된 경우 빈 딕셔너리를 반환한다.
    반환값 구조: { "KRW-BTC": {"date": "2026-04-22", "atr": ..., ...}, ... }
    """
    if not os.path.exists(ATR_CACHE_FILE):
        return {}
    try:
        with open(ATR_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, IOError):
        print("[indicator] atr_cache.json 읽기 오류 → 캐시 무시하고 재계산")
    return {}


def _save_atr_cache(cache: dict):
    """atr_cache.json 에 캐시를 저장한다.

    저장 실패 시 경고 출력 후 계속 진행한다 (캐시 저장 실패가 매매를 막으면 안 됨).
    """
    try:
        with open(ATR_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"[indicator] atr_cache.json 저장 오류: {e}")



def calc_atr(ohlcv_list: list, period: int = 20) -> float:
    """ATR(Average True Range, 평균 실제 범위)을 계산한다.

    ATR 은 코인 가격의 하루 변동폭 평균이다.
    터틀 트레이딩에서 'N' 이라고 부르며, Unit 수량·손절가 설정에 사용한다.

    True Range = max(고가 - 저가, |고가 - 전일종가|, |저가 - 전일종가|)
    ATR = 최근 period 일 True Range 의 평균

    Args:
        ohlcv_list: 날짜 오름차순 정렬된 OHLCV 딕셔너리 리스트
                    [{"open":..., "high":..., "low":..., "close":...}, ...]
                    최소 (period + 1) 개 이상 필요
        period:     ATR 계산 기간 (기본 20일)

    Returns:
        ATR 값 (float). 데이터 부족 시 0.0.
    """
    # 최소 (period + 1) 개: period일 TR 계산에는 period개의 전일종가가 필요
    if len(ohlcv_list) < period + 1:
        print(f"[indicator] ATR 계산 데이터 부족: {len(ohlcv_list)}개 (필요: {period + 1}개 이상)")
        return 0.0

    # 각 날짜의 True Range 계산
    true_ranges = []
    for i in range(1, len(ohlcv_list)):
        high       = ohlcv_list[i]["high"]
        low        = ohlcv_list[i]["low"]
        prev_close = ohlcv_list[i - 1]["close"]

        # 세 가지 중 가장 큰 값이 True Range
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low  - prev_close),
        )
        true_ranges.append(tr)

    # 가장 최근 period개 TR 평균
    recent_trs = true_ranges[-period:]
    return sum(recent_trs) / len(recent_trs)


def calc_ma(close_list: list, period: int) -> float:
    """단순 이동평균선(SMA)을 계산한다.

    Args:
        close_list: 종가 리스트 (오름차순, 최신 값이 마지막)
        period:     이동평균 계산 기간

    Returns:
        period 일 이동평균 값 (float). 데이터 부족 시 0.0.
    """
    if len(close_list) < period:
        print(f"[indicator] MA{period} 계산 데이터 부족: {len(close_list)}개 (필요: {period}개)")
        return 0.0

    return sum(close_list[-period:]) / period


def calc_10day_low(ohlcv_list: list) -> float:
    """최근 10일 중 가장 낮은 종가(10일 신저가)를 계산한다.

    트레일링 스탑 판단에 사용한다. 현재가가 이 값 이하로 떨어지면
    추세가 끝난 것으로 판단해 익절 청산한다.

    Args:
        ohlcv_list: 날짜 오름차순 정렬된 OHLCV 딕셔너리 리스트 (최소 10개)

    Returns:
        최근 10일 최저 종가 (float). 데이터 부족 시 0.0.
    """
    if len(ohlcv_list) < 10:
        print(f"[indicator] 10일 신저가 데이터 부족: {len(ohlcv_list)}개 (필요: 10개 이상)")
        return 0.0

    recent_10 = ohlcv_list[-10:]
    return float(min(item["close"] for item in recent_10))


def calc_n_day_high(ohlcv_list: list, n: int) -> float:
    """최근 N일 장중 고가(high) 중 최고값을 계산한다.

    오리지널 터틀 트레이딩 진입 신호 판단에 사용한다.
      - 시스템1: n=20
      - 시스템2: n=55

    오늘 캔들(마지막 항목)은 제외하고 직전 N일만 본다.
    "오늘 처음 신고가를 돌파했는가" 를 판단하기 위해서다.

    Args:
        ohlcv_list: 날짜 오름차순 정렬된 OHLCV 딕셔너리 리스트
        n:          기간 일 수 (20 또는 55)

    Returns:
        직전 N일 장중 고가 최고값 (float). 데이터 부족 시 0.0.
    """
    # 오늘 + 직전 N일 = 최소 (n + 1) 개 필요
    if len(ohlcv_list) < n + 1:
        print(f"[indicator] {n}일 신고가 계산 데이터 부족: "
              f"{len(ohlcv_list)}개 (필요: {n + 1}개 이상)")
        return 0.0

    # 오늘(마지막) 제외 → 직전 N개 캔들의 high 중 최고값
    prev_n_candles = ohlcv_list[-(n + 1):-1]
    return float(max(d["high"] for d in prev_n_candles))


def get_all_indicators(ticker: str) -> dict:
    """한 코인의 전략에 필요한 모든 지표를 한 번에 계산해서 반환한다.

    ATR·5MA·20MA·10일 신저가는 일봉 데이터로 계산되므로 하루 1회만 API를 호출하고
    atr_cache.json 에 캐싱한다. 같은 날 재호출 시 캐시에서 즉시 반환한다.

    Args:
        ticker: 업비트 티커 (예: "KRW-BTC")

    Returns:
        {
            "atr":       1200000.0,  # ATR(N): Unit 수량·손절가·피라미딩 트리거용
            "ma5":       9400000.0,  # 5일 이동평균 (일봉 종가)
            "ma20":      9300000.0,  # 20일 이동평균 (일봉 종가)
            "day10_low": 9000000.0,  # 10일 신저가
        }
        데이터 부족 또는 오류 시 모든 값이 0 인 딕셔너리.
    """
    default   = {
        "atr": 0.0, "ma5": 0.0, "ma20": 0.0, "day10_low": 0.0,
        "s1_high": 0.0, "s2_high": 0.0,
    }
    today_str = datetime.now(_KST).strftime("%Y-%m-%d")  # 캐시 유효 기준 날짜 (KST)

    try:
        # ── 캐시 확인 ──────────────────────────────────────────────
        # 오늘 날짜 캐시가 있으면 일봉 API 호출을 생략하고 캐시 값을 사용한다
        cache  = _load_atr_cache()
        cached = cache.get(ticker, {})

        if (cached.get("date") == today_str
                and "s1_high" in cached
                and "s2_high" in cached):
            # s1_high/s2_high 필드 없는 구버전 캐시는 캐시 미스로 처리해 재계산
            print(f"[indicator] {ticker} 일봉 캐시 적중 ({today_str}) — 일봉 API 생략")
            return {
                "atr":       cached["atr"],
                "ma5":       cached["ma5"],
                "ma20":      cached["ma20"],
                "day10_low": cached["day10_low"],
                "s1_high":   float(cached.get("s1_high", 0.0)),
                "s2_high":   float(cached.get("s2_high", 0.0)),
            }

        # ── 캐시 미스: 일봉 새로 계산 (저장은 run_daily.py 담당) ────────
        # run_daily.py 가 하루 1회 refresh_atr_cache() 를 호출해 미리 저장해 둔다.
        # 캐시가 없는 상황(초기 설정, run_daily 실패 등)에서도 매매가 멈추지 않도록
        # 여기서는 계산만 하고 저장하지 않는다.
        print(f"[indicator] {ticker} 일봉 캐시 미스 → API 호출 (저장은 run_daily 담당)")

        # 일봉 60개 요청 (20일 ATR + 55일 신고가 + 여유)
        daily = upbit_client.get_daily_chart(ticker, count=60)
        if not daily:
            print(f"[indicator] {ticker} 일봉 데이터 없음")
            return default

        close_list = [d["close"] for d in daily]

        # 일봉 기반 지표 계산
        atr_val       = calc_atr(daily, period=20)
        ma5_val       = calc_ma(close_list, period=5)
        ma20_val      = calc_ma(close_list, period=20)
        day10_low_val = calc_10day_low(daily)
        s1_high_val   = calc_n_day_high(daily, n=20)
        s2_high_val   = calc_n_day_high(daily, n=55)

        return {
            "atr":       atr_val,
            "ma5":       ma5_val,
            "ma20":      ma20_val,
            "day10_low": day10_low_val,
            "s1_high":   s1_high_val,
            "s2_high":   s2_high_val,
        }

    except Exception as e:
        print(f"[indicator] {ticker} 지표 계산 오류: {e}")
        return default


def refresh_atr_cache(tickers: list):
    """감시 코인 전체의 일봉 기반 지표를 계산해 atr_cache.json 에 저장한다.

    run_daily.py 에서 하루 1회 호출한다.
    이 함수가 미리 캐시를 채워 두면, run_all.py 는 하루 종일 일봉 API 호출 없이
    캐시에서 바로 지표를 읽어 쓸 수 있다.

    Args:
        tickers: 캐시를 갱신할 티커 목록 (예: ["KRW-BTC", "KRW-ETH", ...])
    """
    today_str = datetime.now(_KST).strftime("%Y-%m-%d")
    cache     = _load_atr_cache()  # 기존 캐시 유지(다른 코인 데이터 보존)

    print(f"[indicator] ATR 캐시 갱신 시작 — {today_str} / {len(tickers)}개 코인")

    for ticker in tickers:
        try:
            daily = upbit_client.get_daily_chart(ticker, count=60)
            if not daily:
                print(f"[indicator] {ticker} 일봉 데이터 없음 → 캐시 갱신 스킵")
                continue

            close_list = [d["close"] for d in daily]

            cache[ticker] = {
                "date":      today_str,
                "atr":       calc_atr(daily, period=20),
                "ma5":       calc_ma(close_list, period=5),
                "ma20":      calc_ma(close_list, period=20),
                "day10_low": calc_10day_low(daily),
                "s1_high":   calc_n_day_high(daily, n=20),
                "s2_high":   calc_n_day_high(daily, n=55),
            }
            print(f"[indicator] {ticker} ATR 캐시 갱신 완료 "
                  f"(ATR={cache[ticker]['atr']:,.0f})")

        except Exception as e:
            print(f"[indicator] {ticker} ATR 캐시 갱신 오류: {e}")

    _save_atr_cache(cache)
    print(f"[indicator] ATR 캐시 저장 완료 → {ATR_CACHE_FILE}")


def get_n_day_high_signals(ticker: str) -> dict:
    """터틀 시스템1(20일) / 시스템2(55일) 신고가 값을 반환한다.

    target_manager.py 에서 현재가와 비교해 신호 발생 여부를 판단한다.

    Args:
        ticker: 업비트 티커

    Returns:
        {"s1_high": 9800000.0, "s2_high": 9900000.0}
        데이터 부족 시 두 값 모두 0.0.
    """
    try:
        daily = upbit_client.get_daily_chart(ticker, count=60)
        if not daily:
            return {"s1_high": 0.0, "s2_high": 0.0}

        return {
            "s1_high": calc_n_day_high(daily, n=20),
            "s2_high": calc_n_day_high(daily, n=55),
        }
    except Exception as e:
        print(f"[indicator] {ticker} 신고가 계산 오류: {e}")
        return {"s1_high": 0.0, "s2_high": 0.0}


def prefetch_indicators(tickers: list) -> dict:
    """티커 목록의 지표를 일괄 계산해 반환한다.

    run_all 에서 단계별 중복 API 호출을 줄이기 위한 프리페치 레이어.
    내부적으로 get_all_indicators() 의 캐시(일봉/240분 TTL)를 그대로 활용한다.
    """
    result = {}
    if not tickers:
        return result

    unique = list(dict.fromkeys([t for t in tickers if t]))
    print(f"[indicator] 지표 프리페치 시작 — {len(unique)}개 티커")

    for ticker in unique:
        try:
            result[ticker] = get_all_indicators(ticker)
        except Exception as e:
            print(f"[indicator] {ticker} 프리페치 오류: {e}")
            result[ticker] = {
                "atr": 0.0, "ma5": 0.0, "ma20": 0.0, "day10_low": 0.0,
                "s1_high": 0.0, "s2_high": 0.0,
            }

    print("[indicator] 지표 프리페치 완료")
    return result
