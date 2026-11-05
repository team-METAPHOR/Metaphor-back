# 실시간 재현 화면 API (WebSocket)

실행 중인 검증 작업의 **마스킹된 브라우저 화면(JPEG)과 진행 단계**를 재현이 끝나기 전에 보여 줍니다. 엔진(core_engine 0.7.0, `426b3da`)의 `LiveStream` 출력을 워커가 중계하고, API가 권한을 확인한 뒤 전달합니다. 관찰 전용이며 원격 클릭·키 입력은 없습니다. 최종 결과·영상·보고서는 기존 REST API를 그대로 씁니다. 기존 `GET /validation-jobs/{id}/events`도 유지됩니다.

## 켜기 (운영자)

| 환경변수 (워커 전용) | 기본값 | 의미 |
|---|---|---|
| `QA_LIVE_ENABLED` | 꺼짐 | `1`/`true`/`yes`/`on`이면 실행마다 새 `LiveStream(LiveOptions(fps=2))`을 엔진에 넘깁니다. 녹화(`QA_VIDEO_ENABLED`)와 독립입니다. |

꺼져 있으면 연결은 되지만 `preview: "disabled"` 상태만 옵니다.

## 연결

```
GET wss://<API>/api/v1/validation-jobs/{job_id}/live   (WebSocket upgrade)
```

인증 방법은 두 가지입니다. URL·query에는 어떤 비밀값도 넣지 않습니다.

| 방법 | 언제 | 전달 |
|---|---|---|
| **일회용 티켓** (권장, 배포) | 대시보드가 API 호스트에 직접 연결할 때. 대시보드 host-only `sid` 쿠키는 API 호스트로 가지 않습니다. | `Sec-WebSocket-Protocol: metaphor-live.v1, ticket.<ticket>` |
| `sid` 쿠키 | 같은 origin(로컬 Next rewrite 등)에서 쿠키가 그대로 전달될 때 | 쿠키 |

둘 다 보내면 티켓을 씁니다.

### 1) 티켓 발급 (REST, 기존 세션 프록시 경유)

```
POST /api/v1/validation-jobs/{job_id}/live-ticket
Cookie: sid=…            (기존 대시보드 세션)
Origin: <ALLOWED_ORIGINS 중 하나>   (CSRF 가드, 없으면 403 CSRF_REJECTED)
```

```json
200 OK
Cache-Control: no-store

{"websocket_url": "wss://api.example.com/api/v1/validation-jobs/<job_id>/live",
 "ticket": "<43자 base64url>",
 "expires_at": "2026-11-06T12:00:30+00:00",
 "subprotocol": "metaphor-live.v1"}
```

- **권한**: 작업 조회 권한과 같습니다. 신고 소유 조직의 활성 멤버(모든 역할, `viewer` 포함). 다른 조직의 작업이나 없는 작업은 똑같이 `404 NOT_FOUND`입니다. 로그인하지 않았으면 `401 UNAUTHENTICATED`입니다.
- **수명**: 30초, 한 번만 쓸 수 있습니다. 발급한 사용자·세션·조직·작업에 묶입니다. 서버에는 SHA-256 해시만 저장합니다.
- **속도 제한**: 사용자당 1분에 30개. 넘으면 `429 LIVE_TICKET_THROTTLED`입니다.
- **`websocket_url`**: `PUBLIC_API_BASE`(API 환경변수)로 만듭니다(`https://` → `wss://`, 로컬 `http://` → `ws://`). 설정되지 않았으면 `null`이고, 이때는 대시보드와 같은 origin의 `/api/v1/validation-jobs/{job_id}/live`로 연결합니다(로컬 rewrite). 운영에서는 유효한 TLS의 `https://` API 주소를 넣어야 합니다.
- 티켓은 로그, 분석 이벤트, 오류 보고, URL, `localStorage`에 남기지 마세요. 받자마자 연결에 쓰고 버립니다.

### 2) 연결

```js
const t = await fetch(`/api/v1/validation-jobs/${jobId}/live-ticket`, {
  method: "POST", credentials: "include",
}).then((r) => r.json());
const url = t.websocket_url ?? `${location.origin.replace(/^http/, "ws")}/api/v1/validation-jobs/${jobId}/live`;
const ws = new WebSocket(url, [t.subprotocol, `ticket.${t.ticket}`]);
ws.binaryType = "arraybuffer";
```

- 서버는 `metaphor-live.v1`만 선택해 응답합니다(`ws.protocol === "metaphor-live.v1"`). 티켓은 응답에 다시 싣지 않습니다.
- 티켓을 보낼 때는 `metaphor-live.v1`을 함께 보내야 하고 티켓은 하나만 보낼 수 있습니다. 어기면 handshake를 거부합니다(HTTP 403, `1008`).
- 티켓은 handshake에서 원자적으로 한 번만 소비됩니다. 같은 티켓으로 동시에 여러 번 연결해도 하나만 성공합니다. 다른 작업 경로에 낸 티켓도 소비된 뒤 `4404`로 닫힙니다.
- 쿠키로 연결할 때도 `metaphor-live.v1`을 보내도 됩니다(선택). 보내면 그대로 선택됩니다.

### 공통 규칙

- **Origin**: `ALLOWED_ORIGINS`에 있는 origin만 허용합니다. 없거나 다르면 handshake를 거부합니다(HTTP 403, close 1008). 티켓이 있어도 같습니다.
- **권한**: 신고 소유 조직의 활성 멤버(모든 역할). 다른 조직의 작업, 없는 작업, 엔진 job ID 대입은 모두 같은 `4404`로 닫습니다.
- 연결 중 10초마다 세션과 멤버십을 다시 확인합니다. 티켓으로 연결했어도 티켓을 발급한 **세션**을 계속 확인합니다. 로그아웃·비밀번호 변경·세션 만료면 `4401 UNAUTHENTICATED`, 멤버십 해제·조직 비활성화면 `4404`로 바로 닫습니다. 세션이 지워지면 그 세션의 미사용 티켓도 함께 지워집니다.
- 사용자당 동시 연결은 API 프로세스마다 4개까지입니다(`4429`).
- 클라이언트가 보내는 메시지는 무시합니다.

| close code | reason | 의미 / 프론트 처리 |
|---|---|---|
| `1000` | `JOB_ENDED` | 작업 종료. 재접속하지 말고 결과 API로 전환 |
| `1008` | — | Origin 거부 또는 잘못된 subprotocol(handshake 단계). 재접속하지 않음 |
| `4401` | `TICKET_INVALID` | 티켓이 없거나 만료·재사용·세션 철회됨. **새 티켓을 한 번** 받아 재접속. 발급이 `401`이면 로그인 화면 |
| `4401` | `UNAUTHENTICATED` | 세션이 없음/만료/철회됨. 재접속하지 말고 로그인 화면 |
| `4404` | `NOT_FOUND` | 접근 불가(권한 철회 포함). 화면을 지우고 재접속하지 않음 |
| `4429` | `TOO_MANY_CONNECTIONS` | 다른 탭을 닫거나 잠시 뒤 재시도(새 티켓) |
| `4408` | `SLOW_CONSUMER` | 5초 안에 전송을 받지 못함. backoff 후 새 티켓으로 재접속 |
| 그 외(1006 등) | — | 네트워크 단절. 제한된 backoff로 새 티켓을 받아 재접속 |

**재접속 = 새 티켓.** 티켓은 한 번 쓰면 끝이므로 재접속할 때마다 `POST .../live-ticket`부터 다시 합니다. 발급 응답별 처리:

| 발급 응답 | 프론트 처리 |
|---|---|
| `401 UNAUTHENTICATED` | 로그인 화면. 재접속 중단 |
| `403 CSRF_REJECTED` | 설정 오류(대시보드 origin이 `ALLOWED_ORIGINS`에 없음). 재접속 중단 |
| `404 NOT_FOUND` | 화면 지우기. 재접속 중단 |
| `429 LIVE_TICKET_THROTTLED` | `Retry-After`가 없으므로 수십 초 backoff 후 재시도 |
| 5xx / 네트워크 | 제한된 backoff로 재시도 |

라이브 연결 실패는 QA 실행 실패가 아닙니다. 미리보기만 "전송 장애"로 표시하고 작업 상태는 기존 REST(`GET /validation-jobs/{id}`, `/events`)로 계속 보여 주세요.

## 메시지

모든 JSON 메시지는 `kind`로 구분합니다. `schema_version`이 `live-relay-1`이면 백엔드 제어 메시지이고, `live-1`이면 엔진 메시지입니다.

### `relay_state` (text, 연결 직후 한 번 + 상태가 바뀔 때마다)

```json
{"schema_version": "live-relay-1", "kind": "relay_state",
 "job_id": "<우리 job id>", "report_id": "…", "job_status": "running",
 "preview": "live", "stream": 1, "condition_id": null,
 "updated_at": "2026-11-05T12:00:00+00:00"}
```

| `preview` | 의미 | 화면 처리 |
|---|---|---|
| `queued` | 작업이 아직 시작 전 | 대기 표시 |
| `starting` | 워커가 작업을 가져갔고 중계 준비 중 | 연결 대기 |
| `waiting` | 중계는 살아 있지만 엔진 스트림이 아직 없음(테스트 컨텍스트 잠금 대기, 조건 선택 단계) | 기존 events API로 단계 표시 |
| `live` | 미리보기 중 | 아래 엔진 메시지 표시 |
| `disabled` | 워커에서 미리보기가 꺼져 있음 | "미리보기 비활성" |
| `relay_unavailable` | 실행 중인데 중계가 10초 넘게 안 올라오거나 15초 넘게 heartbeat가 없음 | "전송 장애"(QA 실패 아님) |
| `worker_lost` | 작업 lease가 만료됨(워커 프로세스 종료) | 화면 지우기 |
| `cancelling` | 취소 처리 중 | 화면 지우기 |
| `ended` | 작업이 종료 상태(`job_status`에 최종 상태). 바로 `1000`으로 닫힘 | 화면 지우고 결과 API로 전환 |

엔진 메시지는 `live`/`waiting` 상태에서만 전달됩니다. 상태가 `live`가 아니게 되면 이전 화면을 live로 표시하지 마세요(`relay_unavailable`이면 "마지막 수신 화면"으로 남겨도 됩니다).

### `heartbeat` (text, 15초마다)

```json
{"schema_version": "live-relay-1", "kind": "heartbeat", "at": "2026-11-05T12:00:15+00:00"}
```

연결이 살아 있는지 판단할 때만 씁니다. 화면 갱신 여부는 엔진 메시지의 `created_at`으로 판단합니다.

### 엔진 메시지 (`schema_version: "live-1"`)

엔진 규격(Core_engine `docs/live-preview.md` §3)과 같습니다. 다만 다음 세 필드는 백엔드가 바꾸거나 덧붙입니다.

- `job_id`: 엔진 job ID 대신 **우리 validation job ID**. 엔진 ID는 노출하지 않습니다.
- `stream`: 이 작업 안에서 1부터 늘어나는 엔진 스트림 번호. 조건 비교 작업은 조건마다, 조건 재검증은 재검증하는 조건마다 새 스트림이 생깁니다.
- `condition_id`: 조건 작업의 현재 조건 ID. 아니면 `null`.

| kind | 전송 | 내용 |
|---|---|---|
| `status`, `progress`, `preview_warning`, `end` | text (JSON) | 엔진 규격 그대로 |
| `frame` | **binary**: 4바이트 big-endian 헤더 길이 + UTF-8 JSON 헤더 + JPEG | 엔진 decoder 예제 그대로 디코딩(`binaryType = "arraybuffer"`) |

- `sequence`는 **스트림 안에서만** 증가합니다. `stream`이 바뀌면 커서를 0으로 되돌리고, 이전 화면과 단계 표시도 지웁니다.
- 번호 공백은 정상입니다. 백엔드는 스트림별로 최근 텍스트 메시지 32개와 최신 화면 한 장만 보관합니다.
- 재접속하면 `relay_state` 다음에 현재 스트림의 보관 메시지와 최신 화면을 `sequence` 순서로 다시 받습니다. 이미 처리한 번호 이하는 버리세요.
- `progress.phase = "attempt_started"`를 받으면 이전 화면을 지웁니다(엔진 규칙).
- `kind = "end"`는 엔진 실행이 끝났다는 뜻이지 결과 저장이 끝났다는 뜻은 아닙니다. 결과는 `relay_state.preview = "ended"`를 받은 뒤 `GET /validation-jobs/{id}`와 `/result`로 확인하세요(`409 RESULT_NOT_READY`면 잠시 뒤 재시도).
- 진행 단계의 `phase = "completed"`는 한 시도의 단계 완료입니다. 작업 완료로 처리하지 마세요.

## 보안·제한

- 화면은 엔진이 마스킹한 JPEG만 중계합니다(입력란·`mask_selectors`·등록 로그인 비밀값, 로그인 화면 제외). 백엔드는 마스킹 전 원본을 다루지 않습니다.
- 미리보기 화면은 파일·영상·증거로 저장하지 않습니다. 중계 테이블(`live_previews`, UNLOGGED)은 실행 중인 작업당 한 행이고 작업 종료·취소·워커 장애 시 삭제됩니다.
- 미리보기 연결, 중계 장애, 느린 뷰어는 QA 작업 진행과 판정에 영향을 주지 않습니다.

## 배포 메모

- API와 워커가 서로 다른 호스트여도 같은 PostgreSQL만 쓰면 됩니다(`LISTEN/NOTIFY` + UNLOGGED 테이블). 브로커는 없습니다.
- WebSocket은 `/api/*` 아래 경로라 Caddy(`reverse_proxy`)는 별도 설정 없이 upgrade를 전달합니다.
- **대시보드(Vercel) → API 연결 경로.** 로컬 Next rewrite에서 WebSocket이 연결된 것만으로 실제 Vercel 배포의 외부 rewrite(`/api/*` → API 호스트) 경로에서 upgrade가 전달된다는 것은 **검증되지 않았습니다**. Vercel 자체는 Fluid Compute에서 네이티브 WebSocket을 지원하지만, 그 기능이 외부 API로 가는 rewrite를 자동으로 해결해 주지는 않습니다. 그래서 배포에서는 대시보드가 API 호스트에 **직접 `wss://`** 로 연결하고, 위의 일회용 티켓으로 인증합니다.
  - 대시보드 host-only `sid` 쿠키는 API 호스트로 가지 않습니다. 티켓 발급은 기존처럼 대시보드의 REST 프록시(쿠키)로 하고, WebSocket만 API 호스트로 직접 붙습니다.
  - API에 `PUBLIC_API_BASE=https://<API 호스트>`(유효한 TLS)를 설정해야 `websocket_url`이 채워집니다.
  - `ALLOWED_ORIGINS`에 대시보드 origin(`https://metaphor-front.vercel.app`)이 있어야 합니다. 이미 CSRF 용도로 등록되어 있습니다.
  - 실제 Vercel 배포에서 로그인 → 허가된 작업 연결 → 프레임 수신 → 재접속, 그리고 다른 조직·만료·재사용·취소 거부를 확인해야 이 경로가 검증됩니다(#107).
