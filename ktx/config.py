"""자격 증명 저장소.

비밀번호·키는 평문 파일이 아니라 Windows 자격 증명 관리자(keyring)에 저장한다.
개발 편의를 위해 keyring 에 값이 없으면 환경변수(.env)를 읽는다.
"""

from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "ktx-macro"
NAMES = ("KORAIL_ID", "KORAIL_PW", "KAKAO_REST_API_KEY", "KAKAO_CLIENT_SECRET")


def get(name: str) -> str | None:
    try:
        value = keyring.get_password(SERVICE, name)
    except KeyringError:
        value = None
    return value or os.environ.get(name) or None


def put(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def remove(*names: str) -> None:
    for name in names:
        try:
            keyring.delete_password(SERVICE, name)
        except (PasswordDeleteError, KeyringError):
            pass
