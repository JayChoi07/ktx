"""카카오톡 '나에게 보내기' (공식 REST API: talk/memo/default/send).

사전 준비 (한 번만):
  1. https://developers.kakao.com 에서 앱 생성 → REST API 키 확인
  2. [카카오 로그인] 활성화 + Redirect URI 에 http://localhost:8000/kakao/callback 등록
  3. [동의항목]에서 '카카오톡 메시지 전송(talk_message)' 을 설정
  4. .env 에 KAKAO_REST_API_KEY (보안 사용 시 KAKAO_CLIENT_SECRET) 입력
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlencode

import httpx

from . import store

AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
TOKEN_FILE = "kakao.json"


def _key() -> str | None:
    return os.environ.get("KAKAO_REST_API_KEY") or None


def redirect_uri() -> str:
    return os.environ.get("KAKAO_REDIRECT_URI", "http://localhost:8000/kakao/callback")


def configured() -> bool:
    return _key() is not None


def connected() -> bool:
    return bool(store.load(TOKEN_FILE, {}).get("refresh_token"))


def login_url() -> str:
    q = {"client_id": _key(), "redirect_uri": redirect_uri(), "response_type": "code", "scope": "talk_message"}
    return f"{AUTH_URL}?{urlencode(q)}"


def _token_request(data: dict) -> dict:
    data = {**data, "client_id": _key()}
    if secret := os.environ.get("KAKAO_CLIENT_SECRET"):
        data["client_secret"] = secret
    r = httpx.post(TOKEN_URL, data=data, timeout=10)
    r.raise_for_status()
    return r.json()


def _store_tokens(resp: dict) -> dict:
    tok = store.load(TOKEN_FILE, {})
    now = time.time()
    tok["access_token"] = resp["access_token"]
    tok["expires_at"] = now + int(resp.get("expires_in", 0)) - 60
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
    link = link or os.environ.get("KTX_PUBLIC_URL", "http://localhost:8000")
    template = {
        "object_type": "text",
        "text": text[:200],
        "link": {"web_url": link, "mobile_web_url": link},
        "button_title": "열기",
    }
    import json

    r = httpx.post(
        SEND_URL,
        headers={"Authorization": f"Bearer {_access_token()}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
        timeout=10,
    )
    r.raise_for_status()
