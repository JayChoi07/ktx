"""디스코드 웹훅 알림.

채널의 웹훅 URL 하나로 알린다. URL 을 가진 사람은 누구나 그 채널에 글을 올릴 수 있으므로
비밀번호처럼 취급해 keyring 에만 저장한다.
"""

from __future__ import annotations

import re

import httpx

from . import config

KEY = "DISCORD_WEBHOOK_URL"
MAX_LEN = 2000  # 디스코드 메시지 content 최대 길이
# 디스코드 외의 호스트로는 절대 보내지 않는다 (붙여 넣은 값이 곧 요청 대상이 된다)
_URL = re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_-]+")


def configured() -> bool:
    return config.get(KEY) is not None


def normalize(url: str) -> str:
    """붙여 넣은 웹훅 URL 을 검증해 공백을 걷어낸 값을 돌려준다. 형식이 다르면 ValueError."""
    url = url.strip()
    if not _URL.fullmatch(url):
        raise ValueError("디스코드 웹후크 URL이 올바르지 않아요. 채널 설정 → 연동 → 웹후크에서 URL을 복사해 붙여 넣어 주세요.")
    return url


def send(text: str, *, mention_everyone: bool = False, url: str | None = None) -> None:
    """채널에 메시지를 보낸다. 실패하면 예외를 던진다 (호출자가 로그로 남긴다).

    웹훅은 allowed_mentions 를 생략하면 users 멘션만 파싱한다. 그래서 @everyone 을 울릴 때만 parse 에 everyone 을 넣고,
    그 외에는 빈 배열로 어떤 멘션도 울리지 않게 한다.
    """
    url = url or config.get(KEY)
    if not url:
        raise RuntimeError("디스코드 웹후크가 설정되지 않았어요")
    content = f"@everyone {text}" if mention_everyone else text
    r = httpx.post(
        url,
        json={"content": content[:MAX_LEN], "allowed_mentions": {"parse": ["everyone"] if mention_everyone else []}},
        timeout=10,
    )
    if r.is_error:  # 디스코드의 에러 코드/설명을 그대로 보여줘야 원인을 알 수 있다 (예: 10015 Unknown Webhook)
        raise RuntimeError(f"디스코드 전송 실패 ({r.status_code}): {r.text}")
