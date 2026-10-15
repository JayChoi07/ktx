"""백그라운드 감시 루프: 매진 열차를 조회하다가 자리가 나면 예약하고 디스코드로 알린다."""

from __future__ import annotations

import dataclasses
import logging
import random
import threading
import time
from collections import deque
from datetime import datetime

from pykorail import AdultPassenger, Korail
from pykorail.device import DeviceProfile, random_profile
from pykorail.exceptions import (
    AccessRestrictedError,
    HttpStatusError,
    KorailError,
    LoginFailedError,
    NeedToLoginError,
    NoResultsError,
    PastDepartureError,
    SoldOutError,
)

from . import config, discord, store

log = logging.getLogger("ktx")

MIN_INTERVAL = 10.0  # 이보다 빠르게 조회하면 계정 정지 위험
MAX_INTERVAL = 3600.0
DEFAULT_INTERVAL = 30.0
BLOCK_PAUSE = 600  # 이용제한 감지 시 쉬는 시간(초)
REMIND_EVERY = 120  # 결제 재알림 간격(초)
# *_FIRST 는 선호하는 쪽이 없으면 다른 쪽 좌석이라도 잡는다 ("전부"). *_ONLY 는 그 등급만.
OPTIONS = {
    "GENERAL_FIRST": "전부 (일반실 먼저)",
    "GENERAL_ONLY": "일반실만",
    "SPECIAL_ONLY": "특실만",
    "SPECIAL_FIRST": "전부 (특실 먼저)",
}
TRAIN_TYPES = {"KTX": "100", "ALL": "109"}


def _hhmm(t: str) -> str:
    return f"{t[:2]}:{t[2:4]}"


def _fmt_limit(d: str, t: str) -> str:
    return f"{d[4:6]}/{d[6:8]} {t[:2]}:{t[2:4]}"


# pykorail 은 curl_cffi(없으면 requests) 로 통신하므로 네트워크 예외 타입을 직접 import 할 수 없다 — 클래스 이름으로 판별한다.
_NETWORK_ERROR_NAMES = {"ConnectionError", "ConnectError", "Timeout", "TimeoutError", "ReadTimeout", "RequestException", "RequestsError"}


def friendly(e: Exception) -> str:
    """코레일 예외를 사용자가 읽을 수 있는 안내문으로 바꾼다."""
    if isinstance(e, AccessRestrictedError):
        return "코레일이 접속을 제한했어요. VPN/프록시를 끄고, 잠시 뒤에 다시 시도하세요. (회사·집 일반 네트워크에서 실행해야 해요)"
    if isinstance(e, LoginFailedError):
        return f"코레일 로그인에 실패했어요. 아이디/비밀번호를 확인하세요. ({e})"
    if isinstance(e, HttpStatusError):
        return f"코레일 서버가 정상 응답하지 않아요 (HTTP {e.status_code}). 잠시 뒤 자동으로 다시 시도해요."
    if {c.__name__ for c in type(e).__mro__} & _NETWORK_ERROR_NAMES:
        return "인터넷 연결을 확인하세요. 연결되면 자동으로 다시 시도해요."
    return str(e)


def _snapshot(job: dict, trains: list) -> list[dict]:
    """감시 조건(날짜·시간대)에 해당하는 열차와 현재 좌석 상태 — 카드에 보여주기 위한 요약."""
    ymd = job["date"].replace("-", "")
    lo, hi = job["time_from"].replace(":", ""), job["time_to"].replace(":", "")
    return [
        {"t": _hhmm(t.dep_time), "no": f"{t.train_type_name} {t.train_no}", "g": t.has_general_seat(), "s": t.has_special_seat()}
        for t in trains
        if t.dep_date == ymd and lo <= t.dep_time[:4] <= hi
    ]


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
        self.settings = {"interval": DEFAULT_INTERVAL, **store.load("settings.json", {})}
        # 예전 버전에서 저장한 값이 새 최소치보다 작으면 올린다
        self.settings["interval"] = min(MAX_INTERVAL, max(MIN_INTERVAL, float(self.settings["interval"])))
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

    def notify(self, text: str, urgent: bool = False) -> None:
        """디스코드 채널로 알린다. urgent 면 @everyone 으로 폰 푸시를 확실히 울린다."""
        if not discord.configured():
            self.say(f"[디스코드 미설정] {text}", "warn")
            return
        # 한 채널을 여러 사람이 쓸 수 있어 누구 건지 붙인다: [KTX 예약 성공] → [KTX 예약 성공 · 이름]
        if (name := self.account) and text.startswith("["):
            text = text.replace("]", f" · {name}]", 1)
        try:
            discord.send(text, mention_everyone=urgent)
        except Exception as e:  # 알림 실패가 예약 흐름을 막으면 안 된다
            self.say(f"디스코드 전송 실패: {e}", "error")

    def wake(self) -> None:
        self._wake.set()

    def set_interval(self, sec: float) -> None:
        sec = float(sec)
        if not sec >= MIN_INTERVAL:  # NaN 도 걸러낸다
            raise ValueError(f"조회 간격은 최소 {MIN_INTERVAL:g}초예요")
        if sec > MAX_INTERVAL:
            raise ValueError(f"조회 간격은 최대 {MAX_INTERVAL / 60:g}분이에요")
        self.settings["interval"] = sec
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
        uid, pw = config.get("KORAIL_ID"), config.get("KORAIL_PW")
        if not uid or not pw:
            self.login_state = "코레일 계정 설정 필요"
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
            self.say(friendly(e), "error")
            self.notify(f"[KTX] {friendly(e)}")
            self.paused_until = time.time() + 3600  # 비밀번호 오류 반복 시 계정 잠김 방지
        except AccessRestrictedError as e:
            self._blocked(e)
        except Exception as e:
            self.login_state = "로그인 오류"
            self.say(f"로그인 오류: {e}", "error")
        return False

    def _blocked(self, e: Exception) -> None:
        self.paused_until = time.time() + BLOCK_PAUSE
        self.say(f"{friendly(e)} ({BLOCK_PAUSE // 60}분 쉬었다가 다시 시도합니다)", "error")
        self.notify(f"[KTX] 코레일 접속 제한 감지 — {BLOCK_PAUSE // 60}분 쉽니다. VPN을 껐는지 확인하세요.")

    @property
    def account(self) -> str | None:
        return self.korail.name if self.korail is not None and self.login_state.startswith("로그인됨") else None

    def setup_korail(self, user_id: str, password: str) -> str:
        """입력받은 계정으로 로그인해 보고, 성공하면 저장한다. 실패 시 안내문과 함께 예외."""
        client = Korail(device_profile=self._profile())
        try:
            client.login(user_id, password)
        except Exception as e:
            raise ValueError(friendly(e)) from e
        config.put("KORAIL_ID", user_id)
        config.put("KORAIL_PW", password)
        if self.korail:
            self.korail.close()
        self.korail = client
        self.paused_until = 0.0
        self.login_state = f"로그인됨 ({client.name})"
        self._stations = sorted(s.name for s in client.stations.all())
        self.say(f"코레일 계정 설정 완료 ({client.name})", "ok")
        self.wake()
        return client.name

    def forget_korail(self) -> None:
        config.remove("KORAIL_ID", "KORAIL_PW")
        if self.korail:
            self.korail.close()
        self.korail = None
        self.login_state = "코레일 계정 설정 필요"

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
        now = datetime.now()
        today, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        for j in self.jobs.all():  # 지난 날짜·이미 지나간 시간대 작업 정리
            if j["status"] == "watching" and (j["date"] < today or (j["date"] == today and j["time_to"] < hhmm)):
                self.jobs.update(j["id"], status="expired")
                self.say(f"기간이 지나 감시를 종료했어요: {j['dep']}→{j['arr']} {j['date']} {j['time_from']}~{j['time_to']}")
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
            self.jobs.update(
                job["id"], checks=job["checks"] + 1, last_checked=now, last_checked_ts=time.time(), trains=_snapshot(job, trains)
            )
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
            self.say(f"빈자리 발견: {t.summary()} → 예약 시도")
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
                "buy_limit_iso": datetime.strptime(rsv.buy_limit_date + rsv.buy_limit_time[:4], "%Y%m%d%H%M").isoformat(),
                "seats": [str(s) for s in rsv.seats],
            }
            self.jobs.update(job["id"], status="reserved", result=result)
            self._reminders[job["id"]] = time.time()
            self.say(f"예약 성공! {result['train']}", "ok")
            self.notify(
                f"[KTX 예약 성공]\n{result['train']}\n{job['date']} · {result['price']:,}원\n"
                f"결제기한 {result['buy_limit']} — 코레일톡에서 결제하세요!",
                urgent=True,
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
