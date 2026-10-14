"""웹 UI + API. 로컬(127.0.0.1)에서만 열어 사용한다."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, RedirectResponse  # noqa: E402
from pydantic import BaseModel, Field, field_validator  # noqa: E402

from . import kakao  # noqa: E402
from .monitor import MIN_INTERVAL, OPTIONS, TRAIN_TYPES, Monitor  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
monitor = Monitor()
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    monitor.start()
    yield


app = FastAPI(title="KTX 자동예매", lifespan=lifespan)


class JobIn(BaseModel):
    dep: str = "서울"
    arr: str = "부산"
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    time_from: str = Field(default="00:00", pattern=r"^\d{2}:\d{2}$")
    time_to: str = Field(default="23:59", pattern=r"^\d{2}:\d{2}$")
    option: str = "GENERAL_FIRST"
    train_type: str = "KTX"
    adults: int = Field(default=1, ge=1, le=9)

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
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    return {
        "login": monitor.login_state,
        "paused_for": max(0, int(monitor.paused_until - time.time())),
        "interval": monitor.settings["interval"],
        "min_interval": MIN_INTERVAL,
        "kakao": {"configured": kakao.configured(), "connected": kakao.connected()},
        "stations": monitor.stations(),
        "options": OPTIONS,
        "jobs": monitor.jobs.all(),
        "logs": list(monitor.logs)[:60],
    }


@app.post("/api/jobs")
def add_job(job: JobIn):
    if job.dep == job.arr:
        raise HTTPException(400, "출발역과 도착역이 같습니다")
    if job.time_from > job.time_to:
        raise HTTPException(400, "시작 시각이 종료 시각보다 늦습니다")
    created = monitor.jobs.add(job.model_dump())
    monitor.say(f"감시 추가: {job.dep}→{job.arr} {job.date} {job.time_from}~{job.time_to}")
    monitor.wake()
    return created


@app.post("/api/jobs/{job_id}/{action}")
def job_action(job_id: str, action: str):
    if action == "stop":
        monitor.jobs.update(job_id, status="stopped")
    elif action == "resume":
        monitor.jobs.update(job_id, status="watching", result=None)
        monitor.wake()
    elif action == "cancel":  # 예약 취소(좌석 반납) 후 중지
        monitor.cancel_reservation(job_id)
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
    monitor.set_interval(s.interval)
    return {"interval": monitor.settings["interval"]}


@app.get("/kakao/login")
def kakao_login():
    if not kakao.configured():
        raise HTTPException(400, ".env 에 KAKAO_REST_API_KEY 를 먼저 설정하세요")
    return RedirectResponse(kakao.login_url())


@app.get("/kakao/callback")
def kakao_callback(code: str | None = None, error: str | None = None):
    if error or not code:
        raise HTTPException(400, f"카카오 인증 실패: {error}")
    kakao.exchange_code(code)
    monitor.say("카카오톡 연결 완료", "ok")
    return RedirectResponse("/")


@app.post("/api/kakao/test")
def kakao_test():
    try:
        kakao.send("[KTX] 테스트 알림입니다. 이 메시지가 보이면 연결 성공!")
    except Exception as e:
        raise HTTPException(400, f"전송 실패: {e}")
    return {"ok": True}
