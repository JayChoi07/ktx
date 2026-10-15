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
    monkeypatch.setattr(app_module.update, "install_state", {"phase": "idle", "done": 0, "total": 0, "error": None})
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


# ---------------------------------------------------------------- 업데이트 (확인 / 설치)
NEWER = {
    "version": "9.9.9",
    "url": "https://github.com/JayChoi07/ktx/releases/tag/v9.9.9",
    "asset": {"url": "https://github.com/JayChoi07/ktx/releases/download/v9.9.9/ktx.exe", "sha256": "a" * 64, "size": 3},
}


@pytest.fixture
def upd(api, monkeypatch):
    """새 버전이 있고 설치 가능한 상태."""
    monkeypatch.setattr(api.mod.update, "available", NEWER)
    monkeypatch.setattr(api.mod.update, "can_install", lambda: (True, None))
    monkeypatch.setattr(api.mod.update, "install_state", {"phase": "idle", "done": 0, "total": 0, "error": None})
    return api


def reserve(api) -> None:
    j = api.mod.monitor.jobs.add({**JOB, "option": "GENERAL_FIRST", "train_type": "KTX", "adults": 1})
    api.mod.monitor.jobs.update(j["id"], status="reserved", result={"rsv_id": "R1"})


def test_state_tells_the_page_whether_it_can_install_without_leaking_internals(upd, monkeypatch):
    u = upd.get("/api/state").json()["update"]
    assert u == {"version": "9.9.9", "url": NEWER["url"], "installable": True, "why_not": None}  # 내려받기 주소·해시는 화면에 안 내보낸다
    monkeypatch.setattr(upd.mod.update, "can_install", lambda: (False, "exe 로 실행 중일 때만 가능해요"))
    u = upd.get("/api/state").json()["update"]
    assert u["installable"] is False and "exe" in u["why_not"]


def test_install_is_blocked_while_a_payment_is_pending(upd):
    """재시작 중에는 결제 재알림이 끊기고 예약 확인도 늦어진다 — 결제를 마친 뒤에 하게 한다."""
    reserve(upd)
    u = upd.get("/api/state").json()["update"]
    assert u["installable"] is False and "결제" in u["why_not"]
    r = upd.post("/api/update/install")
    assert r.status_code == 409 and "결제" in r.json()["detail"]


def test_state_exposes_install_progress(upd, monkeypatch):
    monkeypatch.setattr(upd.mod.update, "install_state", {"phase": "downloading", "done": 10, "total": 100, "error": None})
    assert upd.get("/api/state").json()["update_install"] == {"phase": "downloading", "done": 10, "total": 100, "error": None}


def test_install_endpoint_starts_the_install_with_an_exit_hook_and_the_booking_gate(upd, monkeypatch):
    started = []
    monkeypatch.setattr(upd.mod.update, "start_install", lambda cb, hold=None, release=None: started.append((cb, hold, release)) or True)
    assert upd.post("/api/update/install").json() == {"ok": True}
    exit_cb, hold, release = started[0]
    assert hold == upd.mod.monitor.hold_for_restart and release == upd.mod.monitor.release_restart  # 교체 직전 예약 게이트 (astra 리뷰)
    exited = []
    monkeypatch.setattr(upd.mod, "exit_app", lambda: exited.append(1))  # run.py 가 나중에 바꿔 끼운 종료 함수도 따라간다
    exit_cb()
    assert exited == [1]


def test_state_carries_a_per_process_boot_id(api):
    """화면이 서버가 재시작됐는지 알아채는 기준 — 끊김을 못 봐도 새 프로세스면 다른 값이다 (astra 리뷰)."""
    a, b = api.get("/api/state").json()["boot"], api.get("/api/state").json()["boot"]
    assert a == b and len(a) >= 16


def test_install_endpoint_rejects_what_it_cannot_do(upd, monkeypatch):
    monkeypatch.setattr(upd.mod.update, "start_install", lambda exit_cb, hold=None, release=None: False)
    r = upd.post("/api/update/install")
    assert r.status_code == 409 and "진행 중" in r.json()["detail"]
    monkeypatch.setattr(upd.mod.update, "can_install", lambda: (False, "이 릴리스는 자동 설치용 정보가 없어요"))
    r = upd.post("/api/update/install")
    assert r.status_code == 409 and "정보" in r.json()["detail"]
    monkeypatch.setattr(upd.mod.update, "available", None)  # 새 버전이 없다
    assert upd.post("/api/update/install").status_code == 400


def test_check_endpoint_reports_the_outcome(api, monkeypatch):
    monkeypatch.setattr(api.mod.update, "check_now", lambda: {"status": "latest", "update": None, "error": None, "latest": "1.4.1"})
    assert api.post("/api/update/check").json()["status"] == "latest"
