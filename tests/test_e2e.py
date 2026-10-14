"""실제 브라우저(Playwright)로 앱을 처음부터 끝까지 밟아 보는 E2E 테스트.

별도 포트·데이터 폴더·자격증명 저장소(ktx-macro-test)를 써서 실사용 설정을 건드리지 않는다.
코레일 실계정이 필요하므로 KTX_TEST_KORAIL_ID / KTX_TEST_KORAIL_PW 환경변수가 없으면 건너뛴다.
감시 시간대를 02:00~03:00(열차 없음)으로 잡아 실제 예약이 걸리지 않게 한다.

실행:  uv run pytest tests/test_e2e.py -v
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import keyring
import pytest
from playwright.sync_api import expect, sync_playwright

PORT = 8010
BASE = f"http://localhost:{PORT}"
ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(os.environ.get("KTX_TEST_SHOTS", ROOT / "build" / "shots"))
SERVICE = "ktx-macro-test"
NAMES = ("KORAIL_ID", "KORAIL_PW", "KAKAO_REST_API_KEY", "KAKAO_CLIENT_SECRET")
KID, KPW = os.environ.get("KTX_TEST_KORAIL_ID"), os.environ.get("KTX_TEST_KORAIL_PW")

pytestmark = pytest.mark.skipif(not (KID and KPW), reason="KTX_TEST_KORAIL_ID / KTX_TEST_KORAIL_PW 필요")


def _clean_keyring() -> None:
    for n in NAMES:
        with contextlib.suppress(Exception):
            keyring.delete_password(SERVICE, n)


def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("ktx-data")


@pytest.fixture(scope="session", autouse=True)
def server(data_dir):
    assert not _port_open(), f"포트 {PORT} 가 이미 사용 중"
    _clean_keyring()
    env = {**os.environ, "KTX_PORT": str(PORT), "KTX_DATA_DIR": str(data_dir), "KTX_KEYRING_SERVICE": SERVICE}
    env.update({"KTX_NO_BROWSER": "1", "PYTHONUTF8": "1"})
    with open(data_dir / "server.log", "w", encoding="utf-8") as log:
        proc = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(100):
            if _port_open():
                break
            time.sleep(0.2)
        else:
            proc.kill()
            pytest.fail("서버가 뜨지 않음: " + (data_dir / "server.log").read_text(encoding="utf-8")[-2000:])
        yield proc
        proc.kill()
    _clean_keyring()


@pytest.fixture(scope="session")
def browser():
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="session")
def page(browser):
    """시나리오가 이어지므로 세션 내내 같은 페이지를 쓴다."""
    ctx = browser.new_context(viewport={"width": 1100, "height": 900}, locale="ko-KR")
    pg = ctx.new_page()
    pg.goto(BASE)
    yield pg
    ctx.close()


def shot(page, name: str) -> None:
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def state() -> dict:
    return httpx.get(f"{BASE}/api/state").json()


def toast(page, kind: str = ""):
    return page.locator(f".toast{('.' + kind) if kind else ''}").last


# ---------------------------------------------------------------- 1. 첫 실행 마법사
def test_01_fresh_start_shows_korail_wizard(page):
    expect(page.locator("#wizard")).to_contain_text("코레일 계정을 연결해요")
    expect(page.locator("#app")).to_be_hidden()
    expect(page.locator("#status")).to_contain_text("코레일 계정 설정 필요")
    shot(page, "01-wizard-korail")


def test_02_wrong_password_is_rejected(page):
    page.fill("#wId", "00000000000")
    page.fill("#wPw", "wrong-password")
    page.click("#wKorail button")
    expect(toast(page, "err")).to_be_visible(timeout=20000)
    expect(page.locator("#wizard")).to_contain_text("코레일 계정을 연결해요")
    expect(page.locator("#app")).to_be_hidden()
    expect(page.locator("#wKorail button")).to_be_enabled()  # 다시 시도할 수 있어야 한다


def test_03_korail_login_succeeds(page):
    page.fill("#wId", KID)
    page.fill("#wPw", KPW)
    page.click("#wKorail button")
    expect(toast(page, "ok")).to_contain_text("연결됐어요", timeout=30000)
    expect(page.locator("#app")).to_be_visible()
    expect(page.locator("#wizard")).to_contain_text("설정 코드")  # 2단계
    expect(page.locator("#status")).to_contain_text("님 · 코레일")
    expect(page.locator("#dep")).to_have_value("서울")
    assert state()["setup"]["korail"] is True
    assert len(state()["stations"]) > 200
    shot(page, "03-wizard-kakao")


def test_04_bad_setup_code_then_skip(page):
    page.fill("#wCode", "this-is-not-a-code")
    page.click("#wKakao button[type=submit], #wKakao button:not([type=button])")
    expect(toast(page, "err")).to_contain_text("설정 코드가 올바르지 않아요")
    page.click("#skip")
    expect(page.locator("#wizard")).to_be_empty()
    expect(page.locator("#banners")).to_contain_text("카톡이 연결되지 않았어요")
    expect(page.locator("#banners button")).to_contain_text("카톡 연결하기")
    shot(page, "04-main-empty")


def test_04b_banner_does_not_blink_on_poll(page):
    """배너는 폴링(3초)마다 다시 그리지 않아야 한다 — 다시 그리면 등장 애니메이션이 반복돼 깜빡이고 스크롤 중 사라져 보인다."""
    banner = page.locator("#banners .banner.warn")
    expect(banner).to_be_visible()
    page.evaluate("document.querySelector('#banners .banner.warn').dataset.mark = 'same'")
    page.wait_for_timeout(7000)  # 폴링 2회 이상
    assert page.evaluate("document.querySelector('#banners .banner.warn').dataset.mark") == "same"
    assert page.evaluate("getComputedStyle(document.querySelector('#banners .banner.warn')).opacity") == "1"


# ---------------------------------------------------------------- 2. 새 감시 폼
def test_05_station_combobox(page):
    page.click("#dep")
    lst = page.locator("#depList")
    expect(lst).to_be_visible()
    expect(lst.locator("li").first).to_have_text("자주 가는 역")
    page.fill("#dep", "ㅂㅅ")  # 초성 검색
    expect(lst.locator("[role=option]").first).to_have_text("부산")
    page.keyboard.press("Enter")
    expect(page.locator("#dep")).to_have_value("부산")
    expect(lst).to_be_hidden()
    page.fill("#dep", "zzz")  # 없는 역
    expect(lst).to_contain_text("검색 결과가 없어요")
    page.locator("#date").evaluate("e => e.blur()")
    page.click("body", position={"x": 5, "y": 5})
    expect(page.locator("#dep")).to_have_value("부산")  # 이전 값으로 복구
    page.click("#dep")
    page.fill("#dep", "대전")
    page.keyboard.press("Enter")
    expect(page.locator("#dep")).to_have_value("대전")
    page.click("#swap")
    expect(page.locator("#dep")).to_have_value("부산")
    expect(page.locator("#arr")).to_have_value("대전")


def test_06_add_job(page):
    page.click("#routeChips .chip:has-text('서울 → 부산')")
    expect(page.locator("#dep")).to_have_value("서울")
    expect(page.locator("#arr")).to_have_value("부산")
    page.click("#dateChips .chip:has-text('내일')")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    expect(page.locator("#date")).to_have_value(tomorrow)
    expect(page.locator("#timeCustom")).to_be_hidden()
    page.click("#timeChips .chip:has-text('직접 입력')")
    expect(page.locator("#timeCustom")).to_be_visible()
    page.fill("#time_from", "02:00")  # 열차가 없는 시간대 → 실제 예약이 걸리지 않는다
    page.fill("#time_to", "03:00")
    page.locator("#time_to").dispatch_event("change")
    expect(page.locator("#timeLabel")).to_have_text("02:00 ~ 03:00")
    page.click("#optionChips .chip:has-text('일반실만')")
    page.click("#plus")
    expect(page.locator("#adults")).to_have_text("2")
    page.click("#submit")
    expect(toast(page, "ok")).to_contain_text("감시를 시작했어요")
    card = page.locator("#jobs .job").first
    expect(card).to_contain_text("서울")
    expect(card).to_contain_text("부산")
    expect(card).to_contain_text("감시 중")
    expect(card).to_contain_text("일반실만")
    expect(card).to_contain_text("2명")
    expect(page.locator("#count")).to_contain_text("감시 중 1건")
    shot(page, "06-job-added")


def test_07_job_is_actually_polled(page):
    card = page.locator("#jobs .job").first
    expect(card.locator(".chk")).not_to_have_text("0", timeout=40000)
    expect(card).to_contain_text("조건에 맞는 열차가 아직 조회되지 않았어요")
    expect(card).to_contain_text("초 전")
    j = state()["jobs"][0]
    assert j["status"] == "watching" and j["checks"] >= 1 and j["result"] is None


def test_08_stop_resume_delete(page):
    card = page.locator("#jobs .job").first
    card.locator("button:has-text('중지')").click()
    expect(card).to_contain_text("중지됨")
    card.locator("button:has-text('다시 감시')").click()
    expect(card).to_contain_text("감시 중")
    card.locator("button:has-text('삭제')").click()
    dlg = page.locator("#dlg")
    expect(dlg).to_be_visible()
    expect(dlg).to_contain_text("지울까요")
    page.click("#dlgNo")
    expect(dlg).to_be_hidden()
    expect(page.locator("#jobs .job")).to_have_count(1)
    card.locator("button:has-text('삭제')").click()
    page.click("#dlgYes")
    expect(page.locator("#jobs .job")).to_have_count(0)
    expect(page.locator("#jobs")).to_contain_text("아직 감시 중인 열차가 없어요")
    assert state()["jobs"] == []


# ---------------------------------------------------------------- 3. 설정 시트
def test_09_settings_sheet(page):
    page.click("#gear")
    sheet = page.locator("#settings")
    expect(sheet).to_be_visible()
    expect(sheet).to_contain_text("카카오톡 알림")
    expect(sheet).to_contain_text("연결 안 됨")
    expect(sheet.locator("#sTest")).to_be_disabled()
    expect(sheet).to_contain_text("코레일 계정")
    expect(sheet).to_contain_text("님")
    sheet.locator("#sInt .chip:has-text('1분')").click()
    expect(toast(page, "ok")).to_contain_text("1분")
    expect(sheet.locator("#sInt .chip[aria-pressed=true]")).to_contain_text("1분")
    expect(sheet.locator("#sIntForm")).to_be_hidden()
    assert state()["interval"] == 60
    # 직접 입력: 10초 미만은 거부, 45초는 적용
    sheet.locator("#sCustom").click()
    expect(sheet.locator("#sIntForm")).to_be_visible()
    sheet.locator("#sIntVal").fill("5")
    sheet.locator("#sIntForm button").click()
    expect(toast(page, "err")).to_contain_text("최소 10초")
    assert state()["interval"] == 60
    sheet.locator("#sIntVal").fill("45")
    sheet.locator("#sIntForm button").click()
    expect(toast(page, "ok")).to_contain_text("45초")
    expect(sheet.locator("#sInt .chip[aria-pressed=true]")).to_contain_text("직접 입력")
    expect(sheet).to_contain_text("현재 45초")
    assert state()["interval"] == 45
    sheet.locator("details.logbox summary").click()
    expect(sheet.locator("#logs")).to_contain_text("코레일")
    shot(page, "09-settings")
    page.click("#closeSet")
    expect(sheet).to_be_hidden()


def test_10_delete_korail_account_returns_to_wizard(page):
    """버그 재현: 계정을 지우면 마법사로 돌아가고 '계정 지우기'가 다시 보이면 안 된다."""
    page.click("#gear")
    page.locator("#sResetKorail").click()
    page.click("#dlgYes")
    expect(page.locator("#settings")).to_be_hidden()
    expect(page.locator("#wizard")).to_contain_text("코레일 계정을 연결해요", timeout=10000)
    expect(page.locator("#app")).to_be_hidden()
    expect(page.locator("#status")).to_contain_text("코레일 계정 설정 필요")
    s = state()
    assert s["setup"]["korail"] is False and s["account"] is None
    assert keyring.get_password(SERVICE, "KORAIL_ID") is None
    time.sleep(3)  # 감시 루프가 몰래 다시 로그인하지 않는지
    assert state()["account"] is None
    shot(page, "10-after-delete-account")
    # 설정 창에서도 '계정 지우기'가 아니라 '계정 연결하기'가 보여야 한다
    page.click("#gear")
    sheet = page.locator("#settings")
    expect(sheet).to_contain_text("코레일 계정 설정 필요")
    expect(sheet.locator("#sResetKorail")).to_have_count(0)
    expect(sheet.locator("#sLinkKorail")).to_be_visible()
    page.wait_for_timeout(400)
    shot(page, "10b-settings-without-account")
    page.click("#sLinkKorail")
    expect(sheet).to_be_hidden()
    expect(page.locator("#wizard")).to_be_in_viewport()
    # 다시 설정하면 정상 복귀
    page.fill("#wId", KID)
    page.fill("#wPw", KPW)
    page.click("#wKorail button")
    expect(page.locator("#app")).to_be_visible(timeout=30000)
    expect(page.locator("#wizard")).to_be_empty()  # 카카오는 이미 '나중에' 했으므로
    page.click("#gear")
    expect(page.locator("#settings")).to_contain_text("님")
    page.click("#closeSet")


# ---------------------------------------------------------------- 4. 예약 카드 / 카톡 설정 코드
def test_11_reserved_card_and_expiry(page, data_dir):
    limit = datetime.now() + timedelta(minutes=7)
    result = {"rsv_id": "TEST-NONE", "train_key": "x", "train": "KTX 083 서울→부산 07:00~09:40", "price": 59800, "seats": [],
              "buy_limit": limit.strftime("%m/%d %H:%M"), "buy_limit_iso": limit.strftime("%Y-%m-%dT%H:%M:00")}
    job = {"id": "rsvtest", "dep": "서울", "arr": "부산", "date": limit.strftime("%Y-%m-%d"), "time_from": "06:00", "time_to": "10:00",
           "option": "GENERAL_FIRST", "train_type": "KTX", "adults": 1, "status": "reserved", "checks": 5, "last_checked": None,
           "last_checked_ts": time.time(), "result": result}
    (data_dir / "jobs.json").write_text(json.dumps([job], ensure_ascii=False), encoding="utf-8")
    page.reload()
    sec = page.locator("#rsvSection")
    expect(sec).to_be_visible()
    expect(sec).to_contain_text("예약됐어요!")
    expect(sec).to_contain_text("59,800원")
    expect(sec.locator(".ring b")).to_have_text(re.compile(r"0[56]:\d\d"), timeout=5000)
    expect(sec.locator("button:has-text('예약 취소')")).to_be_visible()
    shot(page, "11-reserved")
    # 코레일에 없는 예약이므로 모니터가 '사라짐'으로 정리해야 한다
    expect(page.locator("#jobs .job").first).to_contain_text("종료", timeout=90000)
    expect(sec).to_be_hidden()
    page.click("#gear")
    page.locator("details.logbox summary").click()
    expect(page.locator("#logs")).to_contain_text("목록에서 사라짐")
    page.click("#closeSet")


def test_12_kakao_setup_code_roundtrip():
    r = httpx.post(f"{BASE}/api/setup/kakao", json={"rest_api_key": "testkey-1234567890", "client_secret": "sec-xyz"})
    assert r.status_code == 200
    assert state()["setup"]["kakao_key"] is True
    code = httpx.get(f"{BASE}/api/setup/kakao/code").json()["code"]
    raw = json.loads(base64.urlsafe_b64decode(code + "=" * (-len(code) % 4)))
    assert raw == {"k": "testkey-1234567890", "s": "sec-xyz"}
    r = httpx.get(f"{BASE}/kakao/login", follow_redirects=False)
    assert r.status_code in (302, 307) and "client_id=testkey-1234567890" in r.headers["location"]
    assert f"localhost%3A{PORT}%2Fkakao%2Fcallback" in r.headers["location"]
    assert httpx.post(f"{BASE}/api/setup/kakao", json={"code": "bad"}).status_code == 400
    assert httpx.post(f"{BASE}/api/setup/kakao", json={"code": code}).status_code == 200  # 코드로 재설정
    assert httpx.post(f"{BASE}/api/setup/reset/kakao").status_code == 200
    assert state()["setup"]["kakao_key"] is False
    assert httpx.get(f"{BASE}/api/setup/kakao/code").status_code == 404


# ---------------------------------------------------------------- 5. 모바일 / 연결 끊김 / 보안
def test_13_mobile_layout(browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, locale="ko-KR", is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    pg.goto(BASE)
    expect(pg.locator("#app")).to_be_visible()
    expect(pg.locator("#jobs .job")).to_have_count(1)  # 11번의 '종료' 카드
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "가로 스크롤 발생"
    expect(pg.locator("#formBox")).not_to_have_attribute("open", "")
    expect(pg.locator("#newBtn")).to_be_visible()
    pg.click("#newBtn")
    expect(pg.locator("#formBox")).to_have_attribute("open", "")
    expect(pg.locator("#submit")).to_be_in_viewport()
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "폼 펼친 뒤 가로 스크롤 발생"
    pg.tap("#gear")
    expect(pg.locator("#settings")).to_be_visible()
    assert pg.evaluate("document.querySelector('#settings').getBoundingClientRect().right <= window.innerWidth")
    pg.wait_for_timeout(400)  # 열림 애니메이션(0.25s)이 끝난 뒤 찍어야 반투명하게 보이지 않는다
    assert pg.evaluate("getComputedStyle(document.querySelector('#settings')).opacity") == "1"
    pg.screenshot(path=str(SHOTS / "13-mobile-settings.png"))
    pg.tap("#closeSet")
    pg.screenshot(path=str(SHOTS / "13-mobile.png"), full_page=True)
    ctx.close()


def test_14_connection_lost_banner(page):
    page.route("**/api/state", lambda route: route.abort())
    expect(page.locator("#banners")).to_contain_text("연결이 끊겼어요", timeout=15000)
    shot(page, "14-disconnected")
    page.unroute("**/api/state")
    expect(page.locator("#banners")).not_to_contain_text("연결이 끊겼어요", timeout=15000)


def test_15_local_only_guard():
    assert httpx.get(f"{BASE}/api/state", headers={"Host": "evil.example"}).status_code == 403
    assert httpx.post(f"{BASE}/api/jobs", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert httpx.get(f"{BASE}/docs").status_code == 404
