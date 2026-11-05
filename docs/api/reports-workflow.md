# 제보 처리 흐름 API (상태·진행 단계·담당자·번호)

제보 상세 대시보드가 쓰는 필드와 API입니다. 기준 코드는 `app/api/v1/endpoints/reports.py`,
`app/services/reports.py`, `app/worker/persist.py`이고, 문서와 코드가 다르면 코드를 따릅니다.

- 경로는 모두 `/api/v1` 아래에 있습니다. 로그인 쿠키(`sid`)가 필요하고, 쓰기 요청은 허용된 `Origin`이어야 합니다(CSRF).
- 제보가 속한 회사의 구성원이 아니면 **404 `NOT_FOUND`**, 구성원이지만 권한이 모자라면 **403 `FORBIDDEN`** 입니다.
- 오류 응답은 공통 형식 `{"error": {"code", "message", "request_id"}}`입니다.

## 대시보드 5단계

신규 신고 → 재현 중 → 재현 완료 → 버그 수정 중 → (수정 확인됨) → 마감

화면 단계는 목록·상세 응답의 `progress` 값으로 정합니다. 서버가 `workflow_status`, `fix_verified`, 최신 검증 작업(`latest_job`)을 보고 계산하며, 표의 위쪽 행부터 먼저 맞는 값을 씁니다.

| `workflow_status` | 최신 작업 | `progress` | 화면 단계 |
|---|---|---|---|
| `closed` | 상관없음 | `closed` | 마감 |
| `dismissed` | 상관없음 | `dismissed` | (단계 밖: 버그 아님·중복) |
| `received` / `fixing` | 상관없음, `fix_verified: true` | `verified` | 수정 확인됨 (마감 대기) |
| `fixing` | 상관없음 | `fixing` | 버그 수정 중 |
| `received` | 없음, 한도 대기로 주차됨 | `waiting_quota` | 신규 신고 (한도 대기) |
| `received` | 없음 | `new` | 신규 신고 |
| `received` | `queued` / `running` / `cancelling` | `reproducing` | 재현 중 |
| `received` | 끝남, 판정 `reproduced` | `reproduced` | 재현 완료 |
| `received` | 끝남, 그 밖의 판정 또는 판정 없음 | `triaged` | 재현 완료 (재현되지 않음) |

"끝남"은 `completed` / `inconclusive` / `needs_info` / `failed` / `cancelled`입니다.

## 한도 대기 (`waiting_quota`)

- 제보를 만들 때 프로그램의 일일 검증 실행 한도(`DAILY_LIMIT_EXCEEDED`)나 위젯 일일 상한(`WIDGET_DAILY_LIMITED`, 실행 한도의 절반)에 걸려도 거절하지 않습니다. 제보는 저장하고 검증 작업만 만들지 않습니다. 이것을 "주차"라고 부릅니다.
- 단, 위젯 제보는 하루 저장 상한이 따로 있습니다. 위젯 일일 상한의 4배를 넘으면 429 `WIDGET_DAILY_LIMITED`로 거절합니다(익명 트래픽으로 저장 공간이 계속 쌓이지 않게). SDK는 이 코드를 받으면 다음 UTC 자정까지 보내지 않습니다.
- 주차된 제보는 `reports.run_blocked = "quota"`(마이그레이션 0011)이고, `workflow_status`가 `received`이면 `progress`는 `waiting_quota`입니다. 감사 로그 `report.parked` `{reason: "quota", code}`가 남습니다(`code`는 걸린 한도).
- 플랫폼 `POST /reports` 응답은 성공과 같은 201이지만 `validation_job_id: null`, `status: "waiting_quota"`입니다.
- 위젯 `POST /widget/reports` 응답은 성공과 똑같은 201 `{receipt_id, status: "received", created_at}`입니다. 고객사가 한도를 넘었다는 사실을 익명 신고자에게 알리지 않습니다.
- 시간당 남용 한도(`REPORT_RATE_LIMITED`, `WIDGET_IP_RATE_LIMITED`, `WIDGET_PROJECT_RATE_LIMITED`)는 그대로 429로 거절합니다.
- 나중에 실행하려면 기존 재실행 API `POST /reports/{id}/validation-jobs {"mode": "fresh"}`(reviewer, admin)를 씁니다. 여전히 한도를 넘으면 429 `DAILY_LIMIT_EXCEEDED`이고 제보는 계속 주차 상태입니다. 작업이 만들어지면 `run_blocked`가 지워집니다. 실행 모드는 `fresh`만 받습니다(`initial` 없음).

## 수정 확인 (`fix_verified`)

- 목록·상세 응답의 `fix_verified: bool`. 그 제보의 **가장 최근에 끝난 작업**이 `retest`이고 `completed` + 판정 `not_reproduced`이면 `true`입니다.
- 끝나지 않은 작업(`queued`/`running`/`cancelling`)은 보지 않습니다. 통과한 재검증 뒤에 새 작업이 돌고 있어도 끝날 때까지는 `true`이고, 그 작업이 다른 결과로 끝나면 `false`로 돌아갑니다.
- 재검증이 통과해도 워커는 상태를 바꾸지 않습니다. 사람이 아래 `PATCH ... {status: "closed"}`로 마감합니다. 화면은 `fix_verified`가 `true`일 때 "수정 확인이 통과했습니다. 이 버그를 마감할까요?" 확인 창을 띄웁니다.

## 처리 상태 (`workflow_status`)

| 값 | 뜻 | 바꾸는 주체 |
|---|---|---|
| `received` | 신규 | 사람 (기본값, 다시 열기) |
| `fixing` | 버그 수정 중 | 사람 |
| `dismissed` | 버그 아님·중복으로 종료 | 사람 |
| `closed` | 마감 | 사람 (수정 확인된 제보만) |

`PATCH /reports/{id}/workflow {status}` — reviewer, admin — 200 제보 상세

- `status`는 `received` / `fixing` / `dismissed` / `closed` 중 하나입니다. 그 밖의 값은 422 `VALIDATION_FAILED`입니다.
- `closed`는 그 순간 `fix_verified`가 `true`일 때만 됩니다. 아니면 409 `FIX_NOT_VERIFIED`입니다. 마감하면 `closed_at`을 기록합니다.
- 이미 `closed`인 제보는 바꿀 수 없습니다(다시 `closed`로 보내도): 409 `REPORT_CLOSED`.
- `dismissed` → `received`로 다시 열 수 있습니다. 같은 상태로 보내도 200(변경 없음)입니다.
- 감사 로그 `report.workflow` `{from, to}`. 마이그레이션 0007: `in_review`→`received`, `accepted`→`fixing`.

### 마감

- 자동 마감은 없습니다. 통과한 재검증은 `fix_verified`만 `true`로 만들고, 마감은 위 `PATCH`로 사람이 합니다. 감사 로그는 다른 상태 변경과 같은 `report.workflow` `{from, to: "closed"}`이고 `actor_id`는 마감한 사람입니다.
- 이전 버전에서 자동 마감된 제보(`actor_id` NULL 감사 로그)는 그대로 `closed`로 남습니다.

## 담당자 (`assignee`)

- `PATCH /reports/{id}/assignee {user_id}` (reviewer, admin) → 200 제보 상세. `null`이면 해제합니다. 감사 로그 `report.assign` `{from, to}`.
- 담당자는 제보 회사의 활성 구성원이어야 하며, 아니면 422 `ASSIGNEE_NOT_MEMBER`입니다(사용자 없음과 비구성원의 메시지가 같음).
- 응답: `assignee: {id, display_name, email, active} | null`.
- 담당자가 비활성화되거나 회사를 나가도 배정은 풀리지 않습니다(연쇄 해제 없음). 대신 `active: false`가 되고 `display_name`·`email`은 `null`로 가립니다.
- `GET /reports?assignee=me` 또는 `?assignee=<user_id>`를 기존 `cursor`/`limit`/`source`/`program_id`와 함께 쓸 수 있습니다.

## 제보 번호 (`number`)

- 회사마다 1부터 매기는 번호이고, 화면에는 `BUG-<number>`로 표시합니다. 위젯·플랫폼 제보가 같은 순서를 씁니다.
- 생성할 때 `organizations.report_seq`를 원자적으로 1 올려 받으므로 동시에 생성해도 겹치지 않습니다. 생성이 롤백되면 번호가 비어 있을 수 있습니다(의도된 동작).
- 기존 제보는 마이그레이션 0009에서 회사별 `created_at`, `id` 순서로 번호를 받았습니다.
- `GET /orgs/{org_id}/reports/{number}` (구성원) → 200, `GET /reports/{id}`와 같은 응답입니다. 없거나 비구성원이면 404, `number < 1`이면 422입니다.
