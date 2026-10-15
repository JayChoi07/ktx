"""백그라운드 감시 루프: 매진 열차를 조회하다가 자리가 나면 예약하고 디스코드로 알린다."""

from __future__ import annotations

import dataclasses
import logging
import random
import threading
import time
from collections import deque
from datetime import datetime, timedelta

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
BLOCK_REPEAT = 3600  # 접속 제한이 이어질 때 "아직 막혀 있다"를 다시 알리는 간격(초)
REMIND_EVERY = 120  # 결제 재알림 간격(초)
MAX_SEARCH_PAGES = 10  # 한 번의 조회에서 이어 받는 최대 쪽 수 (쪽마다 요청이 한 번 나간다)
MAX_AUTO_RESUMES = 2  # 결제기한 만료 뒤 감시를 자동으로 다시 시작하는 최대 횟수
EXPIRY_GRACE = 300  # 결제기한이 지나도 발권 내역에 없을 때 만료로 단정하기 전에 더 기다리는 시간(초) — 막판 결제의 반영 지연 대비
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
        self._block_pinged = False  # 접속 제한이 이어지는 동안 @everyone 은 첫 알림에만 쓴다
        self._block_notified_at = 0.0
        self._reserve_lock = threading.Lock()  # 코레일 예약 생성~저장 구간. 재시작 전에 이 구간이 끝나기를 기다린다
        self._restarting = False  # True 면 새 예약을 만들지 않는다 (업데이트 재시작 직전)
        self.notify_error: str | None = None  # 마지막 디스코드 전송이 실패했다면 그 이유 (화면에 보여준다)

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
            self.notify_error = None
        except Exception as e:  # 알림 실패가 예약 흐름을 막으면 안 된다
            self.notify_error = str(e)
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
            self._block_pinged = False
            return True
        except LoginFailedError as e:
            self.login_state = "로그인 실패"
            self.say(friendly(e), "error")
            self.notify(f"[KTX] {friendly(e)}", urgent=True)  # 1시간 멈추므로 놓치면 안 된다
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
        # 계속 막혀 있어도 10분마다 같은 알림을 보내지 않는다: 첫 알림만 @everyone, 이후엔 BLOCK_REPEAT 마다 멘션 없이
        first = not self._block_pinged
        if first or time.time() - self._block_notified_at >= BLOCK_REPEAT:
            self._block_pinged, self._block_notified_at = True, time.time()
            self.notify(f"[KTX] 코레일 접속 제한 감지 — {BLOCK_PAUSE // 60}분 쉽니다. VPN을 껐는지 확인하세요.", urgent=first)

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
        # 접속 제한으로 멈춘 동안에는 코레일을 부르지 않지만, 결제 재알림은 계속 간다
        online = time.time() >= self.paused_until and (self.korail is not None or self._login())
        if reserved:
            self._remind(reserved, verify=online)
        if not online:
            return
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
        latest = max(j["time_to"] for j in jobs).replace(":", "")
        start = max(datetime.strptime(f"{date} {earliest}", "%Y-%m-%d %H:%M"), datetime.now())
        try:
            trains = self._search(dep, arr, start, date.replace("-", ""), latest, TRAIN_TYPES.get(ttype, "100"))
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
        self._block_pinged = False  # 조회가 정상으로 돌아왔다 — 접속 제한 에피소드 끝
        now = datetime.now().strftime("%m/%d %H:%M:%S")
        for job in jobs:
            self.jobs.update(
                job["id"], checks=job["checks"] + 1, last_checked=now, last_checked_ts=time.time(), trains=_snapshot(job, trains)
            )
            self._try_reserve(job, trains)

    def _search(self, dep: str, arr: str, start: datetime, ymd: str, until: str, train_type: str) -> list:
        """start 이후 열차를 감시 창의 끝(until, 'HHMM')에 닿을 때까지 한 쪽씩 이어 받는다 (최대 MAX_SEARCH_PAGES).

        pykorail 의 max_pages 는 시각과 무관하게 쪽 수만 봐서(쪽당 약 10편) 종일 감시는 앞쪽 30편에서 잘렸다.
        그래서 한 쪽씩 받고, 마지막 열차의 출발 시각을 다음 조회의 시작으로 삼는다 (pykorail 의 쪽 이어받기와 같은 방식).
        좁은 시간대는 한 쪽으로 끝나 예전(항상 3쪽)보다 요청이 줄어든다.
        """
        found: dict[tuple, object] = {}
        cursor = start
        for i in range(MAX_SEARCH_PAGES):
            if i:
                time.sleep(random.uniform(0.3, 0.8))  # 쪽 사이를 띄워 한꺼번에 몰아치지 않는다
            try:
                page = self.korail.trains.search(dep, arr, depart_after=cursor, train_type=train_type, include_no_seats=True, max_pages=1)
            except NoResultsError:
                if not found:
                    raise
                break
            fresh = {k: t for t in page if (k := (t.dep_date, t.dep_time, t.train_no)) not in found}  # 경계 열차는 다음 쪽에 또 나온다
            if not fresh:
                break
            found.update(fresh)
            last = page[-1]
            if last.dep_date != ymd or last.dep_time[:4] > until:  # 같은 분(= until)의 다른 열차가 다음 쪽에 있을 수 있어 초과해야 멈춘다
                break
            following = datetime.strptime(last.dep_date + last.dep_time[:4], "%Y%m%d%H%M")
            if following <= cursor:
                break
            cursor = following
        return list(found.values())

    def _is_watching(self, job_id: str) -> bool:
        return any(j["id"] == job_id and j["status"] == "watching" for j in self.jobs.all())

    def _try_reserve(self, job: dict, trains: list) -> None:
        with self._reserve_lock:  # 예약 생성과 저장 사이에 프로세스가 끝나 예약이 추적 없이 남는 일을 막는다 (hold_for_restart)
            if self._restarting:
                return
            self._try_reserve_locked(job, trains)

    def hold_for_restart(self, timeout: float = 30.0) -> bool:
        """업데이트로 재시작하기 직전에 호출한다. 새 예약을 막고 진행 중인 예약 처리가 끝나기를 기다린 뒤,
        결제 대기 중인 예약이 없을 때만 True (재시작해도 된다). 아니면 막은 것을 풀고 False."""
        self._restarting = True
        if not self._reserve_lock.acquire(timeout=timeout):
            self._restarting = False
            return False
        try:
            if any(j["status"] == "reserved" for j in self.jobs.all()):
                self._restarting = False
                return False
            return True
        finally:
            self._reserve_lock.release()

    def release_restart(self) -> None:
        self._restarting = False

    def _try_reserve_locked(self, job: dict, trains: list) -> None:
        ymd = job["date"].replace("-", "")
        want_from, want_to = job["time_from"].replace(":", ""), job["time_to"].replace(":", "")
        taken = {j["result"]["train_key"] for j in self.jobs.all() if j["status"] in ("reserved", "paid") and j.get("result")}
        for t in trains:
            if t.dep_date != ymd or not (want_from <= t.dep_time[:4] <= want_to):
                continue
            key = f"{t.dep_date}-{t.train_no}"
            if key in taken or not _has_seat(t, job["option"]):
                continue
            if not self._is_watching(job["id"]):  # 조회하는 사이 사용자가 중지·삭제했다 (job 은 그 전의 스냅샷)
                return
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
            if self.jobs.update(job["id"], status="reserved", result=result) is None:
                # 예약이 나가는 사이 사용자가 작업을 삭제했다 — 추적할 수 없는 예약이 남지 않게 바로 취소한다
                self.say(f"삭제된 감시의 예약이라 바로 취소합니다: {result['train']}", "warn")
                try:
                    self.korail.reservations.cancel(rsv)
                except Exception as e:
                    self.say(f"예약 취소 오류 (결제기한이 지나면 자동으로 사라져요): {e}", "warn")
                return
            self._reminders[job["id"]] = time.time()
            self.say(f"예약 성공! {result['train']}", "ok")
            self.notify(
                f"[KTX 예약 성공]\n{result['train']}\n{job['date']} · {result['price']:,}원\n"
                f"결제기한 {result['buy_limit']} — 코레일톡에서 결제하세요!",
                urgent=True,
            )
            return

    def _remind(self, reserved: list[dict], verify: bool = True) -> None:
        """결제 전까지 주기적으로 재알림. 예약이 목록에서 사라지면(결제/기한만료) 종료.

        verify=False 는 코레일 접속이 막힌 상태다 — 예약이 남아 있는지 확인할 수 없으니 저장해 둔 결제기한 전까지만 알린다.
        """
        for j in reserved:
            if time.time() - self._reminders.get(j["id"], 0) < REMIND_EVERY:
                continue
            self._reminders[j["id"]] = time.time()
            r = j["result"]
            if not verify:
                limit = r.get("buy_limit_iso")
                if limit and datetime.now() < datetime.fromisoformat(limit):
                    self.notify(
                        f"[KTX 결제 대기] {r['train']}\n결제기한 {r['buy_limit']} (코레일 접속 제한 중이라 예약 상태는 확인하지 못했어요)"
                    )
                continue
            try:
                still = self.korail.reservations.find(r["rsv_id"])
            except AccessRestrictedError as e:
                self._blocked(e)  # 접속 제한이면 감시 전체를 멈추고 이번 확인은 접는다
                return
            except Exception as e:
                self.say(f"예약 확인 오류: {e}", "warn")
                continue
            if still is None:
                self._reservation_gone(j, r)
            else:
                self.notify(f"[KTX 결제 대기] {r['train']}\n결제기한 {r['buy_limit']}")

    def _finish(self, job: dict, text: str) -> None:
        if self.jobs.transition(job["id"], job["result"]["rsv_id"], status="done") is None:
            return  # 확인하는 사이 사용자가 중지·취소·삭제했다 — 그 선택을 덮어쓰지 않는다
        self._reminders.pop(job["id"], None)
        self.notify(text)

    def _paid(self, job: dict, r: dict, tickets: list) -> None:
        """결제까지 끝나 승차권이 발권됐다 — 카드를 '예약 완료'로 바꾸고 열차·좌석·금액을 알린다."""
        seats = [f"{t.car_no}호차 {t.seat_no}" for t in tickets]
        price = sum(t.price for t in tickets) or r.get("price", 0)
        if self.jobs.transition(job["id"], r["rsv_id"], status="paid", result={**r, "paid": {"seats": seats, "price": price}}) is None:
            return
        self._reminders.pop(job["id"], None)
        self.say(f"예약 완료 (결제 확인): {r['train']} {', '.join(seats)}", "ok")
        self.notify(
            f"[KTX 예약 완료] {r['train']}\n{job['date']} · {', '.join(seats)} · {price:,}원\n결제까지 확인했어요. 좋은 여행 되세요!"
        )

    def _reservation_gone(self, job: dict, r: dict) -> None:
        """예약이 코레일 예약 목록에서 사라졌다. 발권 내역에 같은 예약번호의 승차권이 있으면 결제 완료로 확정한다.

        없으면: 결제기한 안에서는 아직 반영 전일 수 있어 단정하지 않고 다음 확인 때 다시 본다.
        기한이 지나도 EXPIRY_GRACE 동안은 더 기다린다 (막판 결제의 반영 지연을 만료로 오판해 두 번째 예약을 만들지 않으려고).
        그래도 없으면 만료로 보고 같은 조건으로 감시를 다시 시작한다 (최대 MAX_AUTO_RESUMES 번).
        발권 내역을 읽지 못하면 결제했을 수도 있으니 다시 잡지 않고, 접속 제한이면 감시를 멈추고 나중에 다시 확인한다.
        """
        self.say(f"예약 {r['rsv_id']} 이(가) 목록에서 사라짐")
        limit = datetime.fromisoformat(r["buy_limit_iso"]) if r.get("buy_limit_iso") else None
        past_deadline = bool(limit and datetime.now() >= limit)
        expired = bool(limit and datetime.now() >= limit + timedelta(seconds=EXPIRY_GRACE))
        try:
            tickets = [t for t in self.korail.tickets.all() if t.pnr_no == r["rsv_id"]]
        except AccessRestrictedError as e:
            self._blocked(e)
            return
        except Exception as e:
            self.say(f"발권 내역 확인 오류: {e}", "warn")
            if past_deadline:
                self._finish(
                    job,
                    f"[KTX] {r['train']} 예약이 사라졌지만 결제 여부를 확인하지 못해 감시를 다시 시작하지 않았어요. "
                    "결제하지 않았다면 '다시 감시'를 눌러 주세요.",
                )
            else:
                self._finish(
                    job,
                    f"[KTX] {r['train']} 예약이 사라졌어요. 결제기한 안이라 결제를 마친 것으로 보여요. (발권 내역은 확인하지 못했어요)",
                )
            return
        if tickets:
            self._paid(job, r, tickets)
            return
        if not expired:
            return  # 결제 직후라 발권 내역에 반영되기 전일 수 있다 — 다음 확인 때 다시 본다
        resumes = job.get("resumes", 0)
        if resumes >= MAX_AUTO_RESUMES:
            self._finish(
                job,
                f"[KTX] {r['train']} 결제기한이 지나 예약이 만료됐어요. 이미 {MAX_AUTO_RESUMES}번 다시 잡았기 때문에 감시를 종료합니다. "
                "계속하려면 '다시 감시'를 눌러 주세요.",
            )
            return
        if self.jobs.transition(job["id"], r["rsv_id"], status="watching", result=None, resumes=resumes + 1) is None:
            return  # 확인하는 사이 사용자가 중지·취소·삭제했다
        self._reminders.pop(job["id"], None)
        self.say(f"결제기한이 지나 만료 — 감시를 다시 시작합니다 ({resumes + 1}/{MAX_AUTO_RESUMES})", "warn")
        self.notify(
            f"[KTX] {r['train']} 결제기한이 지나 예약이 만료됐어요. "
            f"같은 조건으로 감시를 다시 시작합니다. ({resumes + 1}/{MAX_AUTO_RESUMES})"
        )

    def cancel_reservation(self, job_id: str) -> None:
        job = next((j for j in self.jobs.all() if j["id"] == job_id), None)
        if not job or not job.get("result") or self.korail is None:
            return
        rsv = self.korail.reservations.find(job["result"]["rsv_id"])
        if rsv:
            self.korail.reservations.cancel(rsv)
            self.say(f"예약 취소: {job['result']['train']}")
