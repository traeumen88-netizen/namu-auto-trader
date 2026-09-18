"""
scripts/setup_kakao.py
카카오톡 1회 로그인 인증 및 토큰 자동 발급 도우미 스크립트
"""

import sys
import os
import json
import webbrowser
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# UTF-8 출력 보장
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.kakao_notifier import KakaoNotifier

REST_API_KEY = "41e26d3c23a0a7daf8860c7ea00db4d6"
CLIENT_SECRET = "lCMJH6PhSSVS4n2Mu5TL5PnQDinYi1V0"  # 보안 시크릿 키
REDIRECT_URI = "http://localhost:5000/oauth"
AUTH_URL = f"https://kauth.kakao.com/oauth/authorize?client_id={REST_API_KEY}&redirect_uri={REDIRECT_URI}&response_type=code&scope=talk_message"

auth_code_received = None

class OAuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code_received
        parsed = urlparse(self.path)
        if parsed.path == "/oauth":
            qs = parse_qs(parsed.query)
            if "code" in qs:
                auth_code_received = qs["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                html = """
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="utf-8">
                    <title>카카오톡 연동 성공</title>
                    <style>
                        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f7f7f7; }
                        .card { background: white; padding: 40px; border-radius: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); text-align: center; max-width: 420px; }
                        h2 { color: #3c1e1e; margin-bottom: 12px; }
                        p { color: #555; line-height: 1.6; font-size: 15px; }
                    </style>
                </head>
                <body>
                    <div class="card">
                        <div style="font-size: 52px; margin-bottom: 16px;">🎉</div>
                        <h2>카카오톡 연동 성공!</h2>
                        <p>카카오톡 <b>[나와의 채팅]</b>으로 테스트 메시지가 발송되었습니다.<br>이 브라우저 창은 이제 닫으셔도 됩니다.</p>
                    </div>
                </body>
                </html>
                """
                self.wfile.write(html.encode("utf-8"))
            elif "error" in qs:
                err = qs.get("error_description", ["인증 취소"])[0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"<h2>인증 오류: {err}</h2>".encode("utf-8"))

    def log_message(self, format, *args):
        pass

def exchange_code_for_token(code, client_secret=None):
    url = "https://kauth.kakao.com/oauth/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"
    }
    data = {
        "grant_type": "authorization_code",
        "client_id": REST_API_KEY,
        "redirect_uri": REDIRECT_URI,
        "code": code
    }
    if client_secret:
        data["client_secret"] = client_secret

    resp = requests.post(url, headers=headers, data=data, timeout=10)
    if resp.status_code == 200:
        return resp.json()
    else:
        print(f"[ERROR] 토큰 교환 실패 ({resp.status_code}): {resp.text}")
        return None

def run_wizard(client_secret=None):
    global auth_code_received
    auth_code_received = None

    print("=" * 60)
    print("   [나무증권 AI 퀀트] 카카오톡 실시간 알림 1회 연동 마법사")
    print("=" * 60)
    print(f"- 등록된 REST API 키: {REST_API_KEY}")
    print(f"- 콜백 URL: {REDIRECT_URI}\n")
    print("잠시 후 브라우저가 열리면 카카오 계정으로 로그인 후 [동의하고 계속하기]를 눌러주세요.")
    print("브라우저가 열리지 않으면 아래 링크를 직접 클릭하세요:")
    print(AUTH_URL)
    print("-" * 60)

    server = HTTPServer(("localhost", 5000), OAuthHandler)
    server.timeout = 300  # 5분 대기

    try:
        webbrowser.open(AUTH_URL)
    except Exception as e:
        print(f"[WARN] 브라우저 자동 실행 실패: {e}")

    while not auth_code_received:
        server.handle_request()

    if auth_code_received:
        print("\n[OK] 인증 코드 수신 성공! 카카오 서버와 토큰 교환 중...")
        tokens = exchange_code_for_token(auth_code_received, client_secret)
        if tokens:
            if client_secret:
                tokens["client_secret"] = client_secret
            notifier = KakaoNotifier(REST_API_KEY)
            notifier._save_tokens(tokens)
            print("[OK] 토큰 저장 완료 (`config/kakao_token.json`)")
            print("[INFO] 테스트 카카오톡 메시지를 발송합니다...")
            test_msg = (
                "🚀 [나무증권 AI 퀀트] 카카오톡 실시간 알림 연동 완료!\n\n"
                "앞으로 장중 체결 알림과 모바일 대시보드 링크가 이 채팅방으로 자동 전송됩니다."
            )
            if notifier.send_text(test_msg):
                print("\n[SUCCESS] 축하합니다! 카카오톡 [나와의 채팅]을 확인해보세요.")
                return True
            else:
                print("[WARN] 토큰 발급은 성공했으나 메시지 전송 실패. (동의항목 '카카오톡 메시지 전송' 권한 확인 필요)")
                return False
        else:
            print("[ERROR] 토큰 교환에 실패했습니다.")
            return False

if __name__ == "__main__":
    secret = sys.argv[1] if len(sys.argv) > 1 else CLIENT_SECRET
    run_wizard(secret)
