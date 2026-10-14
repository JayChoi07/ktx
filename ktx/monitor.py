"""백그라운드 감시 루프: 매진 열차를 조회하다가 자리가 나면 예약하고 카톡으로 알린다."""

from __future__ import annotations

import dataclasses
import logging
import os
import random
import threading
import time
from collections import deque
from datetime import datetime

from pykorail import AdultPassenger, Korail
from pykorail.device import DeviceProfile, random_profile
from pykorail.exceptions import (
    AccessRestrictedError,
    KorailError,
    LoginFailedError,
    NeedToLoginError,
    NoResultsError,
    PastDepartureError,
    SoldOutError,
)

from . import kakao, store

log = logging.getLogger("ktx")

MIN_INTERVAL = 5.0  # 이보다 빠르게 조회하면 계정 정지 위험
BLOCK_PAUSE = 600  # 이용제한 감지 시 쉬는 시간(초)
REMIND_EVERY = 120  # 결제 재알림 간격(초)
OPTIONS = {"GENERAL_FIRST": "일반실 우선", "GENERAL_ONLY": "일반실만", "SPECIAL_FIRST": "특실 우선", "SPECIAL_ONLY": "특실만"}
TRAIN_TYPES = {"KTX": "100", "ALL": "109"}


def _hhmm(t: str) -> str:
    return f"{t[:2]}:{t[2:4]}"


def _fmt_limit(d: str, t: str) -> str:
    return f"{d[4:6]}/{d[6:8]} {t[:2]}:{t[2:4]}"


def _has_seat(train, option: str) -> bool:
    if option == "GENERAL_ONLY":
        return train.has_general_seat()
    if option == "SPECIAL_ONLY":
        return train.has_special_seat()
    return train.has_seat()


class Monitor:
    def __init__(self) -> None:
        self.jobs = store.Jobs()
        self.korail: Korail | None = None
        self.logs: deque[dict] = deque(maxlen=200)
        self.settings = {"interval": 8.0, **store.load("settings.json", {})}
        self.paused_until = 0.0
        self.login_state = "로그인 전"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._stations: list[str] = []
        self._reminders: dict[str, float] = {}

    # ---------- 공개 ----------
    def start(self) -> None:
        threading.Thread(target=self._run, name="ktx-monitor", daemon=True).start()

    def say(self, msg: str, level: str = "info") -> None:
        self.logs.appendleft({"t": datetime.now().strftime("%m/%d %H:%M:%S"), "level": level, "msg": msg})
        log.log(logging.WARNING if level in ("warn", "error") else logging.INFO, msg)

    def notify(self, text: str) -> None:
        if not kakao.connected():
            self.say(f"[카톡 미연결] {text}", "warn")
            return
        try:
            kakao.send(text)
        except Exception as e:  # 알림 실패가 예약 흐름을 막으면 안 된다
            self.say(f"카톡 전송 실패: {e}", "error")

    def wake(self) -> None:
        self._wake.set()

    def set_interval(self, sec: float) -> None:
        self.settings["interval"] = max(MIN_INTERVAL, float(sec))
        store.save("settings.json", self.settings)

    def stations(self) -> list[str]:
        return self._stations

    # ---------- 로그인 ----------
    def _profile(self) -> DeviceProfile:
        saved = store.load("device.json", None)
        if saved:
            try:
                return DeviceProfile(**saved)
            except TypeError:
                pass
        p = random_profile()  # 같은 기기로 보이도록 최초 1회 생성 후 고정
        store.save("device.json", dataclasses.asdict(p))
        return p

    def _login(self) -> bool:
        uid, pw = os.environ.get("KORAIL_ID"), os.environ.get("KORAIL_PW")
        if not uid or not pw:
            self.login_state = ".env 에 KORAIL_ID / KORAIL_PW 필요"
            return False
        try:
            if self.korail:
                self.korail.close()
            self.korail = Korail(device_profile=self._profile())
            self.korail.login(uid, pw)
            self.login_state = f"로그인됨 ({self.korail.name})"
            if not self._stations:
                self._stations = sorted(s.name for s in self.korail.stations.all())
            self.say("코레일 로그인 성공")
            return True
        except LoginFailedError as e:
            self.login_state = "로그인 실패"
            self.say(f"코레일 로그인 실패: {e}", "error")
            self.notify(f"[KTX] 코레일 로그인 실패: {e}")
            self.paused_until = time.time() + 3600  # 비밀번호 오류 반복 시 계정 잠김 방지
        except AccessRestrictedError as e:
            self._blocked(e)
        except Exception as e:
            self.login_state = "로그인 오류"
            self.say(f"로그인 오류: {e}", "error")
        return False

    def _blocked(self, e: Exception) -> None:
        self.paused_until = time.time() + BLOCK_PAUSE
        self.say(f"코레일 이용제한 감지, {BLOCK_PAUSE // 60}분 대기: {e}", "error")
        self.notify(f"[KTX] 코레일 이용제한 감지로 {BLOCK_PAUSE // 60}분 중단합니다. VPN/서버 IP인지 확인하세요.")

    # ---------- 메인 루프 ----------
    def _run(self) -> None:
        self._login()  # 시작 즉시 로그인해 역 목록을 UI에 제공
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # 루프가 죽지 않게
                self.say(f"예상치 못한 오류: {e!r}", "error")
                time.sleep(10)
            self._wake.wait(self.settings["interval"] * random.uniform(0.8, 1.4))
            self._wake.clear()

    def _tick(self) -> None:
        if time.time() < self.paused_until:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        for j in self.jobs.all():  # 지난 날짜 작업 정리
            if j["status"] == "watching" and j["date"] < today:
                self.jobs.update(j["id"], status="expired")
        jobs = self.jobs.all()
        watching = [j for j in jobs if j["status"] == "watching"]
        reserved = [j for j in jobs if j["status"] == "reserved"]
        if not watching and not reserved:
            return
        if self.korail is None and not self._login():
            return
        if reserved:
            self._remind(reserved)
        groups: dict[tuple, list[dict]] = {}
        for j in watching:
            groups.setdefault((j["dep"], j["arr"], j["date"], j["train_type"]), []).append(j)
        for (dep, arr, date, ttype), grp in groups.items():
            if self._stop.is_set() or time.time() < self.paused_until:
                break
            self._check_group(dep, arr, date, ttype, grp)
            time.sleep(random.uniform(0.5, 1.5))

    def _check_group(self, dep: str, arr: str, date: str, ttype: str, jobs: list[dict]) -> None:
        earliest = min(j["time_from"] for j in jobs)
        start = max(datetime.strptime(f"{date} {earliest}", "%Y-%m-%d %H:%M"), datetime.now())
        try:
            trains = self.korail.trains.search(
                dep, arr, depart_after=start, train_type=TRAIN_TYPES.get(ttype, "100"), include_no_seats=True, max_pages=3
            )
        except NoResultsError:
            trains = []
        except PastDepartureError:
            return
        except NeedToLoginError:
            self._login()
            return
        except AccessRestrictedError as e:
            self._blocked(e)
            return
        except Exception as e:
            self.say(f"조회 오류 {dep}→{arr}: {e}", "warn")
            return
        now = datetime.now().strftime("%m/%d %H:%M:%S")
        for job in jobs:
            self.jobs.update(job["id"], checks=job["checks"] + 1, last_checked=now)
            self._try_reserve(job, trains)

    def _try_reserve(self, job: dict, trains: list) -> None:
        ymd = job["date"].replace("-", "")
        want_from, want_to = job["time_from"].replace(":", ""), job["time_to"].replace(":", "")
        taken = {j["result"]["train_key"] for j in self.jobs.all() if j["status"] == "reserved" and j.get("result")}
        for t in trains:
            if t.dep_date != ymd or not (want_from <= t.dep_time[:4] <= want_to):
                continue
            key = f"{t.dep_date}-{t.train_no}"
            if key in taken or not _has_seat(t, job["option"]):
                continue
            self.say(f"빈자리 발견: {t.summary} → 예약 시도")
            try:
                rsv = self.korail.reservations.create(t, passengers=[AdultPassenger(job["adults"])], option=job["option"])
            except SoldOutError:
                self.say("예약 직전에 매진됨, 계속 감시합니다")
                continue
            except NeedToLoginError:
                self._login()
                return
            except AccessRestrictedError as e:
                self._blocked(e)
                return
            except KorailError as e:
                self.say(f"예약 거부: {e}", "warn")
                continue
            result = {
                "rsv_id": rsv.rsv_id,
                "train_key": key,
                "train": f"{t.train_type_name} {t.train_no} {t.dep_name}→{t.arr_name} {_hhmm(t.dep_time)}~{_hhmm(t.arr_time)}",
                "price": rsv.price,
                "buy_limit": _fmt_limit(rsv.buy_limit_date, rsv.buy_limit_time),
                "seats": [str(s) for s in rsv.seats],
            }
            self.jobs.update(job["id"], status="reserved", result=result)
            self._reminders[job["id"]] = time.time()
            self.say(f"예약 성공! {result['train']}", "ok")
            self.notify(
                f"[KTX 예약 성공]\n{result['train']}\n{job['date']} · {result['price']:,}원\n"
                f"결제기한 {result['buy_limit']} — 코레일톡에서 결제하세요!"
            )
            return

    def _remind(self, reserved: list[dict]) -> None:
        """결제 전까지 주기적으로 재알림. 예약이 목록에서 사라지면(결제/기한만료) 종료."""
        for j in reserved:
            if time.time() - self._reminders.get(j["id"], 0) < REMIND_EVERY:
                continue
            self._reminders[j["id"]] = time.time()
            r = j["result"]
            try:
                still = self.korail.reservations.find(r["rsv_id"])
            except Exception as e:
                self.say(f"예약 확인 오류: {e}", "warn")
                continue
            if still is None:
                self.jobs.update(j["id"], status="done")
                self.say(f"예약 {r['rsv_id']} 이(가) 목록에서 사라짐 (결제 완료 또는 기한 만료)")
                self.notify(f"[KTX] {r['train']} 예약이 목록에서 사라졌습니다. 결제했다면 OK, 아니면 기한 만료입니다.")
            else:
                self.notify(f"[KTX 결제 대기] {r['train']}\n결제기한 {r['buy_limit']}")

    def cancel_reservation(self, job_id: str) -> None:
        job = next((j for j in self.jobs.all() if j["id"] == job_id), None)
        if not job or not job.get("result") or self.korail is None:
            return
        rsv = self.korail.reservations.find(job["result"]["rsv_id"])
        if rsv:
            self.korail.reservations.cancel(rsv)
            self.say(f"예약 취소: {job['result']['train']}")
