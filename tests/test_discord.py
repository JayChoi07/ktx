"""디스코드 웹훅 알림 단위 테스트 — 실제 네트워크 없이 요청 본문과 에러 처리를 검증한다.

실행:  uv run pytest tests/test_discord.py -v
"""

from __future__ import annotations

import pytest

from ktx import discord

URL = "https://discord.com/api/webhooks/123456789012345678/abcDEF_ghi-JKL0123456789"


class FakeResponse:
    def __init__(self, status=204, text=""):
        self.status_code, self.text = status, text
        self.is_error = status >= 400


@pytest.fixture
def posted(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append({"url": url, **kw})
        return FakeResponse()

    monkeypatch.setattr(discord.httpx, "post", fake_post)
    return calls


# ---------------------------------------------------------------- URL 검증
@pytest.mark.parametrize(
    "url",
    [
        URL,
        f"  {URL}\n",  # 붙여 넣을 때 따라오는 공백
        "https://discordapp.com/api/webhooks/1/abc",
        "https://ptb.discord.com/api/v10/webhooks/1/abc",
    ],
)
def test_normalize_accepts_webhook_urls(url):
    assert discord.normalize(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://discord.com/api/webhooks/1/abc",  # http 는 거부
        "https://evil.example/api/webhooks/1/abc",  # 다른 호스트로는 절대 보내지 않는다
        "https://discord.com.evil.example/api/webhooks/1/abc",
        "https://discord.com/api/webhooks/1",  # 토큰 없음
        "https://discord.com/channels/1/2",  # 웹훅이 아닌 주소
        f"{URL}?wait=true",
        "아무거나",
    ],
)
def test_normalize_rejects_everything_else(url):
    with pytest.raises(ValueError, match="웹후크 URL"):
        discord.normalize(url)


# ---------------------------------------------------------------- 전송
def test_send_plain_message_suppresses_all_mentions(posted):
    discord.send("[KTX] 안내", url=URL)
    (call,) = posted
    assert call["url"] == URL
    # 웹훅은 allowed_mentions 를 생략하면 users 만 파싱한다. 일반 알림은 아무도 울리지 않도록 비워 둔다.
    assert call["json"] == {"content": "[KTX] 안내", "allowed_mentions": {"parse": []}}


def test_send_with_mention_pings_everyone(posted):
    discord.send("[KTX 예약 성공]", mention_everyone=True, url=URL)
    body = posted[0]["json"]
    # @everyone 은 본문에 있고 parse 에 everyone 이 있어야만 울린다
    assert body["content"] == "@everyone [KTX 예약 성공]"
    assert body["allowed_mentions"] == {"parse": ["everyone"]}


def test_send_truncates_to_discord_limit(posted):
    discord.send("가" * 5000, url=URL)
    assert len(posted[0]["json"]["content"]) == discord.MAX_LEN == 2000


def test_send_error_includes_status_and_reason(monkeypatch):
    monkeypatch.setattr(discord.httpx, "post", lambda url, **kw: FakeResponse(404, '{"message": "Unknown Webhook", "code": 10015}'))
    with pytest.raises(RuntimeError, match=r"404.*Unknown Webhook"):
        discord.send("x", url=URL)


def test_send_uses_saved_url_and_fails_when_missing(posted, monkeypatch):
    saved = {}
    monkeypatch.setattr(discord.config, "get", lambda name: saved.get(name))
    with pytest.raises(RuntimeError, match="설정되지 않았어요"):
        discord.send("x")
    assert not posted
    saved[discord.KEY] = URL
    discord.send("x")
    assert posted[0]["url"] == URL


def test_configured_follows_keyring(monkeypatch):
    saved = {}
    monkeypatch.setattr(discord.config, "get", lambda name: saved.get(name))
    assert discord.configured() is False
    saved[discord.KEY] = URL
    assert discord.configured() is True
