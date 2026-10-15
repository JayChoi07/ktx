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
import contextlib
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
ALLOWED_HOSTS = ("github.com", "githubusercontent.com")  # 릴리스 파일은 github.com 에서 *.githubusercontent.com 으로 리다이렉트된다
MAX_REDIRECTS = 5
DOWNLOAD_DEADLINE = 300.0  # 내려받기 전체에 거는 시간 제한(초) — 읽기 제한은 한 번의 읽기에만 적용된다

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


def _host_ok(url: str) -> bool:
    """내려받기 주소로 따라가도 되는 곳: HTTPS 이고 github.com / *.githubusercontent.com 뿐."""
    try:
        u = httpx.URL(url)
    except Exception:
        return False
    host = u.host or ""
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def download(asset: dict, part: Path, progress: Callable[[int], None] | None = None) -> None:
    """asset 을 part 에 받으며 크기·SHA256 을 대조한다. 어긋나면 파일을 지우고 UpdateError.

    리다이렉트는 직접 따라간다: 매 단계마다 HTTPS·호스트를 확인하고 리다이렉트 응답의 본문은 읽지 않는다.
    전체 시간 제한(DOWNLOAD_DEADLINE)과 압축 금지(Accept-Encoding: identity)로 크기 검사를 우회하거나 작업을 붙잡지 못하게 한다.
    """
    url = str(asset["url"])
    if not url.startswith(RELEASES_PREFIX):
        raise UpdateError("허용되지 않은 내려받기 주소예요")
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    digest, done = hashlib.sha256(), 0
    try:
        for _ in range(MAX_REDIRECTS + 1):
            request = {"follow_redirects": False, "headers": {"Accept-Encoding": "identity"}, "timeout": httpx.Timeout(30, read=60)}
            with httpx.stream("GET", url, **request) as r:
                if r.is_redirect:
                    url = str(r.url.join(r.headers["location"]))
                    if not _host_ok(url):
                        raise UpdateError("허용되지 않은 주소로 이동하려 해요")
                    continue
                if r.is_error:
                    raise UpdateError(f"내려받기에 실패했어요 (HTTP {r.status_code})")
                with part.open("wb") as f:
                    for chunk in r.iter_bytes(64 * 1024):
                        if time.monotonic() > deadline:
                            raise UpdateError("내려받는 데 너무 오래 걸려요")
                        done += len(chunk)
                        if done > asset["size"]:
                            raise UpdateError("받은 파일이 예상보다 커요")
                        digest.update(chunk)
                        f.write(chunk)
                        if progress:
                            progress(done)
            break
        else:
            raise UpdateError("리다이렉트가 너무 많이 이어져요")
        if done != asset["size"]:
            raise UpdateError("받은 파일 크기가 맞지 않아요")
        if digest.hexdigest() != asset["sha256"]:
            raise UpdateError("받은 파일의 SHA256이 달라요 (손상됐거나 바뀌었을 수 있어요)")
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)  # 검증 못 한 파일은 남기지 않는다
        raise


def _data(value) -> str:
    """경로를 PowerShell '문법'이 아니라 '데이터'로 넘기는 표현식.

    PowerShell 은 ASCII ' 말고도 ‘ ’ ‚ ‛ 를 작은따옴표로 취급해서, 이스케이프로는 폴더 이름이 명령이 되는 걸 막지 못한다.
    Base64 로 감싸면 스크립트에는 [A-Za-z0-9+/=] 만 들어간다.
    """
    b64 = base64.b64encode(str(value).encode("utf-8")).decode()
    return f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'))"


def helper_script(
    pid: int, exe: Path, new: Path, old: Path, sha256: str | None = None, start: bool = True, wait: int = 60, retries: int = 40
) -> str:
    """이 프로세스(pid)가 끝난 뒤 exe 를 바꾸고 새 exe 를 띄우는 PowerShell.

    - pid 가 wait 초 안에 끝나지 않으면 아무것도 바꾸지 않고 중단한다 (옛 서버가 살아 있는데 디스크만 바뀌는 일을 막는다).
    - sha256 이 주어지면 교체 직전에 ktx-new.exe 를 다시 검증한다. 다르면 지우고 교체하지 않은 채 옛 exe 를 다시 띄운다.
    - 막 끝난 프로세스의 exe 는 백신 검사 등으로 잠시 잠겨 있을 수 있어 이름 변경을 0.5초 간격으로 retries 번까지 다시 시도한다.
      끝내 못 바꾸면 옛 exe 를 그대로 둔다. 이동이 중간에 실패하면 옛 exe 를 되돌린다. 어떤 경우에도 마지막에 (새 또는 옛) exe 를 띄운다.
    """
    if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise ValueError("sha256 형식이 올바르지 않아요")
    lines = [
        f"$exe = {_data(exe)}",
        f"$new = {_data(new)}",
        f"$old = {_data(old)}",
        f"$p = Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue",
        f"if ($p) {{ $null = $p.WaitForExit({int(wait) * 1000}); "
        "if (-not $p.HasExited) { Write-Output 'old process still running'; exit 1 } }",
        "Start-Sleep -Seconds 1",
        "$swap = $true",
    ]
    if sha256:
        lines += [
            "try { $h = (Get-FileHash -LiteralPath $new -Algorithm SHA256 -ErrorAction Stop).Hash } "
            "catch { $h = ''; Write-Output \"hash error: $($_.Exception.Message)\" }",
            f"if ($h -ne '{sha256.upper()}') {{ Write-Output \"hash mismatch: got '$h'\"; $swap = $false; "
            "Remove-Item -LiteralPath $new -Force -ErrorAction SilentlyContinue }",
        ]
    lines += [
        "function Try-Move($from, $to) {",
        # 재시도는 잠금용이다 — 없는 파일은 바로 포기
        "  if (-not (Test-Path -LiteralPath $from)) { $script:why = 'missing: ' + $from; return $false }",
        f"  for ($i = 0; $i -lt {int(retries)}; $i++) {{",
        "    try { Move-Item -LiteralPath $from -Destination $to -Force -ErrorAction Stop; return $true }",
        "    catch { $script:why = $_.Exception.Message; Start-Sleep -Milliseconds 500 }",
        "  }",
        "  return $false",
        "}",
        "if ($swap) {",
        "  if (Try-Move $exe $old) {",
        "    if (-not (Try-Move $new $exe)) {",
        "      Write-Output \"swap failed: $script:why\"",
        "      if (-not (Test-Path -LiteralPath $exe)) { $null = Try-Move $old $exe }",
        "    }",
        "  } else { Write-Output \"old exe stayed locked: $script:why\" }",
        "}",
    ]
    if start:  # --updated: 새 exe 가 브라우저 탭을 또 열지 않게 한다
        lines.append("Start-Process -FilePath $exe -WorkingDirectory (Split-Path -Parent $exe) -ArgumentList '--updated'")
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


def _install(
    asset: dict,
    exit_cb: Callable[[], None],
    hold: Callable[[], bool] | None = None,
    release: Callable[[], None] | None = None,
) -> None:
    """hold: 교체 직전에 새 예약을 막고 결제 대기 건이 없을 때만 True. release: hold 이후 실패했을 때 막은 것을 푼다."""
    exe = Path(sys.executable)
    new, part, old = exe.with_name("ktx-new.exe"), exe.with_name("ktx-new.exe.part"), exe.with_name("ktx.old")
    held = False
    try:
        install_state.update(phase="downloading", done=0, total=asset["size"], error=None)
        download(asset, part, progress=lambda n: install_state.update(done=n))
        install_state["phase"] = "installing"
        part.replace(new)
        if hold is not None:
            if not hold():  # 다운로드 하는 사이 감시가 예약을 잡았을 수 있다 — 재시작하면 결제 알림이 끊긴다
                raise UpdateError("방금 예약이 잡혀서 업데이트를 미뤘어요. 결제를 마친 뒤 다시 눌러 주세요.")
            held = True
        _spawn_helper(helper_script(os.getpid(), exe, new, old, sha256=asset["sha256"]))
        time.sleep(1.5)  # 화면이 '다시 시작하는 중'을 볼 시간
        exit_cb()
    except Exception as e:
        log.warning("업데이트 실패: %s", e)
        # 정리보다 먼저 기록한다 — 파일이 잠겨 지우지 못해도 화면이 '받는 중'에 멈춰 있으면 안 된다
        install_state.update(phase="error", error=_friendly(e))
        if held and release is not None:
            with contextlib.suppress(Exception):
                release()
        for p in (part, new):
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)
    finally:
        _install_lock.release()


def start_install(
    exit_cb: Callable[[], None],
    hold: Callable[[], bool] | None = None,
    release: Callable[[], None] | None = None,
) -> bool:
    """백그라운드에서 내려받아 교체한다. 이미 진행 중이면 False, 할 수 없는 상태면 UpdateError."""
    ok, why = can_install()
    if not ok:
        raise UpdateError(why)
    if not _install_lock.acquire(blocking=False):
        return False
    try:  # 스레드가 락을 넘겨받기 전에 실패하면 여기서 풀어야 한다 (안 그러면 이후 모든 설치가 '진행 중'으로 거절된다)
        info = available  # can_install 이후 주기 확인이 바꿔 치울 수 있어 한 번만 읽는다
        if not info or not info.get("asset"):
            raise UpdateError("새 버전 정보가 바뀌었어요. 다시 확인해 주세요.")
        threading.Thread(target=_install, args=(dict(info["asset"]), exit_cb, hold, release), name="ktx-install", daemon=True).start()
    except BaseException:
        _install_lock.release()
        raise
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
