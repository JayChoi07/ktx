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


class FakeTicket:
    """발권된 승차권 한 장 (좌석 하나). 같은 예약의 승차권은 pnr_no 가 같다."""

    def __init__(self, pnr_no, car_no="10", seat_no="1B", price=48600):
        self.pnr_no, self.car_no, self.seat_no, self.price = pnr_no, car_no, seat_no, price


class FakeTickets:
    """발권된 승차권 내역 (korail.tickets)."""

    def __init__(self):
        self.items, self.error = [], None

    def all(self):
        if self.error:
            raise self.error
        return self.items


class PagedTrains:
    """depart_after 이후 열차를 10편씩 돌려주는 가짜 시간표. 경계 열차가 다음 쪽에 또 나오는 것도 실제 pykorail 과 같다."""

    def __init__(self, minutes):
        self.all = [FakeTrain(f"{m // 60:02d}{m % 60:02d}", no=f"{i:03d}") for i, m in enumerate(minutes)]
        self.calls = []

    def search(self, dep, arr, depart_after=None, max_pages=1, **kw):
        assert max_pages == 1  # 쪽 수는 우리가 시각 기준으로 직접 이어 받는다
        self.calls.append(depart_after)
        after = depart_after.strftime("%H%M")
        page = [t for t in self.all if t.dep_time >= after][:10]
        if not page:
            raise NoResultsError()
        return page


class FakeKorail:
    name = "테스트"

    def __init__(self):
        self.reservations, self.trains, self.tickets = FakeReservations(), FakeTrains(), FakeTickets()

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


def test_block_alerts_are_not_repeated_every_pause(mon):
    e = AccessRestrictedError(403, "이용제한", "-2000")
    mon._blocked(e)
    mon._blocked(e)  # 10분 뒤에도 계속 막혀 있어도 같은 알림을 또 보내지 않는다
    assert len(mon.sent) == 1 and len(mon.urgent) == 1
    mon._block_notified_at -= m.BLOCK_REPEAT  # 1시간이 지나면 아직 막혀 있다고 한 번 더 알린다 (멘션 없이)
    mon._blocked(e)
    assert len(mon.sent) == 2 and len(mon.urgent) == 1
    # 정상 조회가 한 번 되면 에피소드가 끝나고, 다음 제한은 다시 울린다
    mon.paused_until = 0
    j = add_job(mon)
    mon.korail.trains.result = NoResultsError()
    mon._check_group("서울", "부산", TOMORROW, "KTX", [j])
    mon._blocked(e)
    assert len(mon.sent) == 3 and len(mon.urgent) == 2


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


# ---------------------------------------------------------------- 조회 범위 (감시 창 끝까지)
def _every_20_min():
    return [h * 60 + m for h in range(5, 24) for m in (0, 20, 40)]  # 05:00 ~ 23:40, 57편


def test_search_pages_until_the_window_end_not_a_fixed_page_count(mon):
    """종일 감시가 앞쪽 30편에서 잘려 오후·저녁 열차를 못 보던 버그."""
    mon.korail.trains = PagedTrains(_every_20_min())
    j = add_job(mon, time_from="00:00", time_to="23:59")
    mon._check_group("서울", "부산", TOMORROW, "KTX", [j])
    snap = job(mon, j["id"])["trains"]
    assert len(snap) == 57 and snap[-1]["t"] == "23:40"
    assert 3 < len(mon.korail.trains.calls) < m.MAX_SEARCH_PAGES


def test_search_stops_once_the_window_end_is_reached(mon):
    mon.korail.trains = PagedTrains(_every_20_min())
    j = add_job(mon, time_from="08:00", time_to="10:00")
    mon._check_group("서울", "부산", TOMORROW, "KTX", [j])
    assert len(mon.korail.trains.calls) == 1  # 08:00 부터 한 쪽이면 10:00 을 넘는다 (예전엔 항상 3쪽을 받았다)
    assert [t["t"] for t in job(mon, j["id"])["trains"]] == ["08:00", "08:20", "08:40", "09:00", "09:20", "09:40", "10:00"]


def test_search_page_count_is_capped(mon):
    mon.korail.trains = PagedTrains(list(range(0, 24 * 60, 3)))  # 3분 간격 480편
    j = add_job(mon, time_from="00:00", time_to="23:59")
    mon._check_group("서울", "부산", TOMORROW, "KTX", [j])
    assert len(mon.korail.trains.calls) == m.MAX_SEARCH_PAGES


def test_search_covers_the_latest_job_in_the_group(mon):
    """같은 노선·날짜의 감시는 한 번에 조회한다 — 가장 늦은 시간대까지 받아야 한다."""
    mon.korail.trains = PagedTrains(_every_20_min())
    a, b = add_job(mon, time_from="06:00", time_to="08:00"), add_job(mon, time_from="20:00", time_to="22:00")
    mon._check_group("서울", "부산", TOMORROW, "KTX", [a, b])
    assert job(mon, b["id"])["trains"][-1]["t"] == "22:00"


# ---------------------------------------------------------------- 중지·삭제와 예약의 경합
def test_stopped_job_is_not_reserved(mon):
    j = add_job(mon)
    mon.jobs.update(j["id"], status="stopped")  # 조회하는 사이 사용자가 중지를 눌렀다 (j 는 그 전의 스냅샷)
    mon._try_reserve(j, [FakeTrain("0700", general=True)])
    assert not mon.korail.reservations.created and not mon.sent


def test_job_deleted_while_reserving_cancels_the_orphan(mon):
    j = add_job(mon)
    rs = mon.korail.reservations
    create = rs.create

    def create_then_delete(*a, **k):
        rsv = create(*a, **k)
        mon.jobs.remove(j["id"])  # 예약이 나가는 사이 사용자가 삭제했다
        return rsv

    rs.create = create_then_delete
    mon._try_reserve(j, [FakeTrain("0700", general=True)])
    assert [r.rsv_id for r in rs.cancelled] == ["R1"]  # 추적할 수 없는 예약이 남지 않게 바로 취소
    assert not mon.sent and mon.jobs.all() == []


# ---------------------------------------------------------------- 결제 재알림 / 취소
def test_remind_marks_paid_when_reservation_disappears_and_ticket_exists(mon):
    j = add_job(mon)
    mon._try_reserve(j, [FakeTrain("0700", general=True)])
    mon._reminders[j["id"]] = time.time() - m.REMIND_EVERY - 1  # 재알림 주기가 지난 뒤
    mon.korail.tickets.items = [FakeTicket("R1")]  # 코레일 예약 목록에는 R1 이 없고, 발권 내역에는 있다
    mon._remind([job(mon, j["id"])])
    assert job(mon, j["id"])["status"] == "paid"
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


def _reserved_job(mon, limit_in: timedelta, **job_fields):
    """예약이 잡힌 작업을 만들고 결제기한을 limit_in 뒤로 맞춘다. 재알림 주기는 이미 지난 상태."""
    j = add_job(mon)
    mon._try_reserve(j, [FakeTrain("0700", general=True)])
    mon.jobs.update(j["id"], result={**job(mon, j["id"])["result"], "buy_limit_iso": (datetime.now() + limit_in).isoformat()}, **job_fields)
    mon._reminders[j["id"]] = time.time() - m.REMIND_EVERY - 1
    mon.sent.clear()
    mon.urgent.clear()
    return j


def test_paid_reservation_becomes_completed_and_discord_says_so(mon):
    """결제까지 끝난 건은 '종료'가 아니라 '예약 완료'로 — 열차·좌석·금액과 함께."""
    j = _reserved_job(mon, timedelta(minutes=5))
    mon.korail.tickets.items = [FakeTicket("R1", "10", "1B", 48600), FakeTicket("R1", "16", "1C", 48600), FakeTicket("OTHER", "3", "9A", 1)]
    mon._remind([job(mon, j["id"])])
    r = job(mon, j["id"])
    assert r["status"] == "paid"
    assert r["result"]["paid"] == {"seats": ["10호차 1B", "16호차 1C"], "price": 97200}  # 다른 예약의 승차권은 섞이지 않는다
    assert r["result"]["train"] == "KTX 001 서울→부산 07:00~09:30"  # 예약 때의 정보는 그대로
    assert len(mon.sent) == 1 and mon.urgent == []  # 확인 알림이라 멘션은 없다
    msg = mon.sent[0]
    assert "[KTX 예약 완료]" in msg and "KTX 001" in msg and "10호차 1B" in msg and "16호차 1C" in msg and "97,200원" in msg
    assert j["id"] not in mon._reminders


def test_paid_after_the_deadline_is_still_recognized(mon):
    j = _reserved_job(mon, timedelta(minutes=-1))  # 기한 직전에 결제하고 기한 뒤에 확인한 경우
    mon.korail.tickets.items = [FakeTicket("R1")]
    mon._remind([job(mon, j["id"])])
    assert job(mon, j["id"])["status"] == "paid" and "[KTX 예약 완료]" in mon.sent[0]


def test_gone_before_the_deadline_without_a_ticket_keeps_waiting(mon):
    """결제 직후엔 발권 내역에 반영되기 전일 수 있다 — 기한 안에서는 단정하지 않고 다음 확인 때 다시 본다."""
    j = _reserved_job(mon, timedelta(minutes=5))
    mon._remind([job(mon, j["id"])])  # 예약 목록에도 발권 내역에도 없다
    assert job(mon, j["id"])["status"] == "reserved" and mon.sent == []
    mon.korail.tickets.items = [FakeTicket("R1")]  # 다음 확인 때 반영됐다
    mon._reminders[j["id"]] = time.time() - m.REMIND_EVERY - 1
    mon._remind([job(mon, j["id"])])
    assert job(mon, j["id"])["status"] == "paid"


def test_tickets_unreadable_before_the_deadline_assumes_paid_but_says_so(mon):
    j = _reserved_job(mon, timedelta(minutes=5))
    mon.korail.tickets.error = RuntimeError("boom")
    mon._remind([job(mon, j["id"])])
    assert job(mon, j["id"])["status"] == "done"
    assert "결제를 마친 것으로 보여요" in mon.sent[0] and "확인하지 못했어요" in mon.sent[0]


def test_paid_train_is_not_booked_again_by_another_job(mon):
    a = _reserved_job(mon, timedelta(minutes=5))
    mon.korail.tickets.items = [FakeTicket("R1")]
    mon._remind([job(mon, a["id"])])
    assert job(mon, a["id"])["status"] == "paid"
    b = add_job(mon)
    mon._try_reserve(b, [FakeTrain("0700", general=True)])  # 이미 결제한 바로 그 열차
    assert len(mon.korail.reservations.created) == 1 and job(mon, b["id"])["status"] == "watching"


def test_expired_unpaid_reservation_resumes_watching(mon):
    j = _reserved_job(mon, timedelta(minutes=-3))
    mon._remind([job(mon, j["id"])])  # 기한이 지났고 발권된 승차권도 없다 → 만료
    r = job(mon, j["id"])
    assert r["status"] == "watching" and r["result"] is None and r["resumes"] == 1
    assert len(mon.sent) == 1 and "만료" in mon.sent[0] and "다시 시작" in mon.sent[0]
    assert mon.urgent == [] and j["id"] not in mon._reminders


def test_auto_resume_is_capped(mon):
    """결제할 생각이 없는 사용자에게 같은 열차를 무한히 다시 잡아 울리지 않는다."""
    j = _reserved_job(mon, timedelta(minutes=-3), resumes=m.MAX_AUTO_RESUMES)
    mon._remind([job(mon, j["id"])])
    assert job(mon, j["id"])["status"] == "done"
    assert "감시를 종료" in mon.sent[0]


def test_gone_after_the_deadline_but_tickets_unreadable_does_not_resume(mon):
    j = _reserved_job(mon, timedelta(minutes=-3))
    mon.korail.tickets.error = RuntimeError("boom")
    mon._remind([job(mon, j["id"])])
    assert job(mon, j["id"])["status"] == "done"  # 결제했을 수도 있으니 불확실하면 다시 잡지 않는다
    assert "확인하지 못해" in mon.sent[0] and "다시 시작하지 않았어요" in mon.sent[0]
    assert any("발권 내역 확인 오류" in e["msg"] for e in mon.logs)


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


def test_notify_error_is_exposed_until_a_send_succeeds(real_notify, monkeypatch):
    """웹후크가 지워지는 등 전송이 계속 실패하면 화면에 보여야 한다 (로그만 보면 알림이 조용히 끊긴다)."""
    assert real_notify.notify_error is None
    ok = real_notify.posts.append

    def boom(text, mention_everyone=False):
        raise RuntimeError("디스코드 전송 실패 (404): Unknown Webhook")

    monkeypatch.setattr(m.discord, "send", boom)
    real_notify.notify("[KTX] 안내")
    assert "404" in real_notify.notify_error
    monkeypatch.setattr(m.discord, "send", lambda text, mention_everyone=False: ok((text, mention_everyone)))
    real_notify.notify("[KTX] 안내")
    assert real_notify.notify_error is None


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
