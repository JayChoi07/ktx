"""화면(HTML/JS) UI 테스트 — 서버 없이 index.html 을 브라우저로 띄우고 /api/* 응답을 흉내 낸다.

코레일 실계정이 필요한 test_e2e.py 와 달리 CI 에서도 돈다. 날짜 라벨 같은 화면 회귀를 잡는 용도다.
브라우저: 번들 Chromium(uv run playwright install chromium), 없으면 설치된 Chrome/Edge. 둘 다 없으면 건너뛴다.

실행:  uv run pytest tests/test_ui.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "ktx" / "static" / "index.html").read_text(encoding="utf-8")
ORIGIN = "http://localhost:8000"
DOW = "일월화수목금토"  # JS getDay() 순서(일요일=0)
RELEASE = "https://github.com/JayChoi07/ktx/releases/tag/v9.9.9"


def state(**over) -> dict:
    s = {
        "login": "로그인됨 (테스트)", "account": "테스트", "paused_for": 0, "interval": 30.0, "min_interval": 10.0, "max_interval": 3600.0,
        "version": "1.3.1", "update": None, "notify_error": None, "setup": {"korail": True, "discord": True},
        "stations": ["서울", "부산", "대전"],
        "options": {"GENERAL_FIRST": "전부 (일반실 먼저)", "GENERAL_ONLY": "일반실만", "SPECIAL_ONLY": "특실만"},
        "jobs": [], "logs": [],
    }  # fmt: skip
    s.update(over)
    return s


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        for kw in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
            try:
                b = p.chromium.launch(**kw)
                break
            except Exception:
                continue
        else:
            pytest.skip("브라우저를 띄울 수 없어요 (uv run playwright install chromium)")
        yield b
        b.close()


@pytest.fixture
def open_page(browser):
    """open_page(state, posts={"/api/..": {"status": 400, "json": {...}}}) → (page, 서버로 간 요청 목록)"""
    contexts = []

    def _open(st: dict, posts: dict | None = None):
        ctx = browser.new_context(viewport={"width": 1000, "height": 900}, locale="ko-KR")
        contexts.append(ctx)
        page, sent = ctx.new_page(), []

        def handle(route):
            req, path = route.request, urlparse(route.request.url).path
            if req.method == "POST":
                sent.append((path, json.loads(req.post_data) if req.post_data else None))
            if path == "/":
                route.fulfill(body=INDEX, content_type="text/html; charset=utf-8")
            elif path == "/api/state":
                route.fulfill(json=st)
            elif req.method == "POST" and path in (posts or {}):
                route.fulfill(**posts[path])
            else:
                route.fulfill(status=404, json={"detail": "mock 에 없는 요청"})

        page.route("**/*", handle)
        page.goto(ORIGIN + "/")
        return page, sent

    yield _open
    for c in contexts:
        c.close()


def label_of(d: datetime) -> str:
    return f"{d.month}월 {d.day}일 ({DOW[(d.weekday() + 1) % 7]})"


# ---------------------------------------------------------------- 날짜
def test_date_chip_updates_the_label(open_page):
    """칩을 누르면 코드로 input 값만 바뀌어 change 이벤트가 안 떴고, 날짜 라벨이 예전 값에 머물렀다."""
    page, _ = open_page(state())
    expect(page.locator("#dateLabel")).to_have_text(label_of(datetime.now()))
    page.click("#dateChips .chip:has-text('내일')")
    tomorrow = datetime.now() + timedelta(days=1)
    expect(page.locator("#date")).to_have_value(tomorrow.strftime("%Y-%m-%d"))
    expect(page.locator("#dateLabel")).to_have_text(label_of(tomorrow))
    page.click("#dateChips .chip:has-text('오늘')")
    expect(page.locator("#dateLabel")).to_have_text(label_of(datetime.now()))


# ---------------------------------------------------------------- 디스코드 마법사 / 설정
def test_wizard_has_two_steps_and_shows_the_server_error(open_page):
    detail = "디스코드 웹후크 URL이 올바르지 않아요. 채널 설정 → 연동 → 웹후크에서 URL을 복사해 붙여 넣어 주세요."
    page, sent = open_page(
        state(setup={"korail": True, "discord": False}),
        posts={"/api/setup/discord": {"status": 400, "json": {"detail": detail}}},
    )
    wiz = page.locator("#wizard")
    expect(wiz).to_contain_text("디스코드 알림을 받을 준비를 해요")
    expect(wiz).to_contain_text("시작하기 2 / 2")
    assert page.locator("#wizard .steps div").count() == 2
    page.fill("#wUrl", "not-a-webhook")
    page.click("#wDiscord button >> nth=0")
    expect(page.locator(".toast.err").last).to_contain_text("웹후크 URL이 올바르지 않아요")
    expect(page.locator("#wDiscord button >> nth=0")).to_be_enabled()  # 다시 시도할 수 있어야 한다
    assert sent == [("/api/setup/discord", {"url": "not-a-webhook"})]


def test_skipping_the_wizard_leaves_a_banner_that_reopens_it(open_page):
    page, _ = open_page(state(setup={"korail": True, "discord": False}))
    page.click("#skip")
    expect(page.locator("#wizard")).to_be_empty()
    expect(page.locator("#banners")).to_contain_text("디스코드 알림이 설정되지 않았어요")
    page.click("#bannerDiscord")
    expect(page.locator("#wizard")).to_contain_text("웹후크 URL")


def test_settings_sheet_shows_discord_and_version(open_page):
    page, _ = open_page(state())
    page.click("#gear")
    sheet = page.locator("#settings")
    expect(sheet).to_contain_text("디스코드 알림")
    expect(sheet.locator("#sTest")).to_be_enabled()
    expect(sheet.locator("#sResetDiscord")).to_be_visible()
    expect(sheet).to_contain_text("ktx v1.3.1")
    page.click("#closeSet")
    page2, _ = open_page(state(version="dev", setup={"korail": True, "discord": False}))
    page2.click("#gear")
    expect(page2.locator("#settings")).to_contain_text("(개발 실행)")
    expect(page2.locator("#settings #sTest")).to_be_disabled()
    expect(page2.locator("#settings #sLinkDiscord")).to_be_visible()


# ---------------------------------------------------------------- 예약 완료 카드
def paid_job() -> dict:
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "id": "paid1", "dep": "서울", "arr": "부산", "date": tomorrow, "time_from": "08:00", "time_to": "10:00", "option": "GENERAL_FIRST",
        "train_type": "KTX", "adults": 2, "status": "paid", "checks": 7, "last_checked": None, "last_checked_ts": 0,
        "result": {
            "rsv_id": "320261019830259", "train_key": "x", "train": "KTX 103 서울→부산 09:28~12:35", "price": 97200,
            "buy_limit": "10/15 09:18", "buy_limit_iso": "2026-10-15T09:18:00", "seats": [],
            "paid": {"seats": ["10호차 1B", "16호차 1C"], "price": 97200},
        },
    }  # fmt: skip


def test_paid_job_is_shown_as_a_completed_booking(open_page):
    """결제까지 끝난 건이 '종료'로 보이던 것 — 열차·좌석·금액이 보이는 '예약 완료'여야 한다."""
    page, _ = open_page(state(jobs=[paid_job()]))
    card = page.locator("#jobs .job.paid")
    expect(card).to_have_count(1)
    expect(card).to_contain_text("예약 완료!")
    expect(card).to_contain_text("결제 완료")
    expect(card).to_contain_text("KTX 103 서울→부산 09:28~12:35")
    expect(card).to_contain_text("10호차 1B")
    expect(card).to_contain_text("16호차 1C")
    expect(card).to_contain_text("97,200원")
    expect(card).not_to_contain_text("종료")
    expect(card.locator("button:has-text('다시 감시')")).to_have_count(0)  # 이미 결제한 건은 다시 감시할 이유가 없다
    expect(card.locator("button:has-text('목록에서 지우기')")).to_be_visible()
    expect(page.locator("#rsvSection")).to_be_hidden()  # '결제 필요' 구역에는 올라가지 않는다


# ---------------------------------------------------------------- 배너
def test_notify_failure_banner_opens_settings(open_page):
    page, _ = open_page(state(notify_error="디스코드 전송 실패 (404): Unknown Webhook"))
    banner = page.locator("#banners .banner.err")
    expect(banner).to_contain_text("디스코드 알림이 전송되지 않고 있어요")
    expect(banner).to_contain_text("Unknown Webhook")
    page.click("#bannerNotify")
    expect(page.locator("#settings")).to_be_visible()


def test_update_banner_links_to_the_release_and_can_be_dismissed(open_page):
    page, _ = open_page(state(update={"version": "9.9.9", "url": RELEASE}))
    banner = page.locator("#banners .banner.info")
    expect(banner).to_contain_text("새 버전 v9.9.9")
    expect(banner.locator("a")).to_have_attribute("href", RELEASE)
    page.click("#bannerUpdateLater")
    expect(page.locator("#banners")).not_to_contain_text("새 버전")
    page.reload()
    expect(page.locator("#app")).to_be_visible()
    page.wait_for_timeout(500)
    expect(page.locator("#banners")).not_to_contain_text("새 버전")  # 같은 버전은 다시 보여주지 않는다


def test_no_banners_when_everything_is_fine(open_page):
    page, _ = open_page(state())
    expect(page.locator("#app")).to_be_visible()
    expect(page.locator("#banners")).to_be_empty()
