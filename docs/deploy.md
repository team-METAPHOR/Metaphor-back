# EC2 배포와 BugLab Shop 데모 연결

API와 워커는 `/home/ubuntu/metaphor-back`에서 같은 PostgreSQL과 `STORAGE_ROOT`를
사용한다. API는 신고를 저장하고, 워커는 DB 큐의 작업을 가져와 엔진을 실행한다.
엔진·프롬프트·대상 주소 보호 정책은 이 배포 작업에서 변경하지 않는다.

## 1. 환경변수

API 공통 설정은 `/home/ubuntu/metaphor-back/.env`, 워커 전용 설정은
`/home/ubuntu/metaphor-back/.env.worker`에 두고 둘 다 권한을 600으로 설정한다.
두 파일은 Git과 배포 rsync 대상에서 제외된다. API 서비스는 `.env.worker`를 읽지 않는다.
API 설정을 셸에서 `source`하지 않는다(`APP_NAME`의 공백과 JSON 배열은 셸 문법과 다르다).

| API `.env` | 설명 |
|---|---|
| `DATABASE_URL` | PostgreSQL SQLAlchemy URL. 워커도 같은 DB 사용 |
| `STORAGE_ROOT` | 증거 저장 경로. 같은 작업 디렉터리의 상대 경로 또는 동일한 절대 경로 |
| `ALLOWED_ORIGINS` | JSON 배열. `https://metaphor-front.vercel.app`, 필요 시 로컬 프론트 주소 추가 |
| `IP_HASH_SECRET` | UTF-8 기준 32바이트 이상의 무작위 비밀 값. 운영에서는 누락 시 시작 실패. 기존 값을 유지 |
| `TRUSTED_PROXIES` | 직접 접속이면 `[]`. 직접 운영하는 프록시의 IP/CIDR만 등록 |
| `PUBLIC_API_BASE` | 설치 코드에 사용할 외부 API 주소. 데모: `http://13.197.7.82:8000` |
| `TEST_ACCOUNT_PUBLIC_KEY` | 선택. 테스트 계정 암호화용 RSA 공개키 PEM |

| 워커 `.env.worker` | 설명 |
|---|---|
| `OPENAI_API_KEY` | 필수. 키 값은 로그·코드·문서에 남기지 않음 |
| `OPENAI_MODEL` | 필수. 이번 데모는 `gpt-5.4-mini` |
| `QA_CRITIC_PROVIDER` | 선택. 비우면 비판 모델 끔. 현재 지원 값은 `groq` |
| `GROQ_API_KEY`, `GROQ_MODEL` | 비판 모델 사용 시 필수. 이번 데모 모델은 `openai/gpt-oss-120b` |
| `QA_VIDEO_ENABLED` | 선택. `true`이면 마스킹된 재현 영상. 기본 `false` |
| `FFMPEG_BINARY` | 선택. 보통 worker 그룹의 imageio-ffmpeg 사용 |
| `TEST_ACCOUNT_PRIVATE_KEY` | 테스트 계정을 쓸 때 공개키와 일치하는 RSA 개인키 |
| `QA_SESSION_<ORG>_<REF>` | 운영자 세션을 사용할 때 JSON 헤더 |

`.env.worker`에서 `DATABASE_URL`, `STORAGE_ROOT` 등 공통 설정을 덮어쓰지 않는다.
키 파일을 만들거나 편집할 때 내용이 터미널·셸 기록에 찍히지 않게 한다.

## 2. 서버 1회 준비

```bash
cd /home/ubuntu/metaphor-back
uv sync --locked --group worker
uv run --group worker playwright install --with-deps chromium
chmod 600 .env .env.worker
```

시스템 라이브러리 설치에는 sudo가 필요하다. 브라우저는 서비스 사용자 `ubuntu`로
설치한다. Playwright 버전을 올리면 해당 버전의 브라우저를 다시 설치해야 한다.
참고: [Playwright 브라우저 설치 문서](https://playwright.dev/python/docs/browsers).

기존 서버의 워커 설정이 `/home/ubuntu/.config/metaphor/worker.env`에 있다면
새 `.env.worker`로 기존 내용을 옮긴 뒤 모델 키·모델명을 추가한다. 기존 암호화 키는 재사용한다.
두 경로를 따로 수정하며 운영하지 않는다.

테스트 계정 키가 **없는 서버에서만** 다음 명령으로 생성한다.

```bash
uv run python -m app.worker.keys /home/ubuntu/.config/metaphor/test-account-keys
```

공개키는 API `TEST_ACCOUNT_PUBLIC_KEY`, 개인키는 워커 `TEST_ACCOUNT_PRIVATE_KEY`에
넣는다. PEM 줄바꿈을 한 줄의 리터럴 `\n`으로 표현해도 된다. 이미 암호화된 계정이 있다면
새 키를 생성해 덮어쓰지 않는다. 개인키 분실 시 기존 계정을 복호화할 수 없다.

## 3. systemd 등록

```bash
sudo cp deploy/metaphor-back.service deploy/metaphor-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now metaphor-back.service
# .env.worker 준비와 시작 검사 성공을 확인한 뒤 실행한다.
sudo systemctl enable --now metaphor-worker.service
sudo systemctl status metaphor-worker.service --no-pager
sudo journalctl -u metaphor-worker.service -f
```

배포 계정의 sudoers에 기존 API 재시작 허용과 함께 다음 항목을 추가한다.
`visudo`로 문법을 확인한다.

```sudoers
ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl restart metaphor-worker.service
```

워커는 시작 시 모델 키·모델 설정 형식, 해시 키, DB 연결, Chromium 실행을 한 번 검사한다.
실패하면 다음 코드만 로그에 남기고 종료한다. 외부 모델의 실제 인증·잔액·응답은
시작 검사에서 호출하지 않으므로 실제 작업에서 별도로 확인해야 한다.

- `WORKER_MODEL_KEY_MISSING`, `WORKER_MODEL_CONFIG_INVALID`
- `WORKER_HASH_SECRET_INVALID`, `WORKER_DATABASE_UNAVAILABLE`
- `WORKER_CHROMIUM_UNAVAILABLE`
- 성공: `WORKER_STARTUP_OK`
- 영상 인코더가 없으면 기존 `VIDEO_ENCODER_UNAVAILABLE` 경고를 남긴다. 영상 없이 작업은 가능하다.

종료 신호를 받으면 `WORKER_STOP_REQUESTED`를 남기고 새 작업을 가져오지 않는다.
진행 중인 작업은 lease 갱신과 기존 종료 처리를 유지하며 완료 또는 210초 제한까지 기다린다.
`KillMode=mixed`는 먼저 감독 프로세스에만 SIGTERM을 보내며, `TimeoutStopSec=240`은
처리 여유를 준다. 비정상 종료로 남은 lease는 다음 워커가 만료 후 정리한다.

## 4. 자동 배포

CI 성공 후 main을 rsync하고 `uv sync --locked --group worker`, 마이그레이션,
API 재시작을 수행한다. 워커 unit이 등록된 서버에서는 워커도 재시작하고 재시작 루프를 확인한다.
미등록 서버에서는 안내만 남긴다. 서비스 파일 갱신은 자동 설치되지 않으므로 변경 시
3장의 복사와 daemon-reload를 다시 수행한다.

`/api/v1/health`는 API의 생존만 확인한다. 이번 작업에는 DB heartbeat와
`worker_seen_at` 응답을 추가하지 않는다. 워커는 systemd 상태·시작 로그·실제 큐 처리로 확인한다.

## 5. Vercel과 BugLab Shop 연결

Vercel 프로젝트 환경변수는 `API_ORIGIN=http://13.197.7.82:8000`이며 변경하면 재배포한다.
대시보드는 `/api/*`를 프록시한다. API의 `ALLOWED_ORIGINS`에도 대시보드 origin을 등록한다.

1. 대시보드에서 로그인하고 회사를 만든다. 기존 회사·프로그램을 사용한다면 중복 생성하지 않는다.
2. 프로그램을 만들고 공개 테스트 주소를 `base_url`로 등록한다. HTTP 데모는 로그인 없는
   재현으로 제한한다. 워커가 접근할 수 없는 localhost/사설 주소를 그대로 등록하지 않는다.
3. `GET /api/v1/programs/{id}/origin-verification` 또는 화면에서 받은 토큰을 BugLab의
   `server/.env.production`의 `METAPHOR_VERIFY_TOKEN`에 넣고 shop을 재시작한다.
4. `GET /.well-known/metaphor-verify.txt`가 200과 토큰 원문을 반환하는지 확인한 뒤
   대시보드에서 소유 확인(`POST .../origin-verification/check`)을 실행한다.
5. 위젯 설정에서 shop origin을 허용하고 위젯·공개 신고를 켠다. 프로그램도 active,
   외부 AI 처리 허용 상태여야 실제 작업이 만들어진다.
6. 발급된 공개 프로젝트 키와 아래 값을 shop production 환경에 넣고 재시작한다.

```dotenv
METAPHOR_SDK_URL=http://13.197.7.82:8000/sdk/v1/metaphor.js
METAPHOR_API_URL=http://13.197.7.82:8000
# METAPHOR_PROJECT_KEY와 METAPHOR_VERIFY_TOKEN은 해당 프로그램에서 받은 값 사용
```

기존 shop이 있으면 DB를 보존한 채 빌드·재시작한다. 새 서버에서는 저장소 루트에서
`npm run setup`, `npm run build`, `npm run start:prod`를 수행한다. 외부 접속에는
`HOST=0.0.0.0`과 공개 포트가 필요하다. 서버의 기존 production은 3200, dev는
localhost:3100으로 분리돼 있으므로 dev를 대상으로 쓰려면 별도의 공개 테스트 주소를
준비하고 소유 확인해야 한다. 임의로 보호 정책을 해제하지 않는다.
소유 확인 경로는 dev에서도 지원하지만 SDK는 production에서만 활성화한다.

## 6. 실제 검증

- API `/api/v1/health`: 200과 `{"status":"ok"}`.
- SDK `/sdk/v1/metaphor.js`: 200과 `Access-Control-Allow-Origin: *`.
- `systemctl is-active metaphor-worker`: active, 로그에 `WORKER_STARTUP_OK`, 재시작 루프 없음.
- shop 소유 확인 경로: 토큰과 일치. HTML의 `</head>` 앞에 SDK script 존재.
- shop SDK로 익명 신고 → 대시보드 목록 저장 → queued에서 running → 최종 판정과 증거 확인.
- 정상 정책 신고와 엔진 실패를 구분한다. 작업이 끝났다는 것만으로 버그가 재현된 것은 아니다.

실제 신고·모델 호출·브라우저 재현·대시보드 표시를 확인하지 않았다면 연결 완료로 적지 않는다.
HTTP API/SDK는 HTTPS 고객 사이트에서 혼합 콘텐츠로 차단된다. 실서비스에는 HTTPS API가 필요하다.
엔진 0.4.1은 HTTP 대상으로 테스트 계정·세션을 전송하지 않는다. 기존 HTTP 계정 조합은
`scope.credentials_blocked_by_http=true`로 표시될 수 있다.

설정 화면 설치 코드 개선, 신고자 마스킹 UI, SDK 통합 e2e는 별도 이슈 범위다.
