"""트레이(exe) 모드의 파일 로깅 테스트 — 전역 로거를 건드리므로 서브프로세스로 격리해 검증한다.

실행:  uv run pytest tests/test_run.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = """
import logging
import run

run._setup_file_logging()
logging.getLogger("ktx").info("예약 성공 기록")
logging.getLogger("ktx").warning("조회 오류 기록")
logging.getLogger("other").info("무관한 INFO")
print(type(logging.getLogger().handlers[0]).__name__)
"""


def test_stale_downloads_go_at_startup_but_the_old_exe_is_kept(tmp_path, monkeypatch):
    """시작하는 순간엔 진행 중인 설치가 있을 수 없어 받다 만 파일은 바로 지운다. 복구용 ktx.old 는 건드리지 않는다."""
    import run

    exe = tmp_path / "ktx.exe"
    for name in ("ktx.exe", "ktx.old", "ktx-new.exe", "ktx-new.exe.part", "keep-me.txt"):
        (tmp_path / name).write_bytes(b"x")
    run._remove_stale_downloads(exe)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep-me.txt", "ktx.exe", "ktx.old"]
    run._remove_stale_downloads(exe)  # 이미 없어도 에러 없이 지나간다

    def locked(self, *a, **k):
        raise PermissionError(13, "in use")

    (tmp_path / "ktx-new.exe").write_bytes(b"x")
    monkeypatch.setattr(type(exe), "unlink", locked)
    run._remove_stale_downloads(exe)  # 잠겨 있어도 시작을 막지 않는다


def test_old_exe_is_removed_only_once_the_new_one_is_actually_serving(tmp_path, monkeypatch):
    """시작에 실패해 오류 상자가 떠 있는 동안 복구용 ktx.old 를 지우면 안 된다 (astra 리뷰)."""
    import run

    exe = tmp_path / "ktx.exe"
    old = tmp_path / "ktx.old"
    old.write_bytes(b"x")
    monkeypatch.setattr(run, "_already_running", lambda: False)  # 서버가 안 떴다
    run._remove_old_exe_when_serving(exe)
    assert old.exists()
    monkeypatch.setattr(run, "_already_running", lambda: True)  # 서버가 응답한다
    run._remove_old_exe_when_serving(exe)
    assert not old.exists()
    run._remove_old_exe_when_serving(exe)  # 이미 없어도 에러 없이 지나간다


def test_updated_restart_does_not_open_another_browser_tab():
    import run

    assert run._should_auto_open(["ktx.exe"]) is True
    assert run._should_auto_open(["ktx.exe", "--updated"]) is False  # 이미 열려 있는 탭이 알아서 다시 연결한다


NO_CONSOLE_SCRIPT = """
import logging, sys

real = sys.stdout
sys.stdout = sys.stderr = None  # 콘솔 없는 exe(console=False) 와 같은 상태
import run

run._setup_file_logging()
print("stdout 한 줄")
sys.stderr.write("stderr 한 줄\\n")
for _ in range(1500):
    logging.getLogger("ktx").info("x" * 1000)  # 1MB 를 넘겨 회전을 일으킨다
real.write("done\\n")
"""


def test_rotation_works_without_a_console_and_stdout_ends_up_in_the_log(tmp_path):
    """stdout/stderr 용으로 같은 파일을 따로 열면 Windows 에서 이름 변경(회전)이 PermissionError 로 막혀 파일이 계속 커진다 (astra 리뷰)."""
    env = {**os.environ, "KTX_DATA_DIR": str(tmp_path), "PYTHONUTF8": "1"}
    out = subprocess.run([sys.executable, "-c", NO_CONSOLE_SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip() == "done", out.stderr
    files = sorted(tmp_path.glob("ktx.log*"))
    assert [f.name for f in files][:2] == ["ktx.log", "ktx.log.1"]  # 실제로 회전됐다
    assert max(f.stat().st_size for f in files) < 1_300_000  # 한없이 커지지 않는다
    text = "".join(f.read_text(encoding="utf-8") for f in files)
    assert "stdout 한 줄" in text and "stderr 한 줄" in text  # 콘솔이 없어도 print/예외 출력이 로그에 남는다
    assert "PermissionError" not in text


def test_exe_mode_logs_ktx_history_to_a_size_limited_file(tmp_path):
    env = {**os.environ, "KTX_DATA_DIR": str(tmp_path), "PYTHONUTF8": "1"}
    out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=True)
    text = (tmp_path / "ktx.log").read_text(encoding="utf-8")
    # 예약·로그인 같은 정상 이력도 남아야 장애 때 원인을 추적할 수 있다 (예전엔 WARNING 이상만 남아 파일이 비어 있었다)
    assert "예약 성공 기록" in text and "조회 오류 기록" in text
    assert "무관한 INFO" not in text  # 다른 라이브러리의 INFO 는 남기지 않는다
    assert out.stdout.strip() == "RotatingFileHandler"  # 무한히 커지지 않는다
