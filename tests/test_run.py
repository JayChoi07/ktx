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


def test_exe_mode_logs_ktx_history_to_a_size_limited_file(tmp_path):
    env = {**os.environ, "KTX_DATA_DIR": str(tmp_path), "PYTHONUTF8": "1"}
    out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=True)
    text = (tmp_path / "ktx.log").read_text(encoding="utf-8")
    # 예약·로그인 같은 정상 이력도 남아야 장애 때 원인을 추적할 수 있다 (예전엔 WARNING 이상만 남아 파일이 비어 있었다)
    assert "예약 성공 기록" in text and "조회 오류 기록" in text
    assert "무관한 INFO" not in text  # 다른 라이브러리의 INFO 는 남기지 않는다
    assert out.stdout.strip() == "RotatingFileHandler"  # 무한히 커지지 않는다
