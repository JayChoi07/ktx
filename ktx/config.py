"""자격 증명 저장소.

비밀번호·키는 평문 파일이 아니라 Windows 자격 증명 관리자(keyring)에만 저장한다.
(.env / 환경변수 대체 경로는 두지 않는다: '지우기'를 눌러도 값이 되살아나는 문제가 있었다.)
"""

from __future__ import annotations

import contextlib
import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = os.environ.get("KTX_KEYRING_SERVICE", "ktx-macro")  # 테스트에서 실계정과 분리하기 위한 값


def get(name: str) -> str | None:
    try:
        return keyring.get_password(SERVICE, name) or None
    except KeyringError:
        return None


def put(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def remove(*names: str) -> None:
    for name in names:
        with contextlib.suppress(PasswordDeleteError, KeyringError):  # 없는 항목 삭제는 성공으로 본다
            keyring.delete_password(SERVICE, name)
