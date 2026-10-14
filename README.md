# KTX 자동예매

KTX 매진 열차를 감시하다가 자리가 나면 **자동 예약**하고 **카카오톡**으로 알려주는 개인용 프로그램.
각자 **자기 PC**에서 실행하고, 코레일 계정과 카톡 알림도 각자 본인 것을 씁니다.
결제는 자동화하지 않습니다 — 알림을 받고 코레일톡에서 기한(약 10분) 안에 직접 결제하세요.

- 클라이언트: [pykorail](https://pypi.org/project/pykorail) (비공식, 코레일톡 API)
- 서버: FastAPI(내 PC `localhost`에서만 열림) + 백그라운드 감시 스레드
- 알림: 카카오 "나에게 보내기" REST API
- 비밀번호/키: Windows 자격 증명 관리자(keyring)에 저장

## 사용 (exe)

[Releases](../../releases/latest)에서 `ktx.exe`를 받아 실행 → 브라우저에서 첫 실행 마법사를 따라가면 됩니다.
안내 페이지: GitHub Pages (`docs/`).

## 개발

```bash
uv sync
cp .env.example .env      # (선택) 개발용. 실사용은 웹 UI 마법사에서 입력
uv run python run.py      # http://localhost:8000
uv run pyinstaller ktx.spec   # dist/ktx.exe
```

릴리스: `git tag v1.0.0 && git push --tags` → GitHub Actions가 exe를 빌드해 Releases에 올립니다.
Pages: 저장소 Settings → Pages → Source를 **GitHub Actions**로 설정.

## 카카오 앱 준비 (앱 만드는 사람이 1회)

1. <https://developers.kakao.com> 에서 앱 생성 → **REST API 키**
2. [카카오 로그인] 활성화, Redirect URI `http://localhost:8000/kakao/callback` 등록
3. [동의항목] → **카카오톡 메시지 전송(talk_message)** 선택 동의
4. 클라이언트 시크릿을 켰다면 그 값도 함께 사용자에게 전달
5. 다른 사람(예: 가족)이 로그인이 안 되면 [앱 설정 → 팀 멤버]에 그 계정을 초대

> REST API 키/시크릿은 **저장소에 넣지 말고** 사용할 사람에게 따로 전달하세요.

## 주의

- 조회 간격 최소 5초(기본 8초, ±지터). 더 짧게 하면 계정/IP 제한 위험이 있습니다.
- 이용제한 감지 시 10분간 자동 중단하고 카톡으로 알립니다.
- **VPN/클라우드 IP에서는 코레일이 차단**합니다. 일반 가정·회사 네트워크에서 실행하세요.
- 개인 사용 전용입니다. 되팔기·상업적 이용 금지. 약관 위반 소지와 책임은 사용자에게 있습니다.
- 비공식 API라 코레일 앱 업데이트 시 언제든 동작이 깨질 수 있습니다.
- `.env`, `data/` 는 절대 커밋하지 마세요 (`.gitignore` 처리됨).
