"""
scripts/start_mobile_dashboard.py
Cloudflare 보안 터널을 열고 모바일 접속 URL을 생성하여
카카오톡 [나와의 채팅]으로 즉시 전송하고 바탕화면 텍스트 파일에도 저장하는 스크립트

[고가용성 & 인터넷 단절 자동 재연결(Auto-Reconnect) 통합 패치]:
1. 파이프 버퍼 데드락 원천 차단: 백그라운드 데몬 스레드가 cloudflared stdout/stderr를 실시간 소비(drain)하여 4KB 버퍼 가득 참으로 인한 동결 방지
2. Windows 절전 모드 자동 차단: SetThreadExecutionState API로 PC 및 네트워크 어댑터 절전 방지
3. 인터넷 단절 감지 & 무한 자동 재연결: 통신사 회선 순단, 공유기 재부팅 발생 시 인터넷 복구를 실시간 감지하여 자동 재접속
4. 일시적 네트워크 글리치 보호: cloudflared에 retries/grace-period를 부여하여 30초 내 회선 순단 시 동일 URL 유지
5. URL 변경 시 카카오톡 자동 재발송: IP 변경 등으로 새 URL이 발급되면 카카오톡 [나와의 채팅]으로 새 링크 즉시 갱신 전송
"""

import sys
import os
import re
import time
import socket
import ctypes
import threading
import subprocess
import urllib.request
from datetime import datetime

# UTF-8 출력 보장
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 프로젝트 루트 경로 추가
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if base_dir not in sys.path:
    sys.path.append(base_dir)
from core.telegram_notifier import telegram_notifier

CLOUDFLARED_PATH = r"C:\Users\DAPCHC-071\Desktop\system-monitor\cloudflared.exe"


def check_internet_connectivity(timeout: float = 2.0) -> bool:
    """인터넷 통신 가능 여부를 초고속 소켓으로 확인 (Google DNS, Cloudflare DNS, Telegram API)"""
    targets = [("8.8.8.8", 53), ("1.1.1.1", 53), ("api.telegram.org", 443)]
    for host, port in targets:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            s.close()
            return True
        except Exception:
            continue
    return False


def wait_for_internet_restoration():
    """인터넷 연결이 끊겼을 때 복구될 때까지 2초 주기로 감시하며 대기"""
    if check_internet_connectivity():
        return

    print("\n" + "!" * 65)
    print("🌐 [네트워크 단절 감지] PC의 인터넷 회선 연결이 끊어졌습니다.")
    print("   공유기/Wi-Fi/통신사 회선 복구를 실시간으로 대기합니다...")
    print("!" * 65)

    start_wait = time.time()
    dots = 0
    while not check_internet_connectivity():
        time.sleep(2)
        elapsed = int(time.time() - start_wait)
        dots = (dots + 1) % 4
        print(f"\r⏳ [인터넷 복구 대기 중] {elapsed}초 경과{'.' * dots}    ", end="", flush=True)

    print("\n\n" + "🎉" * 20)
    print(f"🎉 [인터넷 복구 확인] 네트워크가 정상 복구되었습니다! (단절 시간: {int(time.time() - start_wait)}초)")
    print("   모바일 대시보드 터널을 즉시 다시 연결합니다!")
    print("🎉" * 20 + "\n")


def prevent_system_sleep():
    """Windows 절전 모드 및 네트워크 절전 방지 (Stay Awake)"""
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_AWAYMODE_REQUIRED = 0x00000040
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        )
        print("💡 [절전 방지] Windows PC 및 네트워크 절전 모드 자동 차단(Stay Awake) 활성화 완료")
    except Exception as e:
        print(f"⚠️ [절전 방지 실패] {e}")


def ensure_dashboard_running():
    """웹 대시보드(포트 8080)가 실행 중인지 확인하고 안 켜져 있으면 켬"""
    try:
        urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=2)
        print("[OK] 웹 대시보드가 이미 실행 중입니다. (포트 8080)")
        return
    except Exception:
        print("[INFO] 웹 대시보드를 백그라운드에서 실행합니다...")
        py_exe = sys.executable
        subprocess.Popen([py_exe, "web_dashboard.py", "--no-browser"], cwd=base_dir)
        for _ in range(15):
            time.sleep(1)
            try:
                urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=2)
                print("[OK] 웹 대시보드 기동 완료!")
                return
            except Exception:
                pass
        print("[WARN] 대시보드 응답 대기 중 계속 진행합니다.")


def save_desktop_url(tunnel_url: str, status_msg: str = "정상 연결됨"):
    desktop_dir = r"C:\Users\DAPCHC-071\Desktop"
    txt_path = os.path.join(desktop_dir, "모바일_접속주소.txt")
    try:
        content = (
            f"오늘의 모바일 대시보드 접속 주소:\n"
            f"{tunnel_url}\n\n"
            f"• 상태: {status_msg}\n"
            f"• 최종 확인 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"• 스마트폰 브라우저나 텔레그램 링크로 접속하세요.\n"
            f"• 인터넷이 끊겼다 복구되면 자동으로 새 주소가 갱신되고 텔레그램으로 발송됩니다.\n"
        )
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[OK] 바탕화면에 접속 주소 저장 완료: {txt_path}")
    except Exception as e:
        print(f"[WARN] 텍스트 저장 실패: {e}")

    # data 디렉토리에도 실시간 URL 캐시
    try:
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "active_mobile_url.txt"), "w", encoding="utf-8") as f:
            f.write(tunnel_url.strip())
    except Exception:
        pass


class CloudflareTunnelSupervisor:
    """Cloudflare 터널 고가용성 관리자 (파이프 버퍼 드레인 + 인터넷 단절 복구 + URL 갱신 카톡 전송)"""

    def __init__(self, cloudflared_path: str):
        self.cloudflared_path = cloudflared_path
        self.proc = None
        self.current_url = None
        self.last_sent_url = None
        self.stop_requested = False
        self.url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

    def start_tunnel_process(self) -> str:
        """터널 프로세스 시작 및 URL 추출 (파이프 드레인 스레드 즉시 가동)"""
        # 먼저 인터넷 통신 가능 여부 점검
        if not check_internet_connectivity():
            wait_for_internet_restoration()

        cmd = [
            self.cloudflared_path, "tunnel",
            "--url", "http://localhost:8080",
            "--proxy-keepalive-timeout", "2m",
            "--protocol", "http2",
            "--edge-ip-version", "auto",
            "--retries", "20",
            "--grace-period", "60s",
            "--heartbeat-count", "5",
            "--heartbeat-interval", "5s"
        ]

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1
        )

        extracted_url = None
        found_event = threading.Event()

        # 실시간 스트림 소비 스레드 (파이프 데드락 100% 방지)
        def drain_output():
            nonlocal extracted_url
            try:
                for line in iter(self.proc.stdout.readline, ''):
                    if not line:
                        break
                    # URL 추출
                    if not found_event.is_set():
                        match = self.url_pattern.search(line)
                        if match:
                            extracted_url = match.group(0)
                            found_event.set()
            except Exception:
                pass
            finally:
                try:
                    self.proc.stdout.close()
                except Exception:
                    pass

        t = threading.Thread(target=drain_output, daemon=True)
        t.start()

        # 최대 35초간 URL 대기
        found_event.wait(timeout=35.0)

        if not extracted_url:
            print("[ERROR] 35초 내에 Cloudflare 터널 URL을 추출하지 못했습니다.")
            self.terminate_current()
            return None

        self.current_url = extracted_url
        return extracted_url

    def terminate_current(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

    def run_supervisor_loop(self):
        """지속 감시 및 끊김 자동 복구 루프"""
        while not self.stop_requested:
            print("\n[1/3] Cloudflare 보안 암호화 터널을 생성하는 중입니다...")
            url = self.start_tunnel_process()

            if not url:
                print("⚠️ 터널 기동 실패. 인터넷 연결 상태를 확인 후 5초 뒤 재시도합니다...")
                time.sleep(5)
                continue

            print("\n[2/3] 모바일 보안 접속 링크가 준비되었습니다!")
            print("-" * 65)
            print(f"👉 접속 URL: {url}")
            print("-" * 65)

            # 바탕화면 저장
            save_desktop_url(url, "정상 연결됨")

            # 알림 발송 (텔레그램 - 최초 또는 URL 변경 시에만 발송)
            if self.last_sent_url is None:
                print("\n[3/3] 텔레그램으로 모바일 링크를 전송합니다...")
                try:
                    tg_sent = telegram_notifier.send_mobile_dashboard_link(url)
                    if tg_sent:
                        print("🎉 [텔레그램 전송 완료] 스마트폰 텔레그램으로 링크 버튼이 도착했습니다!")
                except Exception as e:
                    print(f"[WARN] 텔레그램 발송 실패: {e}")

                self.last_sent_url = url
            elif url != self.last_sent_url:
                print("\n🔄 [주소 갱신 감지] 인터넷 재연결로 새 접속 링크가 생성되었습니다. 텔레그램으로 재발송합니다...")
                try:
                    tg_sent = telegram_notifier.send_reconnect_link(url)
                    if tg_sent:
                        print("🎉 [텔레그램 갱신 완료] 복구된 새 링크가 텔레그램으로 전송되었습니다!")
                except Exception as e:
                    print(f"[WARN] 텔레그램 갱신 발송 실패: {e}")

                self.last_sent_url = url
            else:
                print("✅ [터널 유지] 기존 모바일 접속 링크가 정상 유지되고 있습니다.")

            print("\n" + "=" * 65)
            print("🛡️ [연결 지속 보호 및 인터넷 자동 재연결 시스템 가동 중]")
            print(" - 파이프 버퍼 실시간 드레인: 활성화 (메모리 정체 방지)")
            print(" - Windows 절전 방지(Stay Awake): 활성화")
            print(" - 인터넷 단절 시 무한 자동 재접속 워치독: 활성화")
            print(" - 주기적 터널 Keep-Alive 헬스체크: 30초 주기")
            print("⚠️ 주의: 이 콘솔 창을 최소화(-)해두고 퇴근하시면 외부에서 언제든 접속 가능합니다.")
            print("=" * 65 + "\n")

            # 30초 주기 헬스체크 루프
            fail_count = 0
            while not self.stop_requested:
                # 1. 프로세스 생존 확인
                poll_res = self.proc.poll()
                if poll_res is not None:
                    print(f"\n⚠️ [터널 종료 감지] cloudflared 프로세스가 종료되었습니다 (종료코드: {poll_res})")
                    break

                # 2. 30초 대기
                time.sleep(30)

                # 3. 인터넷 자체가 살아있는지 먼저 확인
                if not check_internet_connectivity(timeout=2.0):
                    print("\n🌐 [인터넷 단절 감지] PC의 인터넷 연결이 끊어졌습니다. 터널을 재정비합니다...")
                    self.terminate_current()
                    wait_for_internet_restoration()
                    break

                # 4. HTTP 헬스체크 (터널을 통한 실제 응답 테스트)
                try:
                    req = urllib.request.Request(
                        f"{self.current_url}/api/state",
                        headers={"User-Agent": "Tunnel-Heartbeat/1.0"}
                    )
                    with urllib.request.urlopen(req, timeout=6) as resp:
                        if resp.status == 200:
                            fail_count = 0
                except Exception as e:
                    fail_count += 1
                    print(f"⚠️ [터널 핑 지연] 헬스체크 응답 지연 ({fail_count}/3회): {e}")
                    if fail_count >= 3:
                        print("🚨 [터널 무응답 3회 초과] 터널을 자동 재기동하여 경로를 복구합니다...")
                        break

            if not self.stop_requested:
                self.terminate_current()
                print("🔄 3초 후 터널을 자동으로 다시 연결합니다...")
                time.sleep(3)


def main():
    print("=" * 65)
    print("   [나무증권 AI 퀀트] 모바일 원격 대시보드 & 인터넷 무중단 자동 재연결")
    print("=" * 65)

    if not os.path.exists(CLOUDFLARED_PATH):
        print(f"[ERROR] cloudflared.exe를 찾을 수 없습니다: {CLOUDFLARED_PATH}")
        input("엔터를 누르면 종료합니다...")
        return

    prevent_system_sleep()
    ensure_dashboard_running()

    supervisor = CloudflareTunnelSupervisor(CLOUDFLARED_PATH)
    try:
        supervisor.run_supervisor_loop()
    except KeyboardInterrupt:
        print("\n\n[안내] 사용자에 의해 모바일 대시보드 터널이 안전하게 종료되었습니다.")
        supervisor.stop_requested = True
        supervisor.terminate_current()


if __name__ == "__main__":
    main()
