# config.py
# 감시 코인 목록 정의 모듈
#
# 업비트 하이브리드 터틀 전략은 "고정 관심 코인 리스트(lovely_coin_list)"만 감시한다.
# 진입·감시·주문 대상은 이 목록 안에 있는 티커만으로 한정하며,
# 리스트 밖 코인은 주문·상태 변경을 하지 않는다.
#
# 목록을 바꾸고 싶으면 아래 LOVELY_COIN_LIST 를 직접 편집하라.
# 자동 스크리너(동적 종목 선정) 는 본 프로젝트에서 제공하지 않는다.
#
# 사용법:
#   from config import get_watchlist, get_coin_name
#   watchlist = get_watchlist()
#   if ticker in watchlist: ...
#   name = get_coin_name(ticker)

from __future__ import annotations


# ─────────────────────────────────────────
# 관심 코인 고정 목록 (lovely_coin_list)
# ─────────────────────────────────────────
#
# 형식:
#   "KRW-BTC": {"name": "비트코인", "market": "KRW"}
#
# 필드 설명:
#   - name   : 로그/알림에 보여줄 한글 이름
#   - market : 거래 시장 (현재 Upbit 원화 시장 'KRW' 만 지원)
#
# ※ 코인은 상장/폐지가 잦으므로 사용 전에 실제 업비트 거래 페이지에서
#   해당 티커가 거래 가능한지 확인하는 것을 권장한다.

LOVELY_COIN_LIST: dict = {
    "KRW-ADA":   {"name": "에이다",      "market": "KRW"},
    "KRW-ALGO":  {"name": "알고랜드",    "market": "KRW"},
    "KRW-BTC":   {"name": "비트코인",    "market": "KRW"},
    "KRW-DOGE":  {"name": "도지코인",    "market": "KRW"},
    "KRW-ETH":   {"name": "이더리움",    "market": "KRW"},
    "KRW-HBAR":   {"name": "헤데라",    "market": "KRW"},
    "KRW-LINK":   {"name": "체인링크",    "market": "KRW"},
    "KRW-SOL":   {"name": "솔라나",      "market": "KRW"},
    "KRW-SUI":   {"name": "수이",      "market": "KRW"},
    "KRW-XLM":   {"name": "스텔라루멘",  "market": "KRW"},
    "KRW-XRP":   {"name": "리플",        "market": "KRW"},
}


def get_watchlist() -> dict:
    """감시 코인 딕셔너리를 반환한다.

    반환 형식:
        {"KRW-BTC": {"name": "비트코인", "market": "KRW"}, ...}

    상위 모듈은 이 결과의 key(티커)를 기준으로 진입·감시·주문 대상 여부를 판단한다.
    """
    # 매번 새 dict 를 반환해서 호출자가 수정해도 원본이 훼손되지 않도록 한다
    return dict(LOVELY_COIN_LIST)


def get_coin_name(ticker: str) -> str:
    """티커로 코인 한글명을 반환한다. 감시 목록에 없으면 티커를 그대로 반환한다."""
    return LOVELY_COIN_LIST.get(ticker, {}).get("name", ticker)


def get_coin_symbol(ticker: str) -> str:
    """티커에서 코인 심볼만 추출한다 ("KRW-BTC" → "BTC")."""
    if "-" in ticker:
        return ticker.split("-", 1)[1]
    return ticker
