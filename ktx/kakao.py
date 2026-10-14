"""카카오톡 '나에게 보내기' (공식 REST API: talk/memo/default/send).

각자 자기 PC 에서 실행하고, 카카오 로그인한 본인에게 메시지가 간다.
앱(REST API 키)은 개발자 콘솔에서 만든 것 하나를 공유한다 — 키 설정은 웹 UI 설정 화면에서 한다.
"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import urlencode

import httpx

from . import config, store

AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
TOKEN_FILE = "kakao.json"


def port() -> int:
    return int(os.environ.get("KTX_PORT", "8000"))


def redirect_uri() -> str:
    return f"http://localhost:{port()}/kakao/callback"


def configured() -> bool:
    return config.get("KAKAO_REST_API_KEY") is not None


def connected() -> bool:
    return bool(store.load(TOKEN_FILE, {}).get("refresh_token"))


def disconnect() -> None:
    store.save(TOKEN_FILE, {})


def login_url() -> str:
    q = {"client_id": config.get("KAKAO_REST_API_KEY"), "redirect_uri": redirect_uri(), "response_type": "code", "scope": "talk_message"}
    return f"{AUTH_URL}?{urlencode(q)}"


def _token_request(data: dict) -> dict:
    data = {**data, "client_id": config.get("KAKAO_REST_API_KEY")}
    if secret := config.get("KAKAO_CLIENT_SECRET"):
        data["client_secret"] = secret
    r = httpx.post(TOKEN_URL, data=data, timeout=10)
    if r.is_error:  # 카카오의 error_code/설명을 그대로 보여줘야 원인을 알 수 있다
        raise RuntimeError(f"카카오 토큰 발급 실패 ({r.status_code}): {r.text}")
    return r.json()


def _store_tokens(resp: dict) -> dict:
    tok = store.load(TOKEN_FILE, {})
    tok["access_token"] = resp["access_token"]
    tok["expires_at"] = time.time() + int(resp.get("expires_in", 0)) - 60
    if resp.get("refresh_token"):  # 갱신 시에는 refresh_token 이 없을 수 있다
        tok["refresh_token"] = resp["refresh_token"]
    store.save(TOKEN_FILE, tok)
    return tok


def exchange_code(code: str) -> None:
    _store_tokens(_token_request({"grant_type": "authorization_code", "redirect_uri": redirect_uri(), "code": code}))


def _access_token() -> str:
    tok = store.load(TOKEN_FILE, {})
    if not tok.get("refresh_token"):
        raise RuntimeError("카카오 계정이 연결되지 않았습니다")
    if time.time() >= tok.get("expires_at", 0):
        tok = _store_tokens(_token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}))
    return tok["access_token"]


def send(text: str, link: str | None = None) -> None:
    """나에게 카톡을 보낸다. 실패하면 예외를 던진다 (호출자가 로그로 남긴다)."""
    link = link or f"http://localhost:{port()}"
    template = {
        "object_type": "text",
        "text": text[:200],
        "link": {"web_url": link, "mobile_web_url": link},
        "button_title": "열기",
    }
    r = httpx.post(
        SEND_URL,
        headers={"Authorization": f"Bearer {_access_token()}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
        timeout=10,
    )
    if r.is_error:
        raise RuntimeError(f"카카오 전송 실패 ({r.status_code}): {r.text}")
