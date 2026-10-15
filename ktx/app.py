"""웹 UI + API. 내 PC(127.0.0.1)에서만 열린다."""

from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import config, discord, store, update
from ._version import __version__
from .monitor import MAX_INTERVAL, MIN_INTERVAL, OPTIONS, TRAIN_TYPES, Monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
monitor = Monitor()
STATIC = (Path(getattr(sys, "_MEIPASS", "")) / "ktx" / "static") if getattr(sys, "frozen", False) else Path(__file__).parent / "static"
LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def _drop_legacy_kakao() -> None:
    """v1.2 까지 카카오 알림에 쓰던 키·토큰. 더 이상 쓰지 않으니 남겨 두지 않는다."""
    config.remove("KAKAO_REST_API_KEY", "KAKAO_CLIENT_SECRET")
    (store.DATA_DIR / "kakao.json").unlink(missing_ok=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _drop_legacy_kakao()
    update.start()
    monitor.start()
    yield


app = FastAPI(title="KTX 자동예매", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    """다른 웹사이트가 내 로컬 서버를 몰래 호출하지 못하게 막는다 (DNS rebinding / CSRF)."""
    if (request.headers.get("host") or "").split(":")[0] not in LOCAL_HOSTS:
        return JSONResponse({"detail": "허용되지 않은 호스트"}, status_code=403)
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD") and origin and urlparse(origin).hostname not in LOCAL_HOSTS:
        return JSONResponse({"detail": "허용되지 않은 출처"}, status_code=403)
    return await call_next(request)


class JobIn(BaseModel):
    dep: str = "서울"
    arr: str = "부산"
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    time_from: str = Field(default="00:00", pattern=r"^\d{2}:\d{2}$")
    time_to: str = Field(default="23:59", pattern=r"^\d{2}:\d{2}$")
    option: str = "GENERAL_FIRST"
    train_type: str = "KTX"
    adults: int = Field(default=1, ge=1, le=9)

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        try:  # 정규식만으로는 2026-13-45 같은 날짜를 못 거른다 — 저장되면 매 조회가 터진다
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("올바른 날짜가 아니에요") from None
        return v

    @field_validator("time_from", "time_to")
    @classmethod
    def _time(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%H:%M")
        except ValueError:
            raise ValueError("올바른 시각이 아니에요") from None
        return v

    @field_validator("option")
    @classmethod
    def _opt(cls, v: str) -> str:
        if v not in OPTIONS:
            raise ValueError("잘못된 좌석 옵션")
        return v

    @field_validator("train_type")
    @classmethod
    def _tt(cls, v: str) -> str:
        if v not in TRAIN_TYPES:
            raise ValueError("잘못된 열차 종류")
        return v


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/state")
def state():
    korail_set = bool(config.get("KORAIL_ID") and config.get("KORAIL_PW"))
    return {
        "login": monitor.login_state,
        "account": monitor.account,
        "paused_for": max(0, int(monitor.paused_until - time.time())),
        "interval": monitor.settings["interval"],
        "min_interval": MIN_INTERVAL,
        "max_interval": MAX_INTERVAL,
        "version": __version__,
        "update": update.available,
        "notify_error": monitor.notify_error,
        "setup": {"korail": korail_set, "discord": discord.configured()},
        "stations": monitor.stations(),
        "options": OPTIONS,
        "jobs": monitor.jobs.all(),
        "logs": list(monitor.logs)[:60],
    }


# ---------- 감시 작업 ----------
@app.post("/api/jobs")
def add_job(job: JobIn):
    if job.dep == job.arr:
        raise HTTPException(400, "출발역과 도착역이 같아요")
    if job.time_from > job.time_to:
        raise HTTPException(400, "시작 시각이 종료 시각보다 늦어요")
    created = monitor.jobs.add(job.model_dump())
    monitor.say(f"감시 추가: {job.dep}→{job.arr} {job.date} {job.time_from}~{job.time_to}")
    monitor.wake()
    return created


@app.post("/api/jobs/{job_id}/{action}")
def job_action(job_id: str, action: str):
    if action == "stop":
        monitor.jobs.update(job_id, status="stopped")
    elif action == "resume":
        monitor.jobs.update(job_id, status="watching", result=None, resumes=0)  # 직접 다시 시작하면 자동 재개 횟수도 처음부터
        monitor.wake()
    elif action == "cancel":  # 예약 취소(좌석 반납) 후 중지
        try:
            monitor.cancel_reservation(job_id)
        except Exception as e:
            raise HTTPException(400, f"예약 취소 실패: {e}") from e
        monitor.jobs.update(job_id, status="stopped")
    else:
        raise HTTPException(404)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    monitor.jobs.remove(job_id)
    return {"ok": True}


class SettingsIn(BaseModel):
    interval: float


@app.post("/api/settings")
def settings(s: SettingsIn):
    try:
        monitor.set_interval(s.interval)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"interval": monitor.settings["interval"]}


# ---------- 첫 실행 설정 ----------
class KorailIn(BaseModel):
    user_id: str = Field(min_length=1)
    password: str = Field(min_length=1)


@app.post("/api/setup/korail")
def setup_korail(body: KorailIn):
    try:
        name = monitor.setup_korail(body.user_id.strip(), body.password)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"name": name}


class DiscordIn(BaseModel):
    url: str = Field(min_length=1)


@app.post("/api/setup/discord")
def setup_discord(body: DiscordIn):
    """웹훅 URL 로 확인 메시지를 보내 보고, 성공하면 저장한다. 실패 시 안내문과 함께 400."""
    try:
        url = discord.normalize(body.url)
        discord.send("[KTX] 디스코드 알림이 연결됐어요. 예약이 되면 이 채널로 알려드릴게요.", url=url)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    config.put(discord.KEY, url)
    monitor.notify_error = None
    monitor.say("디스코드 알림 연결 완료", "ok")
    return {"ok": True}


@app.post("/api/setup/reset/{what}")
def setup_reset(what: str):
    if what == "korail":
        monitor.forget_korail()
    elif what == "discord":
        config.remove(discord.KEY)
    else:
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/discord/test")
def discord_test():
    try:
        discord.send("[KTX] 테스트 알림입니다. 이 메시지가 보이면 연결 성공!")
    except Exception as e:
        monitor.notify_error = str(e)
        raise HTTPException(400, str(e)) from e
    monitor.notify_error = None
    return {"ok": True}
