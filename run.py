"""실행: uv run python run.py  (또는 배포된 ktx.exe)  →  http://localhost:8000

이미 실행 중이면 새로 띄우지 않고 브라우저만 연다.
"""

import os
import socket
import threading
import webbrowser

import uvicorn

PORT = int(os.environ.get("KTX_PORT", "8000"))
URL = f"http://localhost:{PORT}"


def _already_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main() -> None:
    if _already_running():
        print(f"이미 실행 중입니다. 브라우저를 엽니다: {URL}")
        webbrowser.open(URL)
        return
    from ktx.app import app

    print("=" * 52)
    print("  KTX 자동예매가 실행 중입니다")
    print(f"  주소: {URL}")
    print("  이 창을 닫으면 감시가 멈춥니다. PC 절전도 꺼 두세요.")
    print("=" * 52)
    if os.environ.get("KTX_NO_BROWSER") != "1":
        threading.Timer(1.5, webbrowser.open, args=(URL,)).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
