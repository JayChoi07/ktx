"""새 버전 확인 단위 테스트 — 실제 네트워크 없이 GitHub 응답을 흉내 낸다.

실행:  uv run pytest tests/test_update.py -v
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import httpx
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
CDN = "https://release-assets.githubusercontent.com/github-production-release-asset/1/abc?sig=x"


class FakeStream:
    def __init__(self, chunks=(), status=200, location=None):
        self.chunks, self.status_code, self.is_error = list(chunks), status, status >= 400
        self.is_redirect = status in (301, 302, 307, 308)
        self.headers = {"location": location} if location else {}
        self.url = httpx.URL(EXE_URL)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self, size=None):
        assert not self.is_redirect, "리다이렉트 응답의 본문은 읽으면 안 된다"
        yield from self.chunks


def serve(monkeypatch, *responses):
    """httpx.stream 이 호출될 때마다 responses 를 차례로 돌려주고, 호출 기록을 모아 돌려준다."""
    seen, queue = [], list(responses)

    def fake_stream(method, url, **kw):
        seen.append({"method": method, "url": url, **kw})
        return queue.pop(0)

    monkeypatch.setattr(update.httpx, "stream", fake_stream)
    return seen


def serve_file(monkeypatch, chunks, status=200):
    return serve(monkeypatch, FakeStream(chunks, status))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_download_verifies_size_and_sha256_and_reports_progress(tmp_path, monkeypatch):
    data = b"MZ-new-exe" * 100
    seen = serve_file(monkeypatch, [data[:300], data[300:]])
    progress = []
    part = tmp_path / "ktx-new.exe.part"
    update.download({"url": EXE_URL, "sha256": sha(data), "size": len(data)}, part, progress.append)
    assert part.read_bytes() == data and progress == [300, len(data)]
    assert seen[0]["url"] == EXE_URL
    assert seen[0]["follow_redirects"] is False  # 리다이렉트는 직접 처리한다 (아래 테스트)
    assert seen[0]["headers"]["Accept-Encoding"] == "identity"  # 압축 응답으로 크기 검사를 우회하지 못하게


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
    assert seen == []  # 요청 자체를 보내지 않는다


def test_download_follows_github_redirects_by_hand_over_https(tmp_path, monkeypatch):
    data = b"exe-bytes"
    seen = serve(monkeypatch, FakeStream(status=302, location=CDN), FakeStream([data]))
    update.download({"url": EXE_URL, "sha256": sha(data), "size": len(data)}, tmp_path / "p")
    assert [s["url"] for s in seen] == [EXE_URL, CDN] and all(s["follow_redirects"] is False for s in seen)


@pytest.mark.parametrize(
    "target",
    [
        "http://release-assets.githubusercontent.com/x",  # HTTPS -> HTTP 다운그레이드
        "https://evil.example/x",
        "https://githubusercontent.com.evil.example/x",  # 접미사만 비슷한 호스트
        "ftp://github.com/x",
    ],
)
def test_download_refuses_redirects_that_leave_https_github(tmp_path, monkeypatch, target):
    seen = serve(monkeypatch, FakeStream(status=302, location=target), FakeStream([b"x"]))
    with pytest.raises(update.UpdateError, match="주소"):
        update.download({"url": EXE_URL, "sha256": SHA, "size": 1}, tmp_path / "p")
    assert len(seen) == 1  # 허용되지 않은 곳으로는 요청을 보내지 않는다


def test_download_gives_up_after_too_many_redirects(tmp_path, monkeypatch):
    serve(monkeypatch, *[FakeStream(status=302, location=CDN) for _ in range(20)])
    with pytest.raises(update.UpdateError, match="너무 많이"):
        update.download({"url": EXE_URL, "sha256": SHA, "size": 1}, tmp_path / "p")


def test_download_has_an_overall_deadline(tmp_path, monkeypatch):
    """조금씩 계속 오는 응답이 작업을 붙잡고 있지 못하게 — 읽기 제한(read=60)은 한 번의 읽기에만 적용된다."""
    serve(monkeypatch, FakeStream([b"a" * 10, b"a" * 10]))
    clock = iter([0.0])

    class FakeTime:
        @staticmethod
        def monotonic():
            return next(clock, update.DOWNLOAD_DEADLINE + 1.0)  # 처음 한 번만 0 이고 그 뒤로는 기한을 넘긴 시각

    monkeypatch.setattr(update, "time", FakeTime)
    part = tmp_path / "p"
    with pytest.raises(update.UpdateError, match="너무 오래"):
        update.download({"url": EXE_URL, "sha256": SHA, "size": 20}, part)
    assert not part.exists()


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


def run_install(exit_cb, hold=None, release=None):
    """start_install 이 하는 대로 락을 잡고 _install 을 (스레드 없이) 직접 돌린다. _install 은 끝나면 락을 푼다."""
    assert update._install_lock.acquire(blocking=False)
    update._install(dict(update.available["asset"]), exit_cb, hold, release)


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


def fake_download_ok(a, part, progress=None):
    part.write_bytes(b"NEW")
    if progress:
        progress(3)


def test_install_holds_new_reservations_right_before_restarting(installable, monkeypatch):
    """다운로드 중에 예약이 잡혔을 수 있다 — 교체 직전에 새 예약을 막고 결제 대기 건이 없을 때만 재시작한다 (astra 리뷰)."""
    order = []
    monkeypatch.setattr(update, "download", lambda a, part, progress=None: (fake_download_ok(a, part, progress), order.append("download")))
    monkeypatch.setattr(update, "_spawn_helper", lambda script: order.append("helper"))
    monkeypatch.setattr(update.time, "sleep", lambda s: None)
    run_install(lambda: order.append("exit"), hold=lambda: order.append("hold") or True, release=lambda: order.append("release"))
    assert order == ["download", "hold", "helper", "exit"]  # 막은 뒤에야 헬퍼를 띄우고, 정상이면 풀지 않는다(곧 종료)


def test_install_is_postponed_when_a_booking_slipped_in(installable, monkeypatch):
    spawned = []
    monkeypatch.setattr(update, "download", fake_download_ok)
    monkeypatch.setattr(update, "_spawn_helper", lambda script: spawned.append(script))
    exited = []
    run_install(lambda: exited.append(1), hold=lambda: False, release=lambda: spawned.append("release"))
    assert spawned == [] and exited == []  # 아무것도 바꾸지 않고 종료하지도 않는다
    assert update.install_state["phase"] == "error" and "예약" in update.install_state["error"]
    assert not list(installable.parent.glob("ktx-new*"))


def test_hold_is_released_when_the_restart_fails_after_it(installable, monkeypatch):
    released = []
    monkeypatch.setattr(update, "download", fake_download_ok)

    def boom(script):
        raise OSError("powershell 를 못 찾았어요")

    monkeypatch.setattr(update, "_spawn_helper", boom)
    run_install(lambda: None, hold=lambda: True, release=lambda: released.append(1))
    assert released == [1] and update.install_state["phase"] == "error"  # 감시가 새 예약을 영영 못 잡는 일은 없어야 한다


def test_error_state_is_recorded_even_if_cleanup_is_blocked(installable, monkeypatch):
    """백신·다른 프로세스가 파일을 잡고 있어 지우지 못해도 화면이 '받는 중'에 멈춰 있으면 안 된다 (astra 리뷰)."""

    def bad_download(a, part, progress=None):
        part.write_bytes(b"partial")
        raise update.UpdateError("받은 파일 크기가 맞지 않아요")

    def locked(self, *a, **k):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(update, "download", bad_download)
    monkeypatch.setattr(Path, "unlink", locked)
    run_install(lambda: None)
    assert update.install_state["phase"] == "error" and "크기" in update.install_state["error"]


def test_start_install_does_not_leak_the_lock_when_release_info_vanishes(installable, monkeypatch):
    """can_install 통과 뒤 주기 확인이 asset 없는 릴리스로 바꿔 치면 예외로 락이 영구히 잡히던 경합 (astra 리뷰)."""
    monkeypatch.setattr(update, "can_install", lambda: (True, None))
    monkeypatch.setattr(update, "available", {"version": "1.4.0", "url": RELEASE, "asset": None})
    with pytest.raises(update.UpdateError):
        update.start_install(lambda: None)
    assert update._install_lock.acquire(blocking=False)  # 락이 풀려 있다
    update._install_lock.release()


def test_start_install_releases_the_lock_if_the_thread_cannot_start(installable, monkeypatch):
    class NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(update.threading, "Thread", NoThread)
    with pytest.raises(RuntimeError):
        update.start_install(lambda: None)
    assert update._install_lock.acquire(blocking=False)
    update._install_lock.release()


def test_unwritable_folder_gives_a_readable_error(installable, monkeypatch):
    def denied(a, part, progress=None):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(update, "download", denied)
    run_install(lambda: None)
    assert update.install_state["phase"] == "error" and "폴더" in update.install_state["error"]


def test_only_one_install_runs_at_a_time(installable, monkeypatch):
    # 실제 _install 처럼 끝나면 락을 푼다
    monkeypatch.setattr(update, "_install", lambda asset, exit_cb, hold, release: update._install_lock.release())
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
def test_helper_script_passes_paths_as_data_not_as_script_syntax(tmp_path):
    """PowerShell 은 ASCII ' 말고 ‘ ’ ‚ ‛ 도 작은따옴표로 쳐서 이스케이프로 못 막는다 — 경로를 스크립트 문법에 넣지 않는다 (astra 리뷰)."""
    exe = tmp_path / "O’Brien ‘x’ it's" / "ktx.exe"
    s = update.helper_script(4242, exe, exe.with_name("ktx-new.exe"), exe.with_name("ktx.old"), sha256="AB" * 32)
    assert str(exe) not in s and "O’Brien" not in s and "it's" not in s
    assert base64.b64encode(str(exe).encode("utf-8")).decode() in s
    assert "Get-FileHash" in s and "AB" * 32 in s  # 교체 직전에 파일을 한 번 더 검증한다
    assert "--updated" in s and "Start-Process" in s
    assert "Start-Process" not in update.helper_script(4242, exe, exe.with_name("ktx-new.exe"), exe.with_name("ktx.old"), start=False)


def run_helper(script: str) -> int:
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]
    done = subprocess.run(cmd, timeout=120, capture_output=True, text=True, errors="replace")
    run_helper.output = (done.stdout or "") + (done.stderr or "")  # 실패했을 때 이유를 볼 수 있게 (헬퍼는 실패 사유를 출력한다)
    return done.returncode


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 교체 스크립트는 Windows 전용")
CMD_EXE = r"C:\Windows\System32\cmd.exe"


def running_fake_exe(exe: Path, seconds: int) -> subprocess.Popen:
    shutil.copy(CMD_EXE, exe)  # 실행 중인 'ktx.exe' 흉내
    return subprocess.Popen([str(exe), "/c", f"ping -n {seconds} 127.0.0.1 >nul"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@windows_only
def test_helper_really_swaps_files_after_the_process_exits(tmp_path):
    exe, new, old = tmp_path / "ktx.exe", tmp_path / "ktx-new.exe", tmp_path / "ktx.old"
    new.write_bytes(b"NEW-EXE")
    proc = running_fake_exe(exe, 3)
    run_helper(update.helper_script(proc.pid, exe, new, old, sha256=sha(b"NEW-EXE").upper(), start=False))
    assert exe.read_bytes() == b"NEW-EXE" and old.exists() and not new.exists(), run_helper.output
    assert old.stat().st_size > 10_000  # 옛 exe 는 .old 로 남는다 (새 exe 가 첫 실행에 지운다)


@windows_only
def test_helper_waits_out_a_brief_lock_on_the_old_exe(tmp_path):
    """막 끝난 프로세스의 exe 는 백신 검사 등으로 잠깐 잠겨 있다 — 한 번 실패하고 포기하면 업데이트가 조용히 안 된다 (CI 에서 겪음)."""
    exe, new, old = tmp_path / "ktx.exe", tmp_path / "ktx-new.exe", tmp_path / "ktx.old"
    exe.write_bytes(b"OLD-EXE")
    new.write_bytes(b"NEW-EXE")
    done = subprocess.Popen(["cmd.exe", "/c", "exit"])
    done.wait()
    held = exe.open("rb")  # 이 핸들이 열려 있는 동안 Windows 는 이름 변경을 거부한다
    release = threading.Timer(6.0, held.close)  # 헬퍼가 첫 시도를 하는 시각(PowerShell 기동 포함 2~3초)보다 충분히 길게
    release.start()
    try:
        run_helper(update.helper_script(done.pid, exe, new, old, sha256=sha(b"NEW-EXE"), start=False))
    finally:
        release.cancel()
        held.close()
    assert exe.read_bytes() == b"NEW-EXE" and old.read_bytes() == b"OLD-EXE", run_helper.output


@windows_only
def test_helper_gives_up_cleanly_if_the_old_exe_stays_locked(tmp_path):
    """끝내 못 바꾸면 옛 exe 를 그대로 둔 채 끝난다 (프로그램이 사라지지 않는다)."""
    exe, new, old = tmp_path / "ktx.exe", tmp_path / "ktx-new.exe", tmp_path / "ktx.old"
    exe.write_bytes(b"OLD-EXE")
    new.write_bytes(b"NEW-EXE")
    done = subprocess.Popen(["cmd.exe", "/c", "exit"])
    done.wait()
    held = exe.open("rb")
    try:
        run_helper(update.helper_script(done.pid, exe, new, old, start=False, retries=3))
    finally:
        held.close()
    assert exe.read_bytes() == b"OLD-EXE" and not old.exists(), run_helper.output


@windows_only
def test_helper_refuses_a_file_that_no_longer_matches_the_verified_hash(tmp_path):
    """검증한 뒤 ktx-new.exe 가 바뀌었다면 실행하지 않는다 — 그리고 옛 exe 는 그대로 둔다 (astra 리뷰)."""
    exe, new, old = tmp_path / "ktx.exe", tmp_path / "ktx-new.exe", tmp_path / "ktx.old"
    new.write_bytes(b"TAMPERED")
    proc = running_fake_exe(exe, 2)
    original = exe.read_bytes()
    run_helper(update.helper_script(proc.pid, exe, new, old, sha256=sha(b"THE-VERIFIED-BYTES"), start=False))
    assert exe.read_bytes() == original and not old.exists() and not new.exists()


@windows_only
def test_helper_restores_the_old_exe_when_the_new_one_is_missing(tmp_path):
    exe, new, old = tmp_path / "ktx.exe", tmp_path / "ktx-new.exe", tmp_path / "ktx.old"
    exe.write_bytes(b"OLD-EXE")
    done = subprocess.Popen(["cmd.exe", "/c", "exit"])
    done.wait()
    run_helper(update.helper_script(done.pid, exe, new, old, start=False))  # 새 파일이 없어 두 번째 이동이 실패한다
    assert exe.read_bytes() == b"OLD-EXE" and not old.exists()  # 프로그램이 사라지는 일은 없어야 한다


@windows_only
def test_helper_changes_nothing_if_the_old_process_does_not_exit(tmp_path):
    """종료를 확인하지 못했는데 교체·재실행하면 디스크만 새 버전이고 서버는 옛 버전으로 남는다 (astra 리뷰)."""
    exe, new, old = tmp_path / "ktx.exe", tmp_path / "ktx-new.exe", tmp_path / "ktx.old"
    new.write_bytes(b"NEW-EXE")
    proc = running_fake_exe(exe, 30)
    try:
        original = exe.read_bytes()
        code = run_helper(update.helper_script(proc.pid, exe, new, old, start=False, wait=2))
        assert code != 0  # 중단했다
        assert exe.read_bytes() == original and not old.exists() and new.exists()  # 아무것도 바꾸지 않았다
    finally:
        proc.kill()


@windows_only
def test_helper_survives_quotes_and_injection_in_the_folder_name(tmp_path):
    """폴더 이름이 스크립트가 되면 안 된다. 곧은 따옴표·굽은 따옴표·$(…) 가 든 이름에서도 그냥 교체만 된다 (astra 리뷰)."""
    marker = Path.home() / "ktx_inj_marker.txt"
    marker.unlink(missing_ok=True)
    folder = tmp_path / "x’$(Set-Content ($HOME+[char]92+'ktx_inj_marker.txt') 1)’ O’Brien it's"
    folder.mkdir()
    exe, new, old = folder / "ktx.exe", folder / "ktx-new.exe", folder / "ktx.old"
    new.write_bytes(b"NEW-EXE")
    proc = running_fake_exe(exe, 3)
    try:
        run_helper(update.helper_script(proc.pid, exe, new, old, sha256=sha(b"NEW-EXE"), start=False))
        assert not marker.exists(), "폴더 이름에 든 명령이 실행됐다"
        assert exe.read_bytes() == b"NEW-EXE" and old.exists(), run_helper.output
    finally:
        marker.unlink(missing_ok=True)
