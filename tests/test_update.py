"""새 버전 확인 단위 테스트 — 실제 네트워크 없이 GitHub 응답을 흉내 낸다.

실행:  uv run pytest tests/test_update.py -v
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import sys

import pytest

from ktx import update

RELEASE = "https://github.com/JayChoi07/ktx/releases/tag/v1.4.0"


class FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.is_error = body, status >= 400
        self.status_code = status

    def json(self):
        return self._body


@pytest.fixture
def github(monkeypatch):
    calls = []

    def serve(body, status=200):
        def fake_get(url, **kw):
            calls.append(url)
            if isinstance(body, Exception):
                raise body
            return FakeResponse(body, status)

        monkeypatch.setattr(update.httpx, "get", fake_get)
        return calls

    return serve


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("v1.3.1", (1, 3, 1)), ("1.10.0", (1, 10, 0)), (" v2 ", (2,)), ("dev", None), ("v1.4.0-rc1", None), ("", None)],
)
def test_parse(raw, expected):
    assert update._parse(raw) == expected


def test_newer_release_is_reported(github):
    github({"tag_name": "v1.4.0", "html_url": RELEASE})
    assert update.check("1.3.1") == {"version": "1.4.0", "url": RELEASE, "asset": None}  # exe 정보가 없으면 자동 설치는 못 한다


def test_versions_compare_numerically_not_as_text(github):
    github({"tag_name": "v1.10.0", "html_url": RELEASE})
    assert update.check("1.9.0")["version"] == "1.10.0"  # 문자열 비교면 "1.10" < "1.9" 가 된다


@pytest.mark.parametrize("current", ["1.4.0", "1.5.0"])
def test_same_or_older_release_is_ignored(github, current):
    github({"tag_name": "v1.4.0", "html_url": RELEASE})
    assert update.check(current) is None


def test_dev_run_never_calls_the_network(github):
    calls = github({"tag_name": "v9.9.9", "html_url": RELEASE})
    assert update.check("dev") is None
    assert calls == []


def test_network_error_and_http_error_are_ignored(github):
    github(ConnectionError("offline"))
    assert update.check("1.3.1") is None
    github({"message": "rate limited"}, status=403)
    assert update.check("1.3.1") is None


def test_malformed_response_is_ignored(github):
    github({"unexpected": True})
    assert update.check("1.3.1") is None


def test_link_outside_this_repo_is_not_trusted(github):
    """응답이 이상한 주소를 담고 있어도 화면에 링크로 내보내지 않는다."""
    github({"tag_name": "v1.4.0", "html_url": "https://evil.example/ktx.exe"})
    assert update.check("1.3.1") is None


# ================================================================ 자동 설치 (버튼 한 번으로 업데이트)
EXE_URL = "https://github.com/JayChoi07/ktx/releases/download/v1.4.0/ktx.exe"
SHA = "a" * 64


def asset(**over) -> dict:
    return {"name": "ktx.exe", "size": 1234, "digest": f"sha256:{SHA}", "browser_download_url": EXE_URL, **over}


def release(*assets) -> dict:
    return {"tag_name": "v1.4.0", "html_url": RELEASE, "assets": list(assets)}


def test_release_with_a_trustworthy_exe_can_be_installed(github):
    github(release(asset()))
    assert update.check("1.3.1")["asset"] == {"url": EXE_URL, "sha256": SHA, "size": 1234}


@pytest.mark.parametrize(
    "bad",
    [
        {"digest": None},  # GitHub 가 해시를 안 준 옛 릴리스
        {"digest": "sha256:xyz"},
        {"digest": "md5:" + "a" * 32},
        {"browser_download_url": "https://evil.example/ktx.exe"},  # 이 저장소 밖
        {"browser_download_url": "http://github.com/JayChoi07/ktx/releases/download/v1/ktx.exe"},
        {"size": 0},
        {"size": update.MAX_EXE_BYTES + 1},
        {"name": "other.exe"},
    ],
)
def test_untrustworthy_asset_disables_auto_install_but_keeps_the_notice(github, bad):
    github(release(asset(**bad)))
    found = update.check("1.3.1")
    assert found["version"] == "1.4.0" and found["asset"] is None  # 직접 받기 링크는 여전히 보여 준다


def test_check_now_reports_each_outcome(github):
    github(release(asset()))
    assert update.check_now("1.3.1")["status"] == "available" and update.available["version"] == "1.4.0"
    assert update.check_now("1.4.0")["status"] == "latest"
    assert update.check_now("dev")["status"] == "dev"
    github(ConnectionError("offline"))
    r = update.check_now("1.3.1")
    assert r["status"] == "error" and "offline" in r["error"]


# ---------------------------------------------------------------- 내려받기 검증
class FakeStream:
    def __init__(self, chunks, status=200):
        self.chunks, self.status_code, self.is_error = chunks, status, status >= 400

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self, size=None):
        yield from self.chunks


def serve_file(monkeypatch, chunks, status=200):
    seen = {}

    def fake_stream(method, url, **kw):
        seen.update(method=method, url=url, **kw)
        return FakeStream(chunks, status)

    monkeypatch.setattr(update.httpx, "stream", fake_stream)
    return seen


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_download_verifies_size_and_sha256_and_reports_progress(tmp_path, monkeypatch):
    data = b"MZ-new-exe" * 100
    seen = serve_file(monkeypatch, [data[:300], data[300:]])
    progress = []
    part = tmp_path / "ktx-new.exe.part"
    update.download({"url": EXE_URL, "sha256": sha(data), "size": len(data)}, part, progress.append)
    assert part.read_bytes() == data and progress == [300, len(data)]
    assert seen["url"] == EXE_URL and seen["follow_redirects"] is True  # GitHub 는 다른 호스트로 리다이렉트한다


@pytest.mark.parametrize(
    ("chunks", "status", "why"),
    [
        ([b"the-fake-exe-bytes"], 200, "SHA256"),  # 크기(18바이트)는 맞고 내용만 다르다
        ([b"short"], 200, "크기"),
        ([b"x" * 20_000], 200, "예상보다 커요"),
        ([b""], 404, "HTTP 404"),
    ],
)
def test_download_rejects_bad_files_and_leaves_nothing_behind(tmp_path, monkeypatch, chunks, status, why):
    expected = b"the-real-exe-bytes"
    serve_file(monkeypatch, chunks, status)
    part = tmp_path / "ktx-new.exe.part"
    with pytest.raises(update.UpdateError, match=why):
        update.download({"url": EXE_URL, "sha256": sha(expected), "size": len(expected)}, part)
    assert not part.exists()  # 검증 못 한 파일은 남기지 않는다


def test_download_refuses_a_url_outside_this_repo(tmp_path, monkeypatch):
    seen = serve_file(monkeypatch, [b"x"])
    with pytest.raises(update.UpdateError, match="주소"):
        update.download({"url": "https://evil.example/ktx.exe", "sha256": SHA, "size": 1}, tmp_path / "p")
    assert seen == {}  # 요청 자체를 보내지 않는다


# ---------------------------------------------------------------- 설치 가능 조건 / 흐름
@pytest.fixture
def installable(monkeypatch, tmp_path):
    """exe 로 실행 중이고 새 버전(해시 포함)이 있는 상태."""
    exe = tmp_path / "ktx.exe"
    exe.write_bytes(b"OLD")
    monkeypatch.setattr(update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(update.sys, "executable", str(exe))
    monkeypatch.setattr(update, "available", {"version": "1.4.0", "url": RELEASE, "asset": {"url": EXE_URL, "sha256": SHA, "size": 3}})
    monkeypatch.setattr(update, "install_state", {"phase": "idle", "done": 0, "total": 0, "error": None})
    return exe


def run_install(exit_cb):
    """start_install 이 하는 대로 락을 잡고 _install 을 (스레드 없이) 직접 돌린다. _install 은 끝나면 락을 푼다."""
    assert update._install_lock.acquire(blocking=False)
    update._install(dict(update.available["asset"]), exit_cb)


def test_can_install_requires_a_frozen_exe_and_a_verifiable_release(installable, monkeypatch):
    assert update.can_install() == (True, None)
    monkeypatch.setattr(update.sys, "frozen", False)
    ok, why = update.can_install()
    assert not ok and "exe" in why
    monkeypatch.setattr(update.sys, "frozen", True)
    monkeypatch.setattr(update, "available", {**update.available, "asset": None})
    ok, why = update.can_install()
    assert not ok and "직접" in why
    monkeypatch.setattr(update, "available", None)
    assert update.can_install()[0] is False


def test_install_downloads_swaps_in_the_helper_and_exits(installable, monkeypatch):
    calls = []

    def fake_download(a, part, progress=None):
        part.write_bytes(b"NEW")
        progress(3)
        calls.append("download")

    monkeypatch.setattr(update, "download", fake_download)
    monkeypatch.setattr(update, "_spawn_helper", lambda script: calls.append(("helper", script)))
    monkeypatch.setattr(update.time, "sleep", lambda s: None)
    run_install(lambda: calls.append("exit"))
    assert calls[0] == "download" and calls[1][0] == "helper" and calls[2] == "exit"  # 헬퍼를 띄운 뒤에야 종료한다
    assert (installable.parent / "ktx-new.exe").read_bytes() == b"NEW"
    assert installable.read_bytes() == b"OLD"  # 교체는 헬퍼가 이 프로세스가 끝난 뒤에 한다
    assert update.install_state["phase"] == "installing" and update.install_state["done"] == 3


def test_failed_install_reports_the_reason_cleans_up_and_does_not_exit(installable, monkeypatch):
    def bad_download(a, part, progress=None):
        part.write_bytes(b"partial")
        raise update.UpdateError("받은 파일의 SHA256이 달라요")

    exited = []
    monkeypatch.setattr(update, "download", bad_download)
    monkeypatch.setattr(update, "_spawn_helper", lambda script: exited.append("helper"))
    run_install(lambda: exited.append("exit"))
    assert exited == []  # 실패했으면 아무것도 바꾸지 않고, 종료하지도 않는다
    assert update.install_state["phase"] == "error" and "SHA256" in update.install_state["error"]
    assert not list(installable.parent.glob("ktx-new*"))


def test_unwritable_folder_gives_a_readable_error(installable, monkeypatch):
    def denied(a, part, progress=None):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(update, "download", denied)
    run_install(lambda: None)
    assert update.install_state["phase"] == "error" and "폴더" in update.install_state["error"]


def test_only_one_install_runs_at_a_time(installable, monkeypatch):
    monkeypatch.setattr(update, "_install", lambda asset, exit_cb: update._install_lock.release())  # 실제 _install 처럼 끝나면 락을 푼다
    assert update.start_install(lambda: None) is True
    update._install_lock.acquire()  # 첫 설치가 아직 진행 중인 것처럼
    try:
        assert update.start_install(lambda: None) is False
    finally:
        update._install_lock.release()
    monkeypatch.setattr(update.sys, "frozen", False)
    with pytest.raises(update.UpdateError):
        update.start_install(lambda: None)


# ---------------------------------------------------------------- 교체 헬퍼 스크립트 (실제 PowerShell)
def test_helper_script_waits_swaps_with_a_rollback_and_restarts_flagged(tmp_path):
    exe, new, old = tmp_path / "it's ktx.exe", tmp_path / "ktx-new.exe", tmp_path / "ktx.old"
    s = update.helper_script(4242, exe, new, old)
    assert "Wait-Process -Id 4242" in s
    assert "'" + str(exe).replace("'", "''") + "'" in s  # 경로의 작은따옴표를 이스케이프한다
    assert "Move-Item" in s and "catch" in s  # 교체가 실패하면 옛 exe 를 되돌린다
    assert "--updated" in s and "Start-Process" in s
    assert "Start-Process" not in update.helper_script(4242, exe, new, old, start=False)


def run_helper(script: str) -> None:
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]
    subprocess.run(cmd, check=True, timeout=60)


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 교체 스크립트는 Windows 전용")


@windows_only
def test_helper_really_swaps_files_after_the_process_exits(tmp_path):
    exe, new, old = tmp_path / "ktx.exe", tmp_path / "ktx-new.exe", tmp_path / "ktx.old"
    shutil.copy(r"C:\Windows\System32\cmd.exe", exe)  # 실행 중인 'ktx.exe' 흉내
    new.write_bytes(b"NEW-EXE")
    proc = subprocess.Popen([str(exe), "/c", "ping -n 3 127.0.0.1 >nul"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run_helper(update.helper_script(proc.pid, exe, new, old, start=False))
    assert exe.read_bytes() == b"NEW-EXE" and old.exists() and not new.exists()
    assert old.stat().st_size > 10_000  # 옛 exe 는 .old 로 남는다 (새 exe 가 첫 실행에 지운다)


@windows_only
def test_helper_restores_the_old_exe_when_the_new_one_is_missing(tmp_path):
    exe, new, old = tmp_path / "ktx.exe", tmp_path / "ktx-new.exe", tmp_path / "ktx.old"
    exe.write_bytes(b"OLD-EXE")
    done = subprocess.Popen(["cmd.exe", "/c", "exit"])
    done.wait()
    run_helper(update.helper_script(done.pid, exe, new, old, start=False))  # 새 파일이 없어 두 번째 이동이 실패한다
    assert exe.read_bytes() == b"OLD-EXE" and not old.exists()  # 프로그램이 사라지는 일은 없어야 한다
