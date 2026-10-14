# KTX 자동예매

코레일 KTX 매진 열차를 감시하다가 자리가 나면 **자동 예약**하고 **카카오톡**으로 알려주는 개인용 웹 UI.
결제는 자동화하지 않습니다 — 알림을 받고 코레일톡에서 기한(약 10분) 안에 직접 결제하세요.

- 클라이언트: [pykorail](https://pypi.org/project/pykorail) (비공식, 코레일톡 API)
- 서버: FastAPI + 백그라운드 감시 스레드, 설정/작업은 `data/*.json` 에 저장
- 알림: 카카오 "나에게 보내기" REST API

## 실행

```bash
uv sync
cp .env.example .env      # 코레일 ID/PW, 카카오 키 입력
uv run python run.py      # http://localhost:8000
```

**반드시 집 PC 등 일반 가정용 IP에서 실행하세요.** VPN/클라우드 IP는 코레일이 차단합니다.

## 카카오톡 연결 (1회)

1. <https://developers.kakao.com> 에서 앱 생성 → **REST API 키**를 `.env` 의 `KAKAO_REST_API_KEY` 에 입력
2. [카카오 로그인] 활성화, Redirect URI 에 `http://localhost:8000/kakao/callback` 등록
3. [동의항목] → **카카오톡 메시지 전송(talk_message)** 설정 (선택 동의)
4. 웹 UI 상단 **카카오 연결하기** → 로그인/동의 → "카톡 테스트 전송"으로 확인

토큰은 `data/kakao.json` 에 저장되며 자동 갱신됩니다.

## 주의

- 조회 간격 최소 5초(기본 8초, ±지터). 더 짧게 하면 계정/IP 제한 위험이 있습니다.
- 이용제한(-2000 등) 감지 시 10분간 자동 중단하고 카톡으로 알립니다.
- 개인 사용 전용입니다. 되팔기·상업적 이용 금지. 코레일 약관 위반 소지와 책임은 사용자에게 있습니다.
- 비공식 API라 코레일 앱 업데이트 시 언제든 동작이 깨질 수 있습니다.
- `.env`, `data/` 는 절대 커밋하지 마세요 (`.gitignore` 처리됨).
