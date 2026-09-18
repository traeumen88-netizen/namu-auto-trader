"""
core/kakao_notifier.py
KakaoTalk '나에게 보내기' API를 통한 실시간 알림 발송 모듈
"""

import os
import json
import logging
import requests
from datetime import datetime

logger = logging.getLogger("KakaoNotifier")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: [KAKAO] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

class KakaoNotifier:
    def __init__(self, rest_api_key="41e26d3c23a0a7daf8860c7ea00db4d6", client_secret="lCMJH6PhSSVS4n2Mu5TL5PnQDinYi1V0"):
        self.rest_api_key = rest_api_key
        self.client_secret = client_secret
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.token_file = os.path.join(self.base_dir, "config", "kakao_token.json")
        self.enabled = False  # 텔레그램으로 전면 교체되어 카카오톡 발송 비활성화
        self.tokens = self._load_tokens()

    def _load_tokens(self):
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"토큰 파일 로드 실패: {e}")
        return None

    def _save_tokens(self, tokens):
        os.makedirs(os.path.dirname(self.token_file), exist_ok=True)
        with open(self.token_file, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2, ensure_ascii=False)
        self.tokens = tokens
        logger.info("카카오 토큰 갱신 및 저장 완료")

    def refresh_token(self):
        """리프레시 토큰을 사용하여 엑세스 토큰 자동 갱신"""
        if not self.tokens or "refresh_token" not in self.tokens:
            logger.warning("리프레시 토큰이 없습니다. setup_kakao.py를 통해 재인증 필요.")
            return False

        url = "https://kauth.kakao.com/oauth/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
        data = {
            "grant_type": "refresh_token",
            "client_id": self.rest_api_key,
            "refresh_token": self.tokens["refresh_token"]
        }
        if "client_secret" in self.tokens:
            data["client_secret"] = self.tokens["client_secret"]
        try:
            resp = requests.post(url, headers=headers, data=data, timeout=10)
            if resp.status_code == 200:
                new_data = resp.json()
                self.tokens["access_token"] = new_data["access_token"]
                if "refresh_token" in new_data:
                    self.tokens["refresh_token"] = new_data["refresh_token"]
                self._save_tokens(self.tokens)
                return True
            else:
                logger.error(f"토큰 갱신 실패 ({resp.status_code}): {resp.text}")
                return False
        except Exception as e:
            logger.error(f"토큰 갱신 요청 에러: {e}")
            return False

    def send_text(self, text, link_url="http://localhost:8080", button_title="대시보드 바로가기"):
        """나에게 텍스트 메시지 발송 (비활성화됨)"""
        if not getattr(self, "enabled", False):
            return False
        if not self.tokens or "access_token" not in self.tokens:
            logger.warning("카카오 토큰이 없습니다. setup_kakao.py를 먼저 실행하세요.")
            return False

        send_url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
        headers = {
            "Authorization": f"Bearer {self.tokens['access_token']}"
        }
        payload = {
            "template_object": json.dumps({
                "object_type": "text",
                "text": text,
                "link": {
                    "web_url": link_url,
                    "mobile_web_url": link_url
                },
                "button_title": button_title
            }, ensure_ascii=False)
        }

        try:
            resp = requests.post(send_url, headers=headers, data=payload, timeout=10)
            if resp.status_code == 200:
                logger.info("카카오톡 메시지 발송 성공!")
                return True
            elif resp.status_code == 401:
                logger.info("액세스 토큰 만료 감지 -> 자동 갱신 후 재시도...")
                if self.refresh_token():
                    headers["Authorization"] = f"Bearer {self.tokens['access_token']}"
                    retry_resp = requests.post(send_url, headers=headers, data=payload, timeout=10)
                    return retry_resp.status_code == 200
            else:
                logger.error(f"카카오톡 발송 실패 ({resp.status_code}): {resp.text}")
                return False
        except Exception as e:
            logger.error(f"카카오톡 발송 통신 에러: {e}")
            return False

    def send_mobile_dashboard_link(self, tunnel_url):
        """원격 모바일 대시보드 URL 발송"""
        msg = (
            f"📱 [나무증권 AI 퀀트] 모바일 대시보드 오픈\n\n"
            f"오늘의 안전 원격 접속 링크입니다:\n"
            f"{tunnel_url}\n\n"
            f"• 접속 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"• 아래 버튼을 터치하면 대시보드로 바로 연결됩니다."
        )
        return self.send_text(msg, link_url=tunnel_url, button_title="📱 모바일 대시보드 열기")

    def send_trade_event(self, event_type, symbol, name, price, qty, reason="", return_pct=0.0, pnl_won=0, entry_price=0.0):
        """체결 알림 발송 (BUY/SELL)"""
        now = datetime.now().strftime("%H:%M:%S")
        if event_type.upper() == "BUY":
            header = "🟢 [매수 체결 완료]"
            pnl_info = f"• 매수금액: {price * qty:,}원"
        else:
            pnl_icon = "📈" if return_pct >= 0 else "📉"
            header = f"🔴 [매도 체결 완료] {pnl_icon} {return_pct:+.2f}%"
            entry_line = f"• 진입단가: {int(entry_price):,}원\n" if entry_price > 0 else ""
            won_line = f" ({pnl_won:+,}원)" if pnl_won != 0 else ""
            pnl_info = f"{entry_line}• 실현수익률: {return_pct:+.2f}%{won_line}\n• 매도사유: {reason}"

        msg = (
            f"{header}\n\n"
            f"• 종목: {name} ({symbol})\n"
            f"• 체결단가: {price:,}원 ({qty}주)\n"
            f"{pnl_info}\n"
            f"• 체결시각: {now}"
        )
        return self.send_text(msg)

# 싱글톤 인스턴스
kakao = KakaoNotifier()
