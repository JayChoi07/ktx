"""새 버전 확인 단위 테스트 — 실제 네트워크 없이 GitHub 응답을 흉내 낸다.

실행:  uv run pytest tests/test_update.py -v
"""

from __future__ import annotations

import pytest

from ktx import update

RELEASE = "https://github.com/JayChoi07/ktx/releases/tag/v1.4.0"


class FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.is_error = body, status >= 400
        self.status_code = status

    def json(self):
        return self._body


@pytest.fixture
def github(monkeypatch):
    calls = []

    def serve(body, status=200):
        def fake_get(url, **kw):
            calls.append(url)
            if isinstance(body, Exception):
                raise body
            return FakeResponse(body, status)

        monkeypatch.setattr(update.httpx, "get", fake_get)
        return calls

    return serve


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("v1.3.1", (1, 3, 1)), ("1.10.0", (1, 10, 0)), (" v2 ", (2,)), ("dev", None), ("v1.4.0-rc1", None), ("", None)],
)
def test_parse(raw, expected):
    assert update._parse(raw) == expected


def test_newer_release_is_reported(github):
    github({"tag_name": "v1.4.0", "html_url": RELEASE})
    assert update.check("1.3.1") == {"version": "1.4.0", "url": RELEASE}


def test_versions_compare_numerically_not_as_text(github):
    github({"tag_name": "v1.10.0", "html_url": RELEASE})
    assert update.check("1.9.0")["version"] == "1.10.0"  # 문자열 비교면 "1.10" < "1.9" 가 된다


@pytest.mark.parametrize("current", ["1.4.0", "1.5.0"])
def test_same_or_older_release_is_ignored(github, current):
    github({"tag_name": "v1.4.0", "html_url": RELEASE})
    assert update.check(current) is None


def test_dev_run_never_calls_the_network(github):
    calls = github({"tag_name": "v9.9.9", "html_url": RELEASE})
    assert update.check("dev") is None
    assert calls == []


def test_network_error_and_http_error_are_ignored(github):
    github(ConnectionError("offline"))
    assert update.check("1.3.1") is None
    github({"message": "rate limited"}, status=403)
    assert update.check("1.3.1") is None


def test_malformed_response_is_ignored(github):
    github({"unexpected": True})
    assert update.check("1.3.1") is None


def test_link_outside_this_repo_is_not_trusted(github):
    """응답이 이상한 주소를 담고 있어도 화면에 링크로 내보내지 않는다."""
    github({"tag_name": "v1.4.0", "html_url": "https://evil.example/ktx.exe"})
    assert update.check("1.3.1") is None
