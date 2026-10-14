"""실행: uv run python run.py  (또는 배포된 ktx.exe)  →  http://localhost:8000

- 개발 모드(uv run): 콘솔에 안내를 찍고 Ctrl+C 로 끝낸다.
- exe 모드(또는 KTX_TRAY=1): 콘솔 창 없이 돌고, 작업 표시줄 트레이 아이콘에서 "열기 / 종료"만 제공한다.
  로그는 데이터 폴더의 ktx.log 에 쓴다.
- 이미 실행 중이면 새로 띄우지 않고 브라우저만 연다.
"""

import logging
import os
import socket
import sys
import threading
import webbrowser

PORT = int(os.environ.get("KTX_PORT", "8000"))
URL = f"http://localhost:{PORT}"
TRAY = os.environ.get("KTX_TRAY") == "1" or (getattr(sys, "frozen", False) and os.environ.get("KTX_TRAY") != "0")


def _already_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def _open_browser(*_) -> None:
    if os.environ.get("KTX_NO_BROWSER") != "1":
        webbrowser.open(URL)


def _setup_file_logging() -> None:
    """콘솔이 없는 모드: stdout/stderr 가 None 이라 로그를 파일로 보낸다."""
    from ktx import store

    log = store.DATA_DIR / "ktx.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    if sys.stdout is None:
        sys.stdout = open(log, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 — 프로세스 수명 동안 유지
    if sys.stderr is None:
        sys.stderr = sys.stdout


def _tray_icon():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=16, fill=(37, 99, 235, 255))
    d.rounded_rectangle((16, 14, 48, 44), radius=8, fill=(255, 255, 255, 255))  # 차체
    d.rectangle((20, 20, 44, 30), fill=(37, 99, 235, 255))  # 창문
    d.ellipse((18, 46, 28, 56), fill=(255, 255, 255, 255))  # 바퀴
    d.ellipse((36, 46, 46, 56), fill=(255, 255, 255, 255))
    return img


def _serve(server) -> None:
    server.run()


def main() -> None:
    if _already_running():
        if not TRAY:
            print(f"이미 실행 중입니다: {URL}")
        _open_browser()
        return

    if TRAY:
        _setup_file_logging()

    import uvicorn

    from ktx.app import app

    log_config = None if TRAY else uvicorn.config.LOGGING_CONFIG
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", log_config=log_config)
    server = uvicorn.Server(config)

    if not TRAY:
        print("=" * 52)
        print("  KTX 자동예매가 실행 중입니다")
        print(f"  주소: {URL}")
        print("  이 창을 닫으면 감시가 멈춥니다. PC 절전도 꺼 두세요.")
        print("=" * 52)
        threading.Timer(1.5, _open_browser).start()
        server.run()
        return

    # 트레이 모드: 서버는 백그라운드 스레드, 트레이 아이콘이 메인 스레드(Windows 메시지 루프)를 차지한다.
    import pystray

    t = threading.Thread(target=_serve, args=(server,), daemon=True)
    t.start()
    threading.Timer(1.5, _open_browser).start()

    def quit_app(icon, *_):
        server.should_exit = True
        t.join(timeout=5)
        icon.stop()

    icon = pystray.Icon(
        "ktx",
        _tray_icon(),
        "KTX 자동예매 — 감시 중",
        menu=pystray.Menu(
            pystray.MenuItem("열기", _open_browser, default=True),
            pystray.MenuItem("종료 (감시 멈춤)", quit_app),
        ),
    )
    icon.run()


def _fatal(exc: BaseException) -> None:
    """트레이 모드는 콘솔이 없어서 치명적 오류를 메시지 상자로 보여준다."""
    logging.getLogger("ktx").exception("startup failed")
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, f"KTX 자동예매를 시작하지 못했어요.\n\n{exc}", "KTX 자동예매", 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        if TRAY:
            _fatal(e)
        raise
