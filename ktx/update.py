"""새 버전 확인과 한 번의 버튼으로 업데이트.

하루에 한 번(그리고 설정의 '지금 확인'을 누를 때) 이 저장소의 최신 GitHub 릴리스를 조회해 지금보다 새로우면 화면에 알려 준다.
설치는 사용자가 버튼을 눌렀을 때만 한다: 릴리스의 ktx.exe 를 받아 GitHub 가 알려 주는 SHA256 과 크기를 대조하고,
맞을 때만 실행 중인 exe 와 바꿔 다시 실행한다. 해시가 없거나 주소·크기가 이상하면 자동 설치를 막고 직접 받기 링크만 보여 준다.

exe 가 자기 자신을 덮어쓸 수는 없지만 이름은 바꿀 수 있어서, 이 프로세스가 끝나기를 기다리는 PowerShell 헬퍼가
ktx.exe → ktx.old, ktx-new.exe → ktx.exe 로 바꾸고 새 exe 를 띄운다 (둘째 이동이 실패하면 옛 exe 를 되돌린다).
ktx.old 는 새 exe 가 첫 실행에 지운다 — 새 exe 가 뜨지 못했다면 남아 있어 복구할 수 있다.
릴리스 exe 가 아닌 개발 실행("dev")과 KTX_NO_UPDATE_CHECK=1 은 확인하지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from ._version import __version__

log = logging.getLogger("ktx")

LATEST_URL = "https://api.github.com/repos/JayChoi07/ktx/releases/latest"
RELEASES_PREFIX = "https://github.com/JayChoi07/ktx/releases/"
ASSET_NAME = "ktx.exe"
CHECK_EVERY = 24 * 3600
MAX_EXE_BYTES = 200 * 1024 * 1024  # 이보다 큰 파일은 릴리스 exe 가 아니다

available: dict | None = None  # {"version", "url", "asset": {"url", "sha256", "size"} | None} — 더 새로운 릴리스가 있을 때
install_state: dict = {"phase": "idle", "done": 0, "total": 0, "error": None}  # phase: idle | downloading | installing | error
_install_lock = threading.Lock()


class UpdateError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 업데이트 실패 사유."""


def _parse(version: str) -> tuple[int, ...] | None:
    """'v1.3.1' → (1, 3, 1). 숫자 점 표기가 아니면(dev, 1.4.0-rc1 등) None."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version.strip())
    return tuple(int(n) for n in m.group(1).split(".")) if m else None


def _asset(data: dict) -> dict | None:
    """릴리스에 붙은 ktx.exe — 주소·크기·SHA256 이 모두 믿을 만할 때만. 하나라도 이상하면 None (자동 설치 불가, 직접 받기만)."""
    for a in data.get("assets") or []:
        if a.get("name") != ASSET_NAME:
            continue
        digest = re.fullmatch(r"sha256:([0-9a-f]{64})", str(a.get("digest") or ""))
        url, size = str(a.get("browser_download_url") or ""), a.get("size")
        if digest and url.startswith(RELEASES_PREFIX) and isinstance(size, int) and 0 < size <= MAX_EXE_BYTES:
            return {"url": url, "sha256": digest.group(1), "size": size}
    return None


def _latest() -> dict | None:
    """GitHub 최신 릴리스 {"version", "url", "asset"}. 응답이 이상하면 None, 네트워크·HTTP 오류는 예외."""
    r = httpx.get(LATEST_URL, headers={"Accept": "application/vnd.github+json"}, timeout=5)
    if r.is_error:
        raise RuntimeError(f"HTTP {r.status_code}")
    data = r.json()
    version, url = _parse(str(data["tag_name"])), str(data["html_url"])
    if version is None or not url.startswith(RELEASES_PREFIX):  # 화면에 링크로 내보내므로 이 저장소 주소만 믿는다
        return None
    return {"version": ".".join(map(str, version)), "url": url, "asset": _asset(data)}


def check(current: str = __version__) -> dict | None:
    """더 새로운 릴리스가 있으면 그 정보, 아니면 None. 개발 실행·네트워크 오류·이상한 응답도 None."""
    if _parse(current) is None:
        return None
    try:
        latest = _latest()
    except Exception as e:
        log.info("업데이트 확인 실패: %s", e)
        return None
    if latest is None or _parse(latest["version"]) <= _parse(current):
        return None
    return latest


def check_now(current: str = __version__) -> dict:
    """설정의 '지금 확인': 결과를 구분해서 돌려준다 (available | latest | error | dev)."""
    global available
    if _parse(current) is None:
        return {"status": "dev", "update": None, "error": "개발 실행에서는 업데이트를 확인하지 않아요"}
    try:
        latest = _latest()
    except Exception as e:
        return {"status": "error", "update": None, "error": str(e)}
    if latest is None:
        return {"status": "error", "update": None, "error": "릴리스 정보를 읽지 못했어요"}
    if _parse(latest["version"]) <= _parse(current):
        return {"status": "latest", "update": None, "error": None, "latest": latest["version"]}
    available = latest
    return {"status": "available", "update": latest, "error": None}


# ---------------------------------------------------------------- 설치
def can_install() -> tuple[bool, str | None]:
    """(가능 여부, 불가 사유)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False, "exe 로 실행 중일 때만 자동 업데이트를 할 수 있어요. (직접 받기를 이용하세요)"
    if not available or not available.get("asset"):
        return False, "이 릴리스는 자동 설치용 정보(SHA256)가 없어요. 직접 받아 주세요."
    return True, None


def download(asset: dict, part: Path, progress: Callable[[int], None] | None = None) -> None:
    """asset 을 part 에 받으며 크기·SHA256 을 대조한다. 어긋나면 파일을 지우고 UpdateError."""
    if not str(asset["url"]).startswith(RELEASES_PREFIX):
        raise UpdateError("허용되지 않은 내려받기 주소예요")
    digest, done = hashlib.sha256(), 0
    try:
        with httpx.stream("GET", asset["url"], follow_redirects=True, timeout=httpx.Timeout(30, read=60)) as r:
            if r.is_error:
                raise UpdateError(f"내려받기에 실패했어요 (HTTP {r.status_code})")
            with part.open("wb") as f:
                for chunk in r.iter_bytes(64 * 1024):
                    done += len(chunk)
                    if done > asset["size"]:
                        raise UpdateError("받은 파일이 예상보다 커요")
                    digest.update(chunk)
                    f.write(chunk)
                    if progress:
                        progress(done)
        if done != asset["size"]:
            raise UpdateError("받은 파일 크기가 맞지 않아요")
        if digest.hexdigest() != asset["sha256"]:
            raise UpdateError("받은 파일의 SHA256이 달라요 (손상됐거나 바뀌었을 수 있어요)")
    except BaseException:
        part.unlink(missing_ok=True)  # 검증 못 한 파일은 남기지 않는다
        raise


def _ps(value) -> str:
    """PowerShell 작은따옴표 문자열 리터럴."""
    return "'" + str(value).replace("'", "''") + "'"


def helper_script(pid: int, exe: Path, new: Path, old: Path, start: bool = True) -> str:
    """이 프로세스(pid)가 끝난 뒤 exe 를 바꾸고 새 exe 를 띄우는 PowerShell. 교체가 실패하면 옛 exe 를 되돌린다."""
    lines = [
        f"Wait-Process -Id {pid} -Timeout 60 -ErrorAction SilentlyContinue",
        "Start-Sleep -Seconds 1",
        "try {",
        f"  Move-Item -LiteralPath {_ps(exe)} -Destination {_ps(old)} -Force -ErrorAction Stop",
        f"  Move-Item -LiteralPath {_ps(new)} -Destination {_ps(exe)} -Force -ErrorAction Stop",
        "} catch {",
        f"  if (-not (Test-Path -LiteralPath {_ps(exe)}) -and (Test-Path -LiteralPath {_ps(old)})) "
        f"{{ Move-Item -LiteralPath {_ps(old)} -Destination {_ps(exe)} -Force }}",
        "}",
    ]
    if start:  # --updated: 새 exe 가 브라우저 탭을 또 열지 않게 한다
        lines.append(f"Start-Process -FilePath {_ps(exe)} -WorkingDirectory {_ps(exe.parent)} -ArgumentList '--updated'")
    return "\n".join(lines)


def _spawn_helper(script: str) -> None:
    """헬퍼를 이 프로세스와 독립적으로 띄운다. PyInstaller 6.9+ 는 자기 자신보다 오래 사는 자식에 RESET_ENVIRONMENT 가 필요하다."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    subprocess.Popen(cmd, env=env, creationflags=flags, close_fds=True, **quiet)


def _friendly(e: Exception) -> str:
    if isinstance(e, UpdateError):
        return str(e)
    if isinstance(e, PermissionError):
        return "프로그램이 있는 폴더에 쓸 수 없어요. 쓰기 가능한 폴더로 옮기거나 직접 받아 주세요."
    return f"업데이트에 실패했어요: {e}"


def _install(asset: dict, exit_cb: Callable[[], None]) -> None:
    exe = Path(sys.executable)
    new, part, old = exe.with_name("ktx-new.exe"), exe.with_name("ktx-new.exe.part"), exe.with_name("ktx.old")
    try:
        install_state.update(phase="downloading", done=0, total=asset["size"], error=None)
        download(asset, part, progress=lambda n: install_state.update(done=n))
        install_state["phase"] = "installing"
        part.replace(new)
        _spawn_helper(helper_script(os.getpid(), exe, new, old))
        time.sleep(1.5)  # 화면이 '다시 시작하는 중'을 볼 시간
        exit_cb()
    except Exception as e:
        log.warning("업데이트 실패: %s", e)
        for p in (part, new):
            p.unlink(missing_ok=True)
        install_state.update(phase="error", error=_friendly(e))
    finally:
        _install_lock.release()


def start_install(exit_cb: Callable[[], None]) -> bool:
    """백그라운드에서 내려받아 교체한다. 이미 진행 중이면 False, 할 수 없는 상태면 UpdateError."""
    ok, why = can_install()
    if not ok:
        raise UpdateError(why)
    if not _install_lock.acquire(blocking=False):
        return False
    threading.Thread(target=_install, args=(dict(available["asset"]), exit_cb), name="ktx-install", daemon=True).start()
    return True


# ---------------------------------------------------------------- 주기 확인
def _loop() -> None:
    global available
    while True:
        available = check() or available
        time.sleep(CHECK_EVERY)


def start() -> None:
    if os.environ.get("KTX_NO_UPDATE_CHECK") == "1" or _parse(__version__) is None:
        return
    threading.Thread(target=_loop, name="ktx-update", daemon=True).start()
