# 제보 처리 흐름 API (상태·진행 단계·담당자·번호)

제보 상세 대시보드가 쓰는 필드와 API입니다. 기준 코드는 `app/api/v1/endpoints/reports.py`,
`app/services/reports.py`, `app/worker/persist.py`이고, 문서와 코드가 다르면 코드를 따릅니다.

- 경로는 모두 `/api/v1` 아래에 있습니다. 로그인 쿠키(`sid`)가 필요하고, 쓰기 요청은 허용된 `Origin`이어야 합니다(CSRF).
- 제보가 속한 회사의 구성원이 아니면 **404 `NOT_FOUND`**, 구성원이지만 권한이 모자라면 **403 `FORBIDDEN`** 입니다. `viewer`는 조회만 할 수 있고 제보 등록·상태 변경·담당자 지정이 모두 403입니다.
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
- 나중에 실행하려면 기존 재실행 API `POST /reports/{id}/validation-jobs {"mode": "fresh"}`(developer, admin)를 씁니다. 여전히 한도를 넘으면 429 `DAILY_LIMIT_EXCEEDED`이고 제보는 계속 주차 상태입니다. 작업이 만들어지면 `run_blocked`가 지워집니다. 실행 모드는 `fresh`만 받습니다(`initial` 없음).

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

`PATCH /reports/{id}/workflow {status}` — developer, admin — 200 제보 상세

- `status`는 `received` / `fixing` / `dismissed` / `closed` 중 하나입니다. 그 밖의 값은 422 `VALIDATION_FAILED`입니다.
- `closed`는 그 순간 `fix_verified`가 `true`일 때만 됩니다. 아니면 409 `FIX_NOT_VERIFIED`입니다. 마감하면 `closed_at`을 기록합니다.
- 이미 `closed`인 제보는 바꿀 수 없습니다(다시 `closed`로 보내도): 409 `REPORT_CLOSED`.
- `dismissed` → `received`로 다시 열 수 있습니다. 같은 상태로 보내도 200(변경 없음)입니다.
- 감사 로그 `report.workflow` `{from, to}`. 마이그레이션 0007: `in_review`→`received`, `accepted`→`fixing`.

### 마감

- 자동 마감은 없습니다. 통과한 재검증은 `fix_verified`만 `true`로 만들고, 마감은 위 `PATCH`로 사람이 합니다. 감사 로그는 다른 상태 변경과 같은 `report.workflow` `{from, to: "closed"}`이고 `actor_id`는 마감한 사람입니다.
- 이전 버전에서 자동 마감된 제보(`actor_id` NULL 감사 로그)는 그대로 `closed`로 남습니다.

## 담당자 (`assignee`)

- `PATCH /reports/{id}/assignee {user_id}` (developer, admin) → 200 제보 상세. `null`이면 해제합니다. 감사 로그 `report.assign` `{from, to}`.
- 담당자는 제보 회사의 활성 구성원이어야 하며, 아니면 422 `ASSIGNEE_NOT_MEMBER`입니다(사용자 없음과 비구성원의 메시지가 같음).
- 응답: `assignee: {id, display_name, email, active} | null`.
- 담당자가 비활성화되거나 회사를 나가도 배정은 풀리지 않습니다(연쇄 해제 없음). 대신 `active: false`가 되고 `display_name`·`email`은 `null`로 가립니다.
- `GET /reports?assignee=me` 또는 `?assignee=<user_id>`를 기존 `cursor`/`limit`/`source`/`program_id`와 함께 쓸 수 있습니다.

## 제보 번호 (`number`)

- 회사마다 1부터 매기는 번호이고, 화면에는 `BUG-<number>`로 표시합니다. 위젯·플랫폼 제보가 같은 순서를 씁니다.
- 생성할 때 `organizations.report_seq`를 원자적으로 1 올려 받으므로 동시에 생성해도 겹치지 않습니다. 생성이 롤백되면 번호가 비어 있을 수 있습니다(의도된 동작).
- 기존 제보는 마이그레이션 0009에서 회사별 `created_at`, `id` 순서로 번호를 받았습니다.
- `GET /orgs/{org_id}/reports/{number}` (구성원) → 200, `GET /reports/{id}`와 같은 응답입니다. 없거나 비구성원이면 404, `number < 1`이면 422입니다.

## 제보 목록 필터·페이지 (`GET /reports`)

구성원인 회사의 제보만 최신순으로 돌려줍니다. 필터는 모두 함께 쓸 수 있습니다.

| 파라미터 | 뜻 |
|---|---|
| `progress` | 여러 번 줄 수 있습니다(`?progress=new&progress=reproduced`). 값: `new` `reproducing` `reproduced` `triaged` `verified` `waiting_quota` `fixing` `dismissed` `closed`. 그 밖의 값은 422 `VALIDATION_FAILED`. |
| `include_closed` | 기본 `false`. 마감(`closed`) 제보는 기본으로 빠지고, `include_closed=true`이거나 `progress`에 `closed`가 있을 때만 나옵니다. |
| `from` / `to` | `created_at` 범위(ISO 8601). `from`은 포함, `to`는 제외. 시간대가 없으면 UTC로 봅니다. `from > to`이면 422. |
| `q` | 제목·설명에서 대소문자 구분 없이 찾습니다. `%`, `_`도 글자 그대로 찾습니다. 최대 100자(넘으면 422). |
| `source` / `program_id` / `assignee` | 기존 필터 그대로입니다. |
| `sort` | `created_desc`(기본, 최신순) 또는 `created_asc`(오래된 순). 그 밖의 값은 422. 커서는 같은 `sort`로만 이어 씁니다. |

`progress`는 저장된 열이 아니라 계산 값이지만, 필터는 SQL에서 같은 규칙(`progress_case`, `app/services/reports.py`)으로 걸러서 `total`과 페이지가 정확합니다. `verified`는 "가장 최근에 끝난 작업이 통과한 `retest`"를 상관 서브쿼리로 확인하고(`fix_verified`와 같은 규칙), `waiting_quota`는 `reports.run_blocked = 'quota'`로 확인합니다. 두 규칙이 어긋나지 않는지 테스트가 모든 조합을 비교합니다.

### 페이지

- **페이지 번호 방식**(목록 화면용): `page`(1부터, 기본 1) + `limit`(1~100, 기본 20). 응답에 `page`와 조건에 맞는 전체 개수 `total`이 들어갑니다. 범위를 넘은 페이지는 `items: []`.
- **커서 방식**(기존): `cursor`를 주면 예전처럼 동작하고 `total`·`page`는 `null`입니다. `cursor`와 `page`를 함께 주면 422.
- 두 방식 모두 뒤에 행이 더 있으면 `next_cursor`를 줍니다.

```json
{"items": [...], "next_cursor": "eyJ...", "total": 37, "page": 1}
```

## 회사 대시보드 요약 (`GET /orgs/{org_id}/report-summary`)

`viewer`를 포함한 구성원만 볼 수 있고(읽기 전용), 아니면 404 `NOT_FOUND`입니다.

```json
{
  "counts": {"new": 3, "reproducing": 1, "reproduced": 4, "triaged": 2, "verified": 1,
             "waiting_quota": 2, "fixing": 1, "dismissed": 0, "closed": 5},
  "needs_attention": [{"id": "…", "number": 12, "title": "…", "progress": "triaged",
                       "reason": {"code": "ANSWER_QUESTIONS", "message": "추가 질문에 답하면 다시 검증합니다"}}]
}
```

- `counts`: 회사의 모든 제보(마감 포함)를 `progress`별로 셉니다. 모든 값이 키로 있고 없으면 0입니다. "재현 완료" 카드는 `reproduced + triaged`, "재현 중"은 `reproducing`, "신규"는 `new + waiting_quota`, "수정 확인됨"은 `verified`입니다.
- `needs_attention`: 최신순 최대 5건. `progress`가 `reproduced`나 `waiting_quota`(한도 대기로 주차됨)이거나, 처리 상태가 `received`이고 끝난 최신 작업에서 아래 `required_actions`가 나오는 제보입니다(`needs_info`, 매핑된 오류·차단 코드). 항목의 `progress`도 목록과 같은 규칙(`fix_verified`, `run_blocked` 포함)으로 계산합니다. `reason`은 첫 번째 필요 조치 `{code, message}`이고(주차된 제보는 `WAIT_FOR_QUOTA`), 재현 완료라 검토만 남은 경우 `null`입니다.

## 회사 재현 통계 (`GET /orgs/{org_id}/report-stats`)

`viewer`를 포함한 구성원만 볼 수 있습니다. 기간은 작업·제보의 `created_at` 기준입니다.

- `from` / `to`(ISO 8601): `from`은 포함하고 `to`는 제외합니다. `to`를 생략하면 지금까지입니다. 기간은 최대 90일이고, `from >= to`이거나 90일을 넘으면 422 `VALIDATION_FAILED`를 반환합니다. "이번 주" 같은 기준(예: KST 월요일 00:00)은 프론트가 정해서 `from`으로 보냅니다.
- `from`이 없으면 최근 `days`일입니다(기본 7). `days`는 1~90 범위로 맞추며, 범위를 벗어나면 오류 대신 가까운 끝값을 씁니다.
- `program_id`: 한 프로그램만 집계합니다. 다른 회사의 프로그램이면 404입니다.
- 응답의 `window_from`, `window_to`는 실제로 쓴 기간이고, `days`는 그 길이를 올림한 일수입니다.
- 단계별 개수(`by_progress`)는 `GET /orgs/{org_id}/report-summary`의 `counts`를 씁니다.

| 필드 | 계산 |
|---|---|
| `auto_reproduction_rate` | 기간 안에 만들어져 끝난 `initial` 작업 중, 그 제보가 끝난 `initial`/`fresh` 작업에서 `reproduced` 판정을 받은 비율(0~1). 대상이 없으면 `null`. |
| `avg_reproduction_seconds` | 같은 `initial` 작업들의 `finished_at - created_at` 평균(초). 없으면 `null`. |
| `fix_verified_count` | 기간 안의 `retest` 중 `completed` + `not_reproduced`(수정 확인)인 개수. |
| `by_page` | 기간 안 제보의 페이지 경로 상위 10개 `[{path, count}]`(많은 순, 같으면 경로순). 위젯 `client_context.reported_url`, 없으면 `target_url`의 경로(쿼리·프래그먼트 제외). |

"끝난"은 `completed` / `inconclusive` / `needs_info` / `failed`이고, `cancelled`는 재현 시도로 보지 않아 뺍니다.

## 신고별 실행 목록 (`GET /reports/{report_id}/validation-jobs`)

신고의 모든 실행(`initial` / `fresh` / `resume` / `retest`)을 최신순으로 `JobOut` 목록으로 돌려줍니다. `limit`(1~100, 기본 20)과 `cursor`로 페이지를 넘깁니다. 응답 형식은 `{"items": [...], "next_cursor": null}`입니다. 구성원이면 `viewer`도 볼 수 있고, 다른 회사의 신고면 404입니다. 소요 시간은 `finished_at - started_at`로 계산합니다.

조건 비교 작업(`has_conditions: true`)의 조건별 결과는 `GET /validation-jobs/{job_id}/conditions`로 봅니다. 형식과 권한은 [테스트 조건 비교 API](test-conditions.md#조건별-결과-get-validation-jobsjob_idconditions)를 참고하세요.

## 중복 신고 원문 (`GET /reports/{report_id}/duplicates`)

위젯 신고가 기존 신고의 중복으로 판정돼도, 그 신고의 원문은 따로 저장합니다. 저장하는 것은 설명(원본과 같은 마스킹 처리), `client_context`, 마스킹된 스크린샷입니다. 신고자에게 가는 응답은 예전과 똑같이 `200 {"status":"duplicate","receipt_id":null}`입니다.

- 신고 하나당 원문은 최대 50건까지 저장합니다. 그다음부터는 `duplicate_count`만 늘어납니다.
- 목록은 최신순이고 `limit`(1~100, 기본 20)과 `cursor`로 페이지를 넘깁니다. 각 항목은 `{id, received_at, description, has_screenshot, input_artifact_id, client_context}`이고, 스크린샷은 `GET /evidence/{input_artifact_id}/download`로 받습니다.
- 권한은 신고 상세와 같습니다. `viewer`도 볼 수 있고, 다른 회사면 404입니다.
- 목록과 상세 응답의 `latest_duplicate_excerpt`에는 가장 최근 중복 설명의 앞 120자가 들어갑니다. 중복이 없으면 `null`입니다. "'결제가 안 돼요' 외 8건" 같은 표시는 이 값과 `duplicate_count`로 만듭니다.
- 원문은 원본 신고와 같은 보존 기간을 따릅니다. 보존 기간이 지나 신고가 정리될 때 원문과 스크린샷도 함께 지웁니다.

## 필요 조치 (`required_actions`)와 최근 시도 요약 (`best_attempt`)

제보 상세(`GET /reports/{id}`, `GET /orgs/{org_id}/reports/{number}`)에 들어갑니다. 기준 코드는 `app/services/report_actions.py`입니다.

`required_actions: [{code, message}]` — 사람이 무엇을 해야 다음으로 넘어가는지 알려줍니다.

- `closed`·`dismissed` 제보는 항상 `[]`입니다.
- 작업 기반 조치는 **끝난** 최신 작업에서만 나옵니다. 작업이 진행 중이면 이전 시도의 조치는 보여주지 않습니다.
- 작업의 `backend_error_code`, `error_code`, 저장된 결과의 시도별 차단 코드(`report.run.evidence[].blocker`)를 봅니다. 표에 없는 코드는 조치를 만들지 않습니다(빈 목록).
- 순서는 아래 표 순서이고, 같은 조치는 한 번만 나옵니다.

| `code` | `message` | 나오는 조건 |
|---|---|---|
| `VERIFY_ORIGIN` | 테스트 대상 도메인의 소유 확인이 필요합니다 | 프로그램 현재 범위의 origin이 소유 확인되지 않음(새 작업이 `ORIGIN_NOT_VERIFIED`로 막힘) |
| `WAIT_FOR_QUOTA` | 오늘 실행 한도에 도달해 한도가 풀리면 다시 시도합니다 | 제보의 `run_blocked == "quota"`(한도 대기로 주차되어 아직 작업이 없음) |
| `TEST_ACCOUNT_REQUIRED` | 테스트 계정을 등록하면 다시 시도합니다 | `SESSION_SECRET_MISSING`, `SESSION_SECRET_INVALID`(워커의 테스트 세션 비밀값 없음·형식 오류), `TEST_ACCOUNT_INVALID`(저장된 계정을 열 수 없음, 다시 저장 필요), `TEST_ACCOUNT_KEY_MISSING`·`TEST_ACCOUNT_KEY_INVALID`(워커 개인키 없음·오류, 운영자 조치) |
| `CHECK_TEST_ACCOUNT` | 등록된 테스트 계정으로 로그인하지 못했습니다. 계정과 로그인 설정을 확인해 주세요 | `LOGIN_FAILED`(엔진 자동 로그인 실패: 계정 오류, 폼 불일치, 성공 신호 미확인). 작업 `error_code` 또는 재현 시도 근거의 `blocker` |
| `CHECK_TARGET_URL` | 테스트 대상 URL과 허용 범위를 확인해 주세요 | `ORIGIN_OUT_OF_SCOPE`, `PATH_OUT_OF_SCOPE`, `PATH_EXCLUDED`, `INVALID_URL`, `INVALID_HOST`, `DNS_FAILED`, `DESTINATION_BLOCKED`, `PRIVATE_DESTINATION_BLOCKED`, `NAVIGATION_FAILED`(페이지 응답 없음 또는 4xx/5xx) |
| `USE_HTTPS_FOR_CREDENTIALS` | 테스트 계정이나 세션은 HTTPS 대상에서만 사용할 수 있습니다. 프로그램 원점을 유효한 인증서가 있는 HTTPS 주소로 다시 등록해 주세요 | `CREDENTIALS_REQUIRE_HTTPS`(엔진 0.4.1 또는 워커 실행 전 검사: HTTPS가 아닌 원점에 테스트 세션·계정을 쓰려 함) |
| `CHECK_TEST_RESET` | 테스트 데이터 서버 초기화를 하지 못해 쓰기 검증을 멈췄습니다. 초기화 어댑터와 테스트 계정 설정을 확인해 주세요 | `RESET_FAILED`(엔진 0.6.0: 시도 전 초기화 실패, 쓰기 미전송), `STATE_RESET_REQUIRED`, `STATE_CHANGES_SESSION_CONFLICT`, `RESET_ACCOUNT_MISSING`, `RESET_ACCOUNT_NOT_APPROVED`, `RESET_TOKEN_MISSING`, `RESET_TARGET_NOT_APPROVED`(워커 실행 전 검사, [승인된 테스트 쓰기](./state-changes.md)) |
| `REVIEW_WRITE_APPROVAL` | 재현에 승인되지 않은 쓰기 요청이 필요했습니다. 필요한 경로만 관리자 쓰기 승인에 추가할지 검토해 주세요 | `METHOD_NOT_ALLOWED`(읽기 전용 실행에서 쓰기 시도), `STATE_CHANGE_OUT_OF_SCOPE`(승인 규칙 밖 쓰기) |
| `ANSWER_QUESTIONS` | 추가 질문에 답하면 다시 검증합니다 | 최신 작업 상태가 `needs_info` |
| `WAIT_FOR_PROVIDER` | AI 사용량 제한이 풀린 뒤 다시 검증을 요청해 주세요 | `MODEL_RATE_LIMITED`(공급자 429). 작업의 `retry_available_at` 이후 재요청 |
| `RETRY_PROVIDER_LATER` | AI 응답이 일시적으로 실패했습니다. 잠시 후 다시 요청해 주세요 | `MODEL_TIMEOUT`, `MODEL_UNAVAILABLE`(연결 실패), `MODEL_HTTP_ERROR`(그 밖의 HTTP 오류) |
| `OPERATOR_CHECK_PROVIDER` | AI 공급자 설정 문제로 중단됐습니다. 운영자 조치가 필요합니다 | `MODEL_AUTH_FAILED`(401/403, 키·권한), `MODEL_RESPONSE_TOO_LARGE`. 다시 실행해도 같은 이유로 실패합니다 |

엔진에는 로그인 화면을 따로 알려주는 코드가 없습니다. 로그인이 필요한 페이지가 4xx로 답하면 `NAVIGATION_FAILED` → `CHECK_TARGET_URL`로 보입니다.

### AI 공급자 사용량 제한

- 공급자가 429와 숫자 `Retry-After`를 주면 엔진이 `report.model_calls[].retry_after_seconds`로 기록합니다(엔진은 스스로 재시도하지 않음). 작업 응답(`GET /validation-jobs/{id}`, 작업 목록)에 `retry_after_seconds`와 `retry_available_at`(= `finished_at` + 대기 초, UTC)이 나옵니다. `MODEL_RATE_LIMITED`가 아니거나 힌트가 없으면 둘 다 `null`입니다.
- 24시간(86400초)을 넘거나 음수인 힌트는 무시합니다.
- 최신 작업이 이 대기 시간 안이면 `POST /reports/{id}/validation-jobs`(`fresh`/`retest`)는 `429 PROVIDER_RATE_LIMITED`와 `Retry-After`(남은 초, 올림) 헤더, `error.detail.retry_available_at`을 돌려줍니다. 이때는 사용자별 요청 한도를 쓰지 않습니다. 시간이 지나면 정상 접수됩니다.
- 공급자 오류로 끝난 작업은 자동 재생성(`auto_fresh`)을 하지 않습니다. 공급자 오류 본문과 키는 저장·응답하지 않습니다.

`best_attempt` — 끝난 최신 작업의 저장된 엔진 결과(`qa_results`)가 있을 때만 객체이고, 없으면(작업 없음·진행 중·보존 기간 삭제) `null`입니다.

```json
{"job_id": "…", "verdict": "reproduced", "reason_code": "SYMPTOM_OBSERVED_TWICE",
 "summary": "…", "hypothesis": "…"}
```

- `verdict`·`reason_code`: 엔진 판정(`report.run.verdict`)과 근거 코드(`SYMPTOM_OBSERVED_TWICE`, `SYMPTOM_NOT_OBSERVED_TWICE`, `ATTEMPTS_DISAGREE`, `ATTEMPT_BLOCKED`).
- `summary`: AI 분석 요약(`report.analysis.summary`), `hypothesis`: 첫 번째 원인 가설(`report.analysis.hypotheses[0].description`). 분석이 없으면 `null`입니다.
- 모델 출력이라 믿을 수 없는 데이터입니다. 다시 마스킹(`redact`)한 일반 텍스트이므로 HTML로 렌더링하지 말고 글자 그대로 보여주세요.

## 개발자 인수인계 실행 진단 (`GET /reports/{report_id}/developer-handoff`)

기존 AI 분석 필드(`state`, `summary` … `classification`)는 그대로 두고 `diagnosis_state`와
`diagnosis`를 추가했습니다. `ai_generated`는 이제 AI 분석 필드에 모델 출력이 있을 때만 `true`입니다.

- `diagnosis_state`: `not_run`(실행 없음) · `pending`(실행 중) · `cancelled`(취소, 결과 없음) ·
  `available`(엔진 진단 있음) · `derived`(옛 엔진 결과라 `run.evidence`로 시도만 복원) ·
  `unavailable`(결과나 증거 없음) · `expired`(보존 기간 만료). `available`/`derived`일 때만
  `diagnosis`가 채워집니다.
- `diagnosis.source = "execution"`: 결정적 실행 정보입니다. `outcome`은 `reproduced`,
  `not_observed`(테스트한 조건에서 증상 미관찰, 버그 없음·수정 완료가 아님), `needs_information`,
  `execution_failed`, `cancelled`, `inconclusive` 중 하나입니다. 그 밖에 `category`, `summary`,
  `interpretation`, `reason_codes`, `root_cause_confirmed`(항상 `false`), `timings`가 있습니다.
- `attempts[]`: `completed_steps`/`total_steps`, `failed_step_index`(0 기반), `next_step`
  (`action`, `target`; 입력값은 내려주지 않고 URL은 쿼리·프래그먼트 제거), `blocker`, `ready`,
  `expected_observed`, `symptom_observed`, `elapsed_seconds`, `evidence_ids`, `observation_ids`,
  `timeline[]`(`stage`, `step_index`, `elapsed_seconds`), `video`(`status`, `start_offset_seconds`,
  `duration_seconds`, `capture_error`; 파일 다운로드는 아직 없음),
  `blocked_requests[]`(엔진 0.6.0: 범위 밖이라 차단하고 계속한 하위 요청. `method`, 쿼리 제거 `url`,
  `resource_type`, `blocker`, `send_attempted`, `non_fatal`), `state_changing_requests[]`(승인된 쓰기
  감사 기록. `method`, `path`(쿼리 제거), `send_attempted`, `status`, `blocker`; 본문·쿠키·헤더 없음).
- `observed_facts[]`: 관찰된 4xx/5xx·차단 네트워크 응답(`method`, 쿼리 제거 `url`, `status`,
  `non_fatal`: 엔진이 기록만 하고 계속한 응답이면 `true`, 아니면 `null`).
- `state_changes`: 이 실행에 쓰인 쓰기 승인(`environment`, `reset_adapter_id`, `allowed_requests`),
  읽기 전용이면 `null`([승인된 테스트 쓰기](./state-changes.md)).
- `next_actions[]`: 누가(`owner`: `reporter`/`operator`/`backend`/`developer`) 무엇을 보충할지
  (`code`, `message`, `required_fields`, `retry_after_seconds`). 0.6.0은 차단한 하위 리소스가 있으면
  `REVIEW_BLOCKED_SUBRESOURCES`(`developer`)를 추가합니다.

원본 결과(`GET /validation-jobs/{id}/result`)는 엔진 결과 전체를 개인정보 마스킹만 거쳐 저장한 그대로이므로
`environment.state_changes`, `run.evidence[].blocked_requests`, `run.evidence[].state_changing_requests`,
`observations[].network[].non_fatal`, `diagnosis.next_actions`가 모두 남습니다.
- `missing_information`, `hypotheses`는 모델 출력이라 각각 `source: "ai"`를 달고, `hypotheses`는
  `root_cause_confirmed: false`인 가설입니다.

내부 파일 경로, 엔진 작업 ID, 영상 파일 이름, 세션 비밀 값은 응답에 포함하지 않습니다.
