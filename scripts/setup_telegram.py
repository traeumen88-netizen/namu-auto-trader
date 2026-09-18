"""
scripts/setup_telegram.py
텔레그램 봇 토큰 및 Chat ID 원클릭 연동 및 테스트 발송 스크립트
"""

import sys
import time
import requests
from core.telegram_notifier import TelegramNotifier

DEFAULT_TOKEN = "8704110151:AAHibUhbqXraZMEeeA8Mj9Oe_EKTtKDNOF8"

def main():
    print("=" * 65)
    print("✈️ [나무증권 AI 퀀트] 텔레그램 알림 원클릭 자동 설정")
    print("=" * 65)

    token = DEFAULT_TOKEN
    print(f"• 봇 토큰: {token[:10]}...{token[-5:]}")

    # 1. 봇 정보 확인
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        bot_info = r.json()
        if not bot_info.get("ok"):
            print(f"❌ 봇 토큰이 올바르지 않습니다: {bot_info}")
            return
        bot_user = bot_info["result"]["username"]
        bot_name = bot_info["result"]["first_name"]
        print(f"✅ 봇 연결 확인 성공! [이름: {bot_name} (@{bot_user})]")
    except Exception as e:
        print(f"❌ 텔레그램 서버 통신 실패: {e}")
        return

    print("-" * 65)
    print(f"👉 스마트폰 텔레그램에서 아래 봇을 열고 [시작] 또는 아무 메시지를 전송하세요:")
    print(f"   링크: https://t.me/{bot_user}")
    print(f"   또는 검색창에: @{bot_user}")
    print("-" * 65)
    print("⏳ 사용자님의 연결(Start) 신호를 대기하고 있습니다...")

    chat_id = None
    user_display = None

    for attempt in range(60):
        try:
            r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
            data = r.json()
            if data.get("ok") and data.get("result"):
                # 가장 최신 메시지에서 chat_id 추출
                for item in reversed(data["result"]):
                    msg = item.get("message") or item.get("edited_message") or item.get("channel_post")
                    if msg and "chat" in msg:
                        chat_id = msg["chat"]["id"]
                        user_display = msg["chat"].get("first_name", "") or msg["chat"].get("username", "사용자")
                        break
                if chat_id:
                    break
        except Exception:
            pass
        time.sleep(2)

    if not chat_id:
        print("
⚠️ 2분 동안 [시작] 신호가 감지되지 않았습니다.")
        print(f"스마트폰 텔레그램에서 @{bot_user} 로 들어가 [시작]을 누른 후 다시 실행해 주세요.")
        return

    print(f"
🎉 연결 성공! 사용자님 감지됨: {user_display} (Chat ID: {chat_id})")

    # 2. 설정 저장
    notifier = TelegramNotifier()
    notifier.save_config(token=token, chat_id=chat_id, enabled=True)
    print("💾 설정 파일 저장 완료 (`config/telegram_config.json`)")

    # 3. 테스트 알림 발송
    print("
📩 스마트폰으로 환영 테스트 메시지를 발송합니다...")
    welcome_text = (
        f"🎉 <b>[나무증권 AI 퀀트] 텔레그램 알림 연동 성공!</b>

"
        f"반갑습니다, <b>{user_display}</b>님!
"
        f"이제부터 모든 실시간 매수/매도 체결 알림과 모바일 대시보드 링크가 이 채팅방으로 안전하고 빠르게 전송됩니다.

"
        f"• <b>봇 이름</b>: {bot_name} (@{bot_user})
"
        f"• <b>토큰 상태</b>: 평생 영구 불변 (재로그인 필요 없음)
"
        f"• <b>연결 일시</b>: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    sent = notifier.send_message(welcome_text)
    if sent:
        print("✅ [전송 성공] 스마트폰 텔레그램을 확인해 보세요!")
    else:
        print("⚠️ 테스트 메시지 전송 실패. 네트워크 상태를 확인하세요.")

if __name__ == "__main__":
    main()
