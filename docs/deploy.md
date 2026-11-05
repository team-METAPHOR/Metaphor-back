# EC2 배포와 BugLab Shop 데모 연결

API와 워커는 `/home/ubuntu/metaphor-back`에서 같은 PostgreSQL과 `STORAGE_ROOT`를
사용한다. API는 신고를 저장하고, 워커는 DB 큐의 작업을 가져와 엔진을 실행한다.
엔진·프롬프트·대상 주소 보호 정책은 이 배포 작업에서 변경하지 않는다.

팀 EC2 데모는 Caddy와 IP 기반 임시 DNS 주소로 HTTPS를 사용한다.

| 용도 | 주소 |
|---|---|
| 대시보드 | `https://metaphor-front.vercel.app` |
| API / SDK | `https://api.13-197-7-82.sslip.io` |
| 신고용 BugLab production | `https://shop.13-197-7-82.sslip.io` |
| 엔진 재현 대상 BugLab dev | `https://dev.13-197-7-82.sslip.io` |

공인 IP의 HTTP origin은 위젯 설정에서 `INVALID_ORIGIN`으로 거부된다
(`normalize_origin`: HTTPS 또는 HTTP localhost만 허용). 따라서 HTTP `:3200` 주소를
그대로 허용 origin에 넣는 절차는 현재 코드에서 동작하지 않는다. 보호 정책은 유지한다.

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
| `PUBLIC_API_BASE` | 설치 코드와 실시간 화면 `websocket_url`(`wss://`)에 사용할 외부 API 주소. 데모: `https://api.13-197-7-82.sslip.io` |
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

엔진을 올린 배포(vendor 휠 교체)는 API만 재시작해서 끝내지 않는다. 워커도 재시작한 뒤
워커가 쓰는 가상환경에서 실제 버전을 확인한다(현재 고정 `0.7.2`, `223ec38`).

```bash
uv run python -c "import importlib.metadata as m; print(m.version('metaphor-core-engine'))"
```

`/api/v1/health`는 API의 생존만 확인한다. 이번 작업에는 DB heartbeat와
`worker_seen_at` 응답을 추가하지 않는다. 워커는 systemd 상태·시작 로그·실제 큐 처리로 확인한다.

## 5. Vercel과 BugLab Shop 연결

먼저 공개 DNS가 서버 IP를 가리키는지 확인하고 Caddy를 준비한다.

```bash
sudo apt-get install -y caddy
sudo caddy validate --config deploy/Caddyfile.demo --adapter caddyfile
# 기존 /etc/caddy/Caddyfile은 먼저 백업한다.
sudo cp deploy/Caddyfile.demo /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

80/443이 열려 있어야 한다. 기존 다른 사이트의 프록시 설정이 있으면 덮어쓰지 말고 합친다.
Caddy는 공개 인증서를 발급·갱신하고 세 사이트를 각각 localhost의 API 8000,
production 3200, dev 3100에 연결한다. dev의 바인딩은 계속 localhost로 유지한다.
일반 API의 `TRUSTED_PROXIES`에는 직접 운영하는 프록시 주소만 넣는다.
팀 구성은 `["127.0.0.1","::1"]`이며 uvicorn도 기본적으로 loopback의 전달 헤더만 신뢰한다.
접속 검증에서는 인증서 검증을 끄지 않는다.
참고: [Caddy 자동 HTTPS](https://caddyserver.com/docs/automatic-https),
[IP 기반 DNS 서비스](https://nip.io/).

Vercel 프로젝트 환경변수는 `API_ORIGIN=http://13.197.7.82:8000`이며 변경하면 재배포한다.
이 주소는 Vercel 서버가 사용하는 upstream이어서 현재 구성에서도 브라우저 혼합 콘텐츠를
일으키지 않는다. HTTPS upstream으로 옮길 때는 `https://api.13-197-7-82.sslip.io`를 사용한다.
대시보드는 `/api/*`를 프록시한다. API의 `ALLOWED_ORIGINS`에도 대시보드 origin을 등록한다.
실시간 화면 WebSocket은 이 rewrite 경로로 검증되지 않았으므로, 대시보드가 일회용 티켓을 받아
`PUBLIC_API_BASE`의 `wss://` 주소로 직접 연결한다([live-preview.md](api/live-preview.md#연결)).

1. 대시보드에서 로그인하고 회사를 만든다. 기존 회사·프로그램을 사용한다면 중복 생성하지 않는다.
2. 프로그램 `spear`를 만들고 `https://dev.13-197-7-82.sslip.io`를 `base_url`로 등록한다.
   워커가 접근할 수 없는 localhost/사설 주소를 그대로 등록하지 않는다.
3. `GET /api/v1/programs/{id}/origin-verification` 또는 화면에서 받은 토큰을 BugLab의
   `server/.env.dev`의 `METAPHOR_VERIFY_TOKEN`에 넣고 dev를 재시작한다.
   production에서도 같은 경로를 확인하려면 `.env.production`에도 넣는다.
4. `GET /.well-known/metaphor-verify.txt`가 200과 토큰 원문을 반환하는지 확인한 뒤
   대시보드에서 소유 확인(`POST .../origin-verification/check`)을 실행한다.
5. 위젯 설정에서 위 표의 shop·dev HTTPS origin을 허용하고 위젯·공개 신고를 켠다.
   데모에서 production 스크린샷도 전송하려면 live screenshot을 켜고 신고 시 동의를 받는다.
   프로그램도 active,
   외부 AI 처리 허용 상태여야 실제 작업이 만들어진다.
6. 발급된 공개 프로젝트 키와 아래 값을 shop production 환경에 넣고 재시작한다.

```dotenv
METAPHOR_SDK_URL=https://api.13-197-7-82.sslip.io/sdk/v1/metaphor.js
METAPHOR_API_URL=https://api.13-197-7-82.sslip.io
# METAPHOR_PROJECT_KEY와 METAPHOR_VERIFY_TOKEN은 해당 프로그램에서 받은 값 사용
```

기존 shop이 있으면 DB를 보존한 채 빌드·재시작한다. 새 서버에서는 저장소 루트에서
`npm run setup`, `npm run build`, `npm run start:prod`를 수행한다. 이번 HTTPS 구성은
외부 요청을 Caddy가 받으므로 API·shop의 내부 포트를 추가로 공개할 필요가 없다.
기존 production 3200과 localhost dev 3100은 유지하며 HTTPS 프록시로 분리한다.
임의로 보호 정책을 해제하지 않는다.
소유 확인 경로는 dev에서도 지원하지만 SDK는 production에서만 활성화한다.

### BugLab 로그인과 서버 초기화 (엔진 0.6.0)

엔진 0.6.0은 일반 화면의 비로그인 `/api/auth/me` 401을 관찰 기록(`non_fatal`)으로 남기고 계속한다.
데모 로그인은 대시보드 테스트 계정(설정 > 재현 환경 > + 계정 추가)으로 하고, 로그인 준비 중 401 경로는
계정의 `unauthenticated_paths: ["/api/auth/me"]`로 등록한다(현재 화면에 입력란이 없으면 `PUT /programs/{id}/test-account`로 저장).
장바구니 쓰기 승인과 `buglab-v1` 초기화는 [승인된 테스트 쓰기](./api/state-changes.md)를 따른다.
워커에 `QA_RESET_BUGLAB_TOKEN`, `QA_RESET_BUGLAB_ORIGINS`(dev HTTPS 원점), `QA_RESET_BUGLAB_ACCOUNTS`(예: `qa1@buglab.test,qa2@buglab.test`)를 두고 재시작한다.

운영자 세션(`QA_SESSION_<ORG>_<REF>`)은 엔진이 처리하지 못하는 로그인(MFA·CAPTCHA·SSO)의 대안일 뿐이다.
초기화가 계정 세션을 지우므로 초기화 어댑터·쓰기 승인과 함께 쓰지 않는다(API·워커 모두 `STATE_CHANGES_SESSION_CONFLICT`).
기존 리비전에 연결된 세션 참조는 고객 API로 지울 수 없으므로 운영자가 새 리비전으로 해제한다.
(`psql -v program_id=<프로그램 id> -f unbind.sql`)

```sql
BEGIN;
SELECT current_revision FROM programs WHERE id = :'program_id' FOR UPDATE;
INSERT INTO program_scope_revisions
  (program_id, revision, base_url, allowed_path_prefixes, excluded_path_prefixes, width, height,
   app_version, approved_app_versions, test_context_id, mask_selectors, session_secret_ref,
   reset_adapter, allow_private_targets, private_cidrs, state_changes, created_by)
SELECT r.program_id, r.revision + 1, r.base_url, r.allowed_path_prefixes, r.excluded_path_prefixes,
       r.width, r.height, r.app_version, r.approved_app_versions, r.test_context_id,
       r.mask_selectors, NULL, r.reset_adapter, r.allow_private_targets, r.private_cidrs,
       r.state_changes, NULL
FROM program_scope_revisions r JOIN programs p
  ON p.id = r.program_id AND r.revision = p.current_revision
WHERE p.id = :'program_id';
UPDATE programs SET current_revision = current_revision + 1 WHERE id = :'program_id';
COMMIT;
```

이전 리비전과 실행 기록은 그대로 남는다. 이후 워커 환경에서 해당 `QA_SESSION_*` 값을 지우고 재시작한다.
실행 중인 작업은 리비전이 바뀌면 권한 확인에서 중단되므로 큐가 빈 때 적용한다.

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

데모 계정의 로그인 정보는 서버 `/home/ubuntu/.config/metaphor/demo-access.json`과
운영자 로컬 `.env.demo`(Git 제외, 권한 600)에 둔다. 비밀번호를 문서나 배포 로그에 넣지 않는다.
