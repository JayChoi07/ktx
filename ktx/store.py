"""JSON 파일 기반 저장소 (작업 목록, 설정, 기기 프로파일)."""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any


def _data_dir() -> Path:
    if override := os.environ.get("KTX_DATA_DIR"):
        return Path(override)
    if getattr(sys, "frozen", False):  # exe 로 배포된 경우: 사용자 폴더에 저장
        return Path(os.environ.get("APPDATA", Path.home())) / "ktx-macro"
    return Path(__file__).resolve().parent.parent / "data"


DATA_DIR = _data_dir()
_lock = threading.RLock()


def _path(name: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / name


def load(name: str, default: Any) -> Any:
    p = _path(name)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save(name: str, value: Any) -> None:
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


class Jobs:
    """감시 작업 목록. 모든 접근은 락 안에서 이루어진다."""

    FILE = "jobs.json"

    def all(self) -> list[dict]:
        with _lock:
            return load(self.FILE, [])

    def add(self, job: dict) -> dict:
        with _lock:
            jobs = self.all()
            job = {**job, "id": uuid.uuid4().hex[:8], "status": "watching", "checks": 0, "last_checked": None, "result": None}
            jobs.append(job)
            save(self.FILE, jobs)
            return job

    def update(self, job_id: str, **fields: Any) -> dict | None:
        with _lock:
            jobs = self.all()
            for j in jobs:
                if j["id"] == job_id:
                    j.update(fields)
                    save(self.FILE, jobs)
                    return j
            return None

    def remove(self, job_id: str) -> None:
        with _lock:
            save(self.FILE, [j for j in self.all() if j["id"] != job_id])
