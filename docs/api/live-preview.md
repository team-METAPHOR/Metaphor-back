# 실시간 재현 화면 API (WebSocket)

실행 중인 검증 작업의 **마스킹된 브라우저 화면(JPEG)과 진행 단계**를 재현이 끝나기 전에 보여 줍니다. 엔진(core_engine 0.5.0, `093535d`)의 `LiveStream` 출력을 워커가 중계하고, API가 권한을 확인한 뒤 전달합니다. 관찰 전용이며 원격 클릭·키 입력은 없습니다. 최종 결과·영상·보고서는 기존 REST API를 그대로 씁니다. 기존 `GET /validation-jobs/{id}/events`도 유지됩니다.

## 켜기 (운영자)

| 환경변수 (워커 전용) | 기본값 | 의미 |
|---|---|---|
| `QA_LIVE_ENABLED` | 꺼짐 | `1`/`true`/`yes`/`on`이면 실행마다 새 `LiveStream(LiveOptions(fps=2))`을 엔진에 넘깁니다. 녹화(`QA_VIDEO_ENABLED`)와 독립입니다. |

꺼져 있으면 연결은 되지만 `preview: "disabled"` 상태만 옵니다.

## 연결

```
GET wss://<API>/api/v1/validation-jobs/{job_id}/live   (WebSocket upgrade)
```

- **인증**: 기존 로그인 세션 쿠키 `sid`. URL에 토큰을 넣지 않습니다.
- **Origin**: `ALLOWED_ORIGINS`에 있는 origin만 허용합니다. 없거나 다르면 handshake를 거부합니다(HTTP 403, close 1008).
- **권한**: 신고 소유 조직의 활성 멤버(모든 역할, `viewer` 포함). 다른 조직의 작업, 없는 작업, 엔진 job ID 대입은 모두 같은 `4404`로 닫습니다.
- 연결 중 10초마다 세션과 멤버십을 다시 확인합니다. 로그아웃·세션 만료면 `4401`, 멤버십 해제·조직 비활성화면 `4404`로 바로 닫습니다.
- 사용자당 동시 연결은 API 프로세스마다 4개까지입니다(`4429`).
- 클라이언트가 보내는 메시지는 무시합니다.

| close code | reason | 의미 / 프론트 처리 |
|---|---|---|
| `1000` | `JOB_ENDED` | 작업 종료. 재접속하지 말고 결과 API로 전환 |
| `1008` | — | Origin 거부(handshake 단계). 재접속하지 않음 |
| `4401` | `UNAUTHENTICATED` | 로그인 필요. 재접속하지 않음 |
| `4404` | `NOT_FOUND` | 접근 불가(권한 철회 포함). 화면을 지우고 재접속하지 않음 |
| `4429` | `TOO_MANY_CONNECTIONS` | 다른 탭을 닫거나 잠시 뒤 재시도 |
| `4408` | `SLOW_CONSUMER` | 5초 안에 전송을 받지 못함. backoff 후 재접속 |
| 그 외(1006 등) | — | 네트워크 단절. 제한된 backoff로 재접속 |

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
- **Vercel rewrite는 WebSocket을 프록시하지 않습니다.** 그래서 대시보드가 API origin에 직접 연결해야 합니다. 이때 다음 조건이 필요합니다.
  - `ALLOWED_ORIGINS`에 대시보드 origin(`https://metaphor-front.vercel.app`)이 있어야 합니다. 이미 CSRF 용도로 등록되어 있습니다.
  - `sid` 쿠키가 API 호스트로 전송되어야 합니다. 지금은 Vercel 프록시를 통해 대시보드 호스트에 host-only 쿠키로 설정되므로, API 호스트로 직접 연결하면 쿠키가 가지 않습니다. 대시보드와 API를 같은 사이트(예: `app.example.com`/`api.example.com`)에 두고 API가 직접 쿠키를 설정하거나, 짧은 수명의 1회용 연결 티켓(REST로 발급 → `Sec-WebSocket-Protocol`로 전달)을 추가하는 결정이 필요합니다.
