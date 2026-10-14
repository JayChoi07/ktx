"""웹 UI + API. 내 PC(127.0.0.1)에서만 열린다."""

from __future__ import annotations

import base64
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from . import config, kakao
from .monitor import MIN_INTERVAL, OPTIONS, TRAIN_TYPES, Monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
monitor = Monitor()
STATIC = (Path(getattr(sys, "_MEIPASS", "")) / "ktx" / "static") if getattr(sys, "frozen", False) else Path(__file__).parent / "static"
LOCAL_HOSTS = {"localhost", "127.0.0.1"}


@asynccontextmanager
async def lifespan(_: FastAPI):
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
        "setup": {
            "korail": korail_set,
            "kakao_key": kakao.configured(),
            "kakao_connected": kakao.connected(),
            "redirect_uri": kakao.redirect_uri(),
        },
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
        monitor.jobs.update(job_id, status="watching", result=None)
        monitor.wake()
    elif action == "cancel":  # 예약 취소(좌석 반납) 후 중지
        try:
            monitor.cancel_reservation(job_id)
        except Exception as e:
            raise HTTPException(400, f"예약 취소 실패: {e}")
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


# ---------- 첫 실행 설정 ----------
class KorailIn(BaseModel):
    user_id: str = Field(min_length=1)
    password: str = Field(min_length=1)


@app.post("/api/setup/korail")
def setup_korail(body: KorailIn):
    try:
        name = monitor.setup_korail(body.user_id.strip(), body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"name": name}


class KakaoKeyIn(BaseModel):
    rest_api_key: str = ""
    client_secret: str = ""
    code: str = ""  # 앱 만든 사람이 건네준 '설정 코드' (키+시크릿을 하나로 묶은 것)


def _decode_code(code: str) -> tuple[str, str]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(code.strip() + "=" * (-len(code.strip()) % 4)))
        return str(raw["k"]), str(raw.get("s", ""))
    except Exception:
        raise HTTPException(400, "설정 코드가 올바르지 않아요. 전달받은 코드를 그대로 붙여 넣어 주세요.")


@app.get("/api/setup/kakao/code")
def kakao_code():
    """다른 사람에게 줄 설정 코드. 내 PC(localhost)에서만 호출 가능."""
    key = config.get("KAKAO_REST_API_KEY")
    if not key:
        raise HTTPException(404, "카카오 키가 설정되어 있지 않아요")
    raw = json.dumps({"k": key, "s": config.get("KAKAO_CLIENT_SECRET") or ""}).encode()
    return {"code": base64.urlsafe_b64encode(raw).decode().rstrip("=")}


@app.post("/api/setup/kakao")
def setup_kakao(body: KakaoKeyIn):
    key, secret = (_decode_code(body.code) if body.code.strip() else (body.rest_api_key.strip(), body.client_secret.strip()))
    if len(key) < 10:
        raise HTTPException(400, "카카오 REST API 키를 입력해 주세요")
    config.put("KAKAO_REST_API_KEY", key)
    if secret:
        config.put("KAKAO_CLIENT_SECRET", secret)
    else:
        config.remove("KAKAO_CLIENT_SECRET")
    kakao.disconnect()  # 키가 바뀌면 이전 토큰은 무효
    return {"ok": True}


@app.post("/api/setup/reset/{what}")
def setup_reset(what: str):
    if what == "korail":
        monitor.forget_korail()
    elif what == "kakao":
        config.remove("KAKAO_REST_API_KEY", "KAKAO_CLIENT_SECRET")
        kakao.disconnect()
    else:
        raise HTTPException(404)
    return {"ok": True}


# ---------- 카카오 ----------
@app.get("/kakao/login")
def kakao_login():
    if not kakao.configured():
        raise HTTPException(400, "카카오 REST API 키를 먼저 설정하세요")
    return RedirectResponse(kakao.login_url())


@app.get("/kakao/callback")
def kakao_callback(code: str | None = None, error: str | None = None, error_description: str | None = None):
    if error or not code:
        monitor.say(f"카카오 인증 실패: {error_description or error}", "error")
        return RedirectResponse(f"/?kakao_error={error_description or error or 'unknown'}")
    try:
        kakao.exchange_code(code)
    except Exception as e:
        monitor.say(str(e), "error")
        return RedirectResponse(f"/?kakao_error={e}")
    monitor.say("카카오톡 연결 완료", "ok")
    return RedirectResponse("/?kakao=ok")


@app.post("/api/kakao/test")
def kakao_test():
    try:
        kakao.send("[KTX] 테스트 알림입니다. 이 메시지가 보이면 연결 성공!")
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}
