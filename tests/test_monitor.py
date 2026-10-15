"""감시·예약 로직 단위 테스트 — 가짜 코레일 클라이언트로 실제 예약 없이 핵심 경로를 검증한다.

실행:  uv run pytest tests/test_monitor.py -v
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from pykorail.exceptions import AccessRestrictedError, KorailError, LoginFailedError, NoResultsError, SoldOutError

from ktx import monitor as m
from ktx import store

TOMORROW = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
TOMORROW_YMD = TOMORROW.replace("-", "")


# ---------------------------------------------------------------- 가짜 코레일
class FakeTrain:
    def __init__(self, dep_time: str, *, general=False, special=False, no="001", dep_date=TOMORROW_YMD):
        self.dep_time, self.arr_time, self.train_no, self.dep_date = dep_time, "0930", no, dep_date
        self._g, self._s = general, special
        self.train_type_name, self.dep_name, self.arr_name, self.wait_reserve_flag = "KTX", "서울", "부산", 0

    def has_general_seat(self):
        return self._g

    def has_special_seat(self):
        return self._s

    def has_seat(self):
        return self._g or self._s

    def summary(self):
        return f"[KTX {self.train_no}] {self.dep_time}"


class FakeRsv:
    def __init__(self, rsv_id="R1", price=59800):
        self.rsv_id, self.price, self.seats = rsv_id, price, ()
        self.buy_limit_date, self.buy_limit_time = TOMORROW.replace("-", ""), "234500"


class FakeReservations:
    def __init__(self):
        self.created, self.cancelled, self.existing, self.raise_next = [], [], {}, []

    def create(self, train, passengers=None, option=None):
        if self.raise_next:
            raise self.raise_next.pop(0)
        self.created.append((train, passengers, option))
        return FakeRsv(rsv_id=f"R{len(self.created)}")

    def find(self, rsv_id):
        return self.existing.get(rsv_id)

    def cancel(self, rsv):
        self.cancelled.append(rsv)


class FakeTrains:
    def __init__(self):
        self.result = []

    def search(self, *a, **k):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeKorail:
    name = "테스트"

    def __init__(self):
        self.reservations, self.trains = FakeReservations(), FakeTrains()

    def close(self):
        pass


@pytest.fixture
def mon(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(m.discord, "configured", lambda: False)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    mon = m.Monitor()
    mon.korail = FakeKorail()
    mon.login_state = "로그인됨 (테스트)"
    mon.sent, mon.urgent = [], []

    def fake_notify(text, urgent=False):
        mon.sent.append(text)
        if urgent:
            mon.urgent.append(text)

    monkeypatch.setattr(mon, "notify", fake_notify)
    return mon


def add_job(mon, **over):
    base = {"dep": "서울", "arr": "부산", "date": TOMORROW, "time_from": "06:00", "time_to": "10:00"}
    return mon.jobs.add({**base, "option": "GENERAL_FIRST", "train_type": "KTX", "adults": 1, **over})


def job(mon, job_id):
    return next(j for j in mon.jobs.all() if j["id"] == job_id)


# ---------------------------------------------------------------- 예약
def test_reserves_first_train_with_seat_and_notifies(mon):
    j = add_job(mon, adults=2)
    mon._try_reserve(j, [FakeTrain("0700"), FakeTrain("0730", general=True, no="083"), FakeTrain("0800", general=True)])
    created = mon.korail.reservations.created
    assert len(created) == 1 and created[0][0].train_no == "083"
    assert created[0][1][0].count == 2 and created[0][2] == "GENERAL_FIRST"
    r = job(mon, j["id"])
    assert r["status"] == "reserved"
    assert r["result"]["train"] == "KTX 083 서울→부산 07:30~09:30" and r["result"]["price"] == 59800
    assert r["result"]["buy_limit_iso"] == f"{TOMORROW}T23:45:00"
    assert mon.sent and "[KTX 예약 성공]" in mon.sent[0] and "59,800원" in mon.sent[0]
    assert mon.urgent == [mon.sent[0]]  # 예약 성공만 @everyone 으로 울린다


def test_general_only_ignores_special_seats(mon):
    j = add_job(mon, option="GENERAL_ONLY")
    mon._try_reserve(j, [FakeTrain("0700", special=True)])
    assert not mon.korail.reservations.created and job(mon, j["id"])["status"] == "watching"
    mon._try_reserve(j, [FakeTrain("0700", special=True), FakeTrain("0730", general=True)])
    assert mon.korail.reservations.created[0][0].dep_time == "0730"


def test_special_only_ignores_general_seats(mon):
    j = add_job(mon, option="SPECIAL_ONLY")
    mon._try_reserve(j, [FakeTrain("0700", general=True)])
    assert not mon.korail.reservations.created


def test_respects_date_and_time_window(mon):
    j = add_job(mon, time_from="08:00", time_to="09:00")
    other_day = FakeTrain("0830", general=True, dep_date="20990101")
    mon._try_reserve(j, [FakeTrain("0700", general=True), FakeTrain("0930", general=True), other_day])
    assert not mon.korail.reservations.created
    mon._try_reserve(j, [FakeTrain("0900", general=True)])  # 경계 포함
    assert len(mon.korail.reservations.created) == 1


def test_does_not_double_book_train_already_reserved_by_another_job(mon):
    a, b = add_job(mon), add_job(mon)
    trains = [FakeTrain("0700", general=True, no="083")]
    mon._try_reserve(a, trains)
    mon._try_reserve(b, trains)
    assert len(mon.korail.reservations.created) == 1
    assert job(mon, a["id"])["status"] == "reserved" and job(mon, b["id"])["status"] == "watching"


def test_sold_out_at_reserve_time_moves_on(mon):
    j = add_job(mon)
    mon.korail.reservations.raise_next = [SoldOutError("매진")]
    mon._try_reserve(j, [FakeTrain("0700", general=True), FakeTrain("0730", general=True)])
    assert len(mon.korail.reservations.created) == 1 and mon.korail.reservations.created[0][0].dep_time == "0730"
    assert job(mon, j["id"])["status"] == "reserved"


def test_access_restricted_pauses_and_warns(mon):
    j = add_job(mon)
    mon.korail.reservations.raise_next = [AccessRestrictedError(403, "이용제한", "-2000")]
    mon._try_reserve(j, [FakeTrain("0700", general=True)])
    assert mon.paused_until > time.time() + 500
    assert job(mon, j["id"])["status"] == "watching"
    assert any("접속 제한" in t for t in mon.sent)
    assert any("접속 제한" in t for t in mon.urgent)  # 감시가 멈춘 걸 놓치지 않도록 @everyone


def test_block_pings_everyone_only_once_per_episode(mon):
    e = AccessRestrictedError(403, "이용제한", "-2000")
    mon._blocked(e)
    mon._blocked(e)  # 10분 뒤에도 계속 막혀 있는 경우: 알림은 가되 또 울리지는 않는다
    assert len(mon.sent) == 2 and len(mon.urgent) == 1
    # 정상 조회가 한 번 되면 에피소드가 끝나고, 다음 제한은 다시 울린다
    mon.paused_until = 0
    j = add_job(mon)
    mon.korail.trains.result = NoResultsError()
    mon._check_group("서울", "부산", TOMORROW, "KTX", [j])
    mon._blocked(e)
    assert len(mon.urgent) == 2


def test_login_failure_pauses_for_an_hour_and_pings_everyone(mon, monkeypatch):
    class BadKorail:
        def __init__(self, **kw):
            pass

        def login(self, uid, pw):
            raise LoginFailedError("틀림")

        def close(self):
            pass

    monkeypatch.setattr(m.config, "get", lambda name: "x")
    monkeypatch.setattr(m, "Korail", BadKorail)
    mon.korail = None
    assert mon._login() is False
    assert mon.paused_until > time.time() + 3500
    assert len(mon.urgent) == 1 and "로그인에 실패" in mon.urgent[0]


def test_other_korail_error_is_logged_and_continues(mon):
    j = add_job(mon)
    mon.korail.reservations.raise_next = [KorailError("알 수 없는 오류", "X1")]
    mon._try_reserve(j, [FakeTrain("0700", general=True), FakeTrain("0730", general=True)])
    assert len(mon.korail.reservations.created) == 1
    assert any("예약 거부" in entry["msg"] for entry in mon.logs)


# ---------------------------------------------------------------- 결제 재알림 / 취소
def test_remind_marks_done_when_reservation_disappears(mon):
    j = add_job(mon)
    mon._try_reserve(j, [FakeTrain("0700", general=True)])
    mon._reminders[j["id"]] = time.time() - m.REMIND_EVERY - 1  # 재알림 주기가 지난 뒤
    mon._remind([job(mon, j["id"])])  # 코레일 목록에 R1 이 없음 → 결제 완료 또는 만료
    assert job(mon, j["id"])["status"] == "done"
    assert any("목록에서 사라짐" in entry["msg"] for entry in mon.logs)


def test_remind_renotifies_only_after_interval(mon, monkeypatch):
    j = add_job(mon)
    mon._try_reserve(j, [FakeTrain("0700", general=True)])
    mon.korail.reservations.existing["R1"] = FakeRsv("R1")
    sent_before = len(mon.sent)
    mon._remind([job(mon, j["id"])])  # 예약 직후라 간격 미달
    assert len(mon.sent) == sent_before
    mon._reminders[j["id"]] = time.time() - m.REMIND_EVERY - 1
    mon._remind([job(mon, j["id"])])
    assert len(mon.sent) == sent_before + 1 and "[KTX 결제 대기]" in mon.sent[-1]
    assert mon.urgent == [mon.sent[0]]  # 재알림은 멘션 없이
    assert job(mon, j["id"])["status"] == "reserved"


def _reserve_and_age_reminder(mon):
    j = add_job(mon)
    mon._try_reserve(j, [FakeTrain("0700", general=True)])
    mon._reminders[j["id"]] = time.time() - m.REMIND_EVERY - 1  # 재알림 주기가 지난 상태
    return j


def test_remind_keeps_going_while_paused_without_calling_korail(mon):
    """접속 제한으로 멈춘 10분 동안에도 결제 재알림은 가야 한다. 다만 막힌 코레일을 또 부르면 안 된다."""
    _reserve_and_age_reminder(mon)
    mon.paused_until = time.time() + 600
    asked = []
    mon.korail.reservations.find = lambda rid: asked.append(rid)
    sent_before = len(mon.sent)
    mon._tick()
    assert asked == []
    assert len(mon.sent) == sent_before + 1 and "[KTX 결제 대기]" in mon.sent[-1]
    assert "확인하지 못했어요" in mon.sent[-1]


def test_paused_remind_stops_after_payment_deadline(mon):
    j = _reserve_and_age_reminder(mon)
    past = (datetime.now() - timedelta(minutes=1)).isoformat()
    mon.jobs.update(j["id"], result={**job(mon, j["id"])["result"], "buy_limit_iso": past})
    mon.paused_until = time.time() + 600
    sent_before = len(mon.sent)
    mon._tick()
    assert len(mon.sent) == sent_before  # 기한이 지났으면 더 알리지 않는다
    assert job(mon, j["id"])["status"] == "reserved"  # 사라졌는지는 접속이 풀린 뒤 확인한다


def test_cancel_reservation(mon):
    j = add_job(mon)
    mon._try_reserve(j, [FakeTrain("0700", general=True)])
    rsv = FakeRsv("R1")
    mon.korail.reservations.existing["R1"] = rsv
    mon.cancel_reservation(j["id"])
    assert mon.korail.reservations.cancelled == [rsv]


# ---------------------------------------------------------------- 루프
def test_tick_expires_past_date_and_past_window(mon):
    now = datetime.now()
    if now.strftime("%H:%M") == "00:00":
        pytest.skip("정각 자정에는 '오늘 지난 시간대'를 만들 수 없음")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    past = add_job(mon, date=yesterday)
    gone = add_job(mon, date=now.strftime("%Y-%m-%d"), time_from="00:00", time_to="00:00")  # 오늘, 이미 지난 시간대
    live = add_job(mon)
    mon._tick()
    assert job(mon, past["id"])["status"] == "expired"
    assert job(mon, gone["id"])["status"] == "expired"
    assert job(mon, live["id"])["status"] == "watching"


def test_tick_counts_checks_even_with_no_results(mon):
    j = add_job(mon)
    mon.korail.trains.result = NoResultsError()
    mon._tick()
    r = job(mon, j["id"])
    assert r["checks"] == 1 and r["trains"] == [] and r["last_checked_ts"] > 0


def test_tick_groups_same_route_and_snapshots_trains(mon):
    a, b = add_job(mon, time_from="06:00", time_to="08:00"), add_job(mon, time_from="08:00", time_to="10:00")
    mon.korail.trains.result = [FakeTrain("0700"), FakeTrain("0830", special=True), FakeTrain("1100", general=True)]
    mon._tick()
    assert job(mon, a["id"])["trains"] == [{"t": "07:00", "no": "KTX 001", "g": False, "s": False}]
    assert job(mon, b["id"])["trains"] == [{"t": "08:30", "no": "KTX 001", "g": False, "s": True}]
    assert job(mon, b["id"])["status"] == "reserved"  # 08:30 특실 → GENERAL_FIRST 는 특실도 잡는다
    assert job(mon, a["id"])["status"] == "watching"


def test_tick_is_noop_while_paused(mon):
    add_job(mon)
    mon.paused_until = time.time() + 60
    mon.korail.trains.result = [FakeTrain("0700", general=True)]
    mon._tick()
    assert not mon.korail.reservations.created


# ---------------------------------------------------------------- 디스코드 알림
@pytest.fixture
def real_notify(tmp_path, monkeypatch):
    """notify 를 가로채지 않는 모니터. 디스코드 전송만 기록한다."""
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    mon = m.Monitor()
    mon.korail = FakeKorail()
    mon.login_state = "로그인됨 (테스트)"
    mon.posts = []
    monkeypatch.setattr(m.discord, "configured", lambda: True)
    monkeypatch.setattr(m.discord, "send", lambda text, mention_everyone=False: mon.posts.append((text, mention_everyone)))
    return mon


def test_notify_tags_account_name_inside_the_bracket(real_notify):
    real_notify.notify("[KTX 예약 성공]\n본문", urgent=True)
    real_notify.notify("[KTX] 안내")
    assert real_notify.posts == [("[KTX 예약 성공 · 테스트]\n본문", True), ("[KTX · 테스트] 안내", False)]


def test_notify_without_login_has_no_name_tag(real_notify):
    real_notify.login_state = "로그인 실패"
    real_notify.notify("[KTX] 로그인 실패")
    assert real_notify.posts == [("[KTX] 로그인 실패", False)]


def test_notify_when_not_configured_only_logs(real_notify, monkeypatch):
    monkeypatch.setattr(m.discord, "configured", lambda: False)
    real_notify.notify("[KTX 예약 성공]", urgent=True)
    assert real_notify.posts == []
    assert any("디스코드 미설정" in e["msg"] and e["level"] == "warn" for e in real_notify.logs)


def test_notify_failure_is_logged_not_raised(real_notify, monkeypatch):
    def boom(text, mention_everyone=False):
        raise RuntimeError("디스코드 전송 실패 (429): rate limited")

    monkeypatch.setattr(m.discord, "send", boom)
    real_notify.notify("[KTX 예약 성공]", urgent=True)  # 알림이 실패해도 예약 흐름은 계속돼야 한다
    assert any("디스코드 전송 실패" in e["msg"] and e["level"] == "error" for e in real_notify.logs)


# ---------------------------------------------------------------- 안내문 / 저장소
def test_friendly_messages():
    assert "접속을 제한" in m.friendly(AccessRestrictedError(403, "x", "-2000"))
    assert "로그인에 실패" in m.friendly(LoginFailedError("틀림"))
    assert "HTTP 503" in m.friendly(m.HttpStatusError(503, None))
    ConnectError = type("ConnectError", (Exception,), {})  # curl_cffi/requests 계열 흉내
    assert "인터넷 연결" in m.friendly(ConnectError("boom"))
    assert m.friendly(KorailError("그대로", "Z")) == "그대로 (Z)"


def test_jobs_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    jobs = store.Jobs()
    j = jobs.add({"dep": "서울", "arr": "부산"})
    assert jobs.all()[0]["id"] == j["id"] and j["status"] == "watching" and j["checks"] == 0
    assert jobs.update(j["id"], status="stopped")["status"] == "stopped"
    assert jobs.update("nope", status="x") is None
    jobs.remove(j["id"])
    assert jobs.all() == []


def test_interval_limits(mon):
    mon.set_interval(45)
    assert mon.settings["interval"] == 45
    for bad in (0, 9.9, -1, float("nan"), 3601):
        with pytest.raises(ValueError):
            mon.set_interval(bad)
    assert mon.settings["interval"] == 45
    # 예전 버전이 저장한 8초는 로드할 때 최소치로 올라간다
    store.save("settings.json", {"interval": 8})
    assert m.Monitor().settings["interval"] == m.MIN_INTERVAL == 10
