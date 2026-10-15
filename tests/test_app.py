"""웹 API 단위 테스트 — 실제 서버·코레일·키체인 없이 FastAPI TestClient 로 검증한다.

실행:  uv run pytest tests/test_app.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ktx import store

TOMORROW = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
JOB = {"dep": "서울", "arr": "부산", "date": TOMORROW, "time_from": "08:00", "time_to": "10:00"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)  # 앱 모듈을 처음 불러오기 전에 데이터 폴더를 바꿔 실제 폴더를 건드리지 않는다
    from ktx import app as app_module

    monkeypatch.setattr(app_module.config, "get", lambda name: None)  # 키체인을 읽지 않는다
    monkeypatch.setattr(app_module.update, "available", None)
    app_module.monitor.notify_error = None  # 앱 모듈은 테스트 사이에 공유되는 싱글턴이다
    client = TestClient(app_module.app, base_url="http://localhost")  # with 로 열지 않아 감시 스레드가 뜨지 않는다
    client.mod = app_module
    return client


# ---------------------------------------------------------------- 감시 작업 입력 검증
def test_valid_job_is_stored(api):
    r = api.post("/api/jobs", json=JOB)
    assert r.status_code == 200 and r.json()["status"] == "watching"
    assert len(api.mod.monitor.jobs.all()) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"date": "2026-13-45"},  # 정규식은 통과하지만 없는 날짜
        {"date": "2026-02-30"},
        {"time_from": "99:99"},
        {"time_to": "24:00"},
        {"time_from": "8:00"},
    ],
)
def test_impossible_dates_and_times_are_rejected_before_they_are_saved(api, bad):
    """저장되면 매 조회마다 strptime 이 터져 모든 감시가 10초씩 밀린다."""
    r = api.post("/api/jobs", json={**JOB, **bad})
    assert r.status_code == 422
    assert api.mod.monitor.jobs.all() == []


def test_manual_resume_resets_the_auto_resume_counter(api):
    j = api.mod.monitor.jobs.add({**JOB, "option": "GENERAL_FIRST", "train_type": "KTX", "adults": 1})
    api.mod.monitor.jobs.update(j["id"], status="done", result=None, resumes=2)
    assert api.post(f"/api/jobs/{j['id']}/resume").status_code == 200
    stored = api.mod.monitor.jobs.all()[0]
    assert stored["status"] == "watching" and stored["resumes"] == 0


# ---------------------------------------------------------------- 상태
def test_state_exposes_version_update_and_notify_error(api, monkeypatch):
    s = api.get("/api/state").json()
    assert s["version"] == "dev" and s["update"] is None and s["notify_error"] is None
    api.mod.monitor.notify_error = "디스코드 전송 실패 (404): Unknown Webhook"
    monkeypatch.setattr(api.mod.update, "available", {"version": "9.9.9", "url": "https://github.com/JayChoi07/ktx/releases/tag/v9.9.9"})
    s = api.get("/api/state").json()
    assert "404" in s["notify_error"] and s["update"]["version"] == "9.9.9"


def test_discord_test_updates_the_failure_state(api, monkeypatch):
    def boom(text, **kw):
        raise RuntimeError("디스코드 전송 실패 (404): Unknown Webhook")

    monkeypatch.setattr(api.mod.discord, "send", boom)
    assert api.post("/api/discord/test").status_code == 400
    assert "404" in api.mod.monitor.notify_error
    monkeypatch.setattr(api.mod.discord, "send", lambda text, **kw: None)
    assert api.post("/api/discord/test").status_code == 200
    assert api.mod.monitor.notify_error is None


# ---------------------------------------------------------------- 카카오 잔재 정리
def test_legacy_kakao_leftovers_are_removed(api, tmp_path, monkeypatch):
    removed = []
    monkeypatch.setattr(api.mod.config, "remove", lambda *names: removed.extend(names))
    (tmp_path / "kakao.json").write_text('{"refresh_token": "x"}', encoding="utf-8")
    api.mod._drop_legacy_kakao()
    assert removed == ["KAKAO_REST_API_KEY", "KAKAO_CLIENT_SECRET"]
    assert not (tmp_path / "kakao.json").exists()
    api.mod._drop_legacy_kakao()  # 이미 없어도 에러 없이 지나간다
