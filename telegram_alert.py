# telegram_alert.py
# 텔레그램 알림 단일 모듈
#
# 모든 알림은 이 모듈의 SendMessage() 를 통해서만 발송한다.
# .env 파일에 TELEGRAM_BOT_TOKEN 과 TELEGRAM_CHAT_ID 가 없으면
# 콘솔(화면)에만 출력하고 넘어간다.
#
# 사용법:
#   from telegram_alert import SendMessage
#   SendMessage("비트코인 진입 완료: 0.001 @95,000,000원")

import os

import requests
from dotenv import load_dotenv

# 프로젝트 폴더의 .env 를 명시적으로 로드 — crontab 의 cwd 가 달라도 안전
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def SendMessage(msg: str) -> bool:
    """텔레그램 봇으로 메시지를 발송한다.

    발송 실패 시 1회 재시도 후 포기한다 (프로그램이 멈추지 않음).
    토큰/채팅ID 가 없으면 화면에 메시지를 출력하고 False 를 반환한다.

    Args:
        msg: 보낼 메시지 내용 (문자열)

    Returns:
        True:  발송 성공
        False: 발송 실패 또는 설정 없음
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id   = os.getenv("TELEGRAM_CHAT_ID",   "").strip()

    # 설정이 없으면 화면 출력으로 대체
    if not bot_token or not chat_id:
        print(f"[텔레그램 설정 없음] {msg}")
        return False

    url     = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}

    for attempt in range(1, 3):
        try:
            response = requests.post(url, json=payload, timeout=5)
            if response.status_code == 200:
                return True
            print(f"[텔레그램] 발송 실패 ({attempt}/2회): HTTP {response.status_code}")
        except requests.RequestException as e:
            print(f"[텔레그램] 발송 오류 ({attempt}/2회): {e}")

    print(f"[텔레그램] 최종 실패, 메시지 손실: {msg}")
    return False
