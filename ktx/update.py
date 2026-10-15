"""새 버전 알림.

하루에 한 번 이 저장소의 최신 GitHub 릴리스를 조회해 지금보다 새로우면 화면에 알려 준다.
내려받기와 교체는 하지 않는다 (사용자가 링크에서 직접 받는다).
릴리스 exe 가 아닌 개발 실행("dev")과 KTX_NO_UPDATE_CHECK=1 은 확인하지 않는다.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

import httpx

from ._version import __version__

log = logging.getLogger("ktx")

LATEST_URL = "https://api.github.com/repos/JayChoi07/ktx/releases/latest"
RELEASES_PREFIX = "https://github.com/JayChoi07/ktx/releases/"
CHECK_EVERY = 24 * 3600

available: dict | None = None  # {"version": "1.4.0", "url": "..."} — 더 새로운 릴리스가 있을 때


def _parse(version: str) -> tuple[int, ...] | None:
    """'v1.3.1' → (1, 3, 1). 숫자 점 표기가 아니면(dev, 1.4.0-rc1 등) None."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version.strip())
    return tuple(int(n) for n in m.group(1).split(".")) if m else None


def check(current: str = __version__) -> dict | None:
    """더 새로운 릴리스가 있으면 {"version", "url"}, 아니면 None. 개발 실행·네트워크 오류·이상한 응답도 None."""
    cur = _parse(current)
    if cur is None:
        return None
    try:
        r = httpx.get(LATEST_URL, headers={"Accept": "application/vnd.github+json"}, timeout=5)
        if r.is_error:
            raise RuntimeError(f"HTTP {r.status_code}")
        data = r.json()
        latest, url = _parse(str(data["tag_name"])), str(data["html_url"])
    except Exception as e:
        log.info("업데이트 확인 실패: %s", e)
        return None
    if latest is None or latest <= cur or not url.startswith(RELEASES_PREFIX):  # 화면에 링크로 내보내므로 이 저장소 주소만 믿는다
        return None
    return {"version": ".".join(map(str, latest)), "url": url}


def _loop() -> None:
    global available
    while True:
        available = check() or available
        time.sleep(CHECK_EVERY)


def start() -> None:
    if os.environ.get("KTX_NO_UPDATE_CHECK") == "1" or _parse(__version__) is None:
        return
    threading.Thread(target=_loop, name="ktx-update", daemon=True).start()
