# AI 평가 지원 API (이슈 #105)

실제 신고 접수 → 워커 → 보고서 조회 경로로 AI 평가를 돌릴 때 쓰는 평가자 전용 API입니다.
기준 코드는 `app/api/v1/endpoints/evaluation.py`, `app/services/evaluation.py`이고, 문서와 코드가 다르면 코드를 따릅니다.

- 경로는 모두 `/api/v1` 아래에 있고 로그인 쿠키(`sid`)가 필요합니다. 쓰기 요청은 허용된 `Origin`이어야 합니다(CSRF).
- 제보가 속한 회사의 구성원이 아니면 **404 `NOT_FOUND`** 입니다. 쓰기는 `admin`·`developer`만 가능하고 `viewer`는 403입니다.

## 평가 태그

평가용으로 접수한 제보 하나에 평가 슬롯 `(eval_run_id, case_id, repeat_id)`를 붙입니다.

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| `PUT` | `/reports/{report_id}/eval-tag` | admin·developer | 태그 설정(이미 있으면 교체). 감사 로그 `report.eval_tag` |
| `GET` | `/reports/{report_id}/eval-tag` | 구성원 | 태그 조회. 없으면 404 |

```json
{"eval_run_id": "buglab-qa-v1.run-1", "case_id": "B02", "repeat_id": "r1"}
```

- 각 값은 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`만 허용합니다. 공백·한글·자유 문장은 거절(422 `VALIDATION_FAILED`, 입력값은 응답에 되돌려주지 않음)해서 정답·참조 계획을 태그에 섞을 수 없게 합니다.
- 같은 회사에서 같은 슬롯은 제보 하나에만 붙습니다. 다른 제보가 이미 쓰고 있으면 **409 `EVAL_SLOT_TAKEN`**. 같은 사례를 다시 접수하려면 `repeat_id`를 새로 정합니다.
- **AI·엔진·대상 화면에 보내지 않습니다.** 태그는 `reports`와 분리된 `report_eval_tags` 테이블에만 있고, 워커는 이 테이블을 읽지 않습니다. 제보 상세·목록·개발자 전달·작업 응답에도 나오지 않습니다.
- 위젯으로 접수한 제보도 대시보드에서 제보 id를 찾아 태그를 붙일 수 있습니다. 태그는 접수 뒤에 붙여도 시간 계산은 제보 접수 시각(`reports.created_at`) 기준이라 영향이 없습니다.
- 위젯 중복 접수로 기존 제보에 합쳐진 제출(`report_duplicates`)은 제보가 아니므로 태그를 붙일 수 없고, 새 QA 건으로 세지 않습니다.

## 제보별 평가 기록

`GET /reports/{report_id}/eval-record` (구성원). 저장된 행(`reports`, `validation_jobs`, `job_events`, `qa_results`, `validation_job_conditions`, `artifacts`)만으로 계산하며 엔진·워커를 호출하지 않습니다. 태그가 없는 제보도 조회할 수 있고 그때 `eval`은 `null`입니다.

### 결과(`outcome`)

QA 작업은 `initial`·`fresh`·`resume`입니다. 수정 재검증(`retest`)은 `retests`로만 세고 QA 결과·재시도·시간 계산에서 뺍니다.

| 값 | 조건 |
|---|---|
| `report_ready` | QA 작업 중 하나라도 `completed` (판정이 난 보고서를 조회할 수 있음) |
| `needs_info` / `inconclusive` / `failed` / `cancelled` | 완료된 QA 작업이 없고 마지막 QA 작업 상태가 그 값 |
| `timeout` | 마지막 QA 작업이 `failed`이고 오류 코드에 `QA_RUNTIME_LIMIT`·`CONDITION_RUNTIME_LIMIT`·`RUNTIME_LIMIT`·`WORKER_TIMEOUT` 중 하나가 있음. 공급자 시간 초과(`MODEL_TIMEOUT`)는 모델 실패라 `failed` |
| `unfinished` | 마지막 QA 작업이 `queued`·`running`·`cancelling` |
| `not_run` | QA 작업이 없음(예: 일일 한도로 주차, `run_blocked: "quota"`) |

`needs_info`(추가 질문)와 `inconclusive`는 보고서 파일이 있어도 성공(`report_ready`)으로 세지 않습니다. 재현 성공과 질문 성공을 합산하지 않기 위해서입니다.

### 1건 처리 시간

- **`processing_ms` = 신고 접수(`received_at` = `reports.created_at`) → 처음으로 `completed`된 QA 작업의 결과 저장(`report_ready_at`)**. `report_ready`일 때만 값이 있습니다.
- 그 밖의 종료(`failed`·`timeout`·`cancelled`·`needs_info`·`inconclusive`)는 `ended_at`·`ended_ms`(접수 → 마지막 QA 작업 종료)에만 들어갑니다. 성공 건 시간과 섞지 않습니다. `unfinished`·`not_run`은 둘 다 `null`입니다.
- `report_ready_at`은 그 작업의 `report_readable_at`입니다. 워커가 결과·보고서(`report.md`)·스크린샷·영상·JSON 산출물을 **한 트랜잭션**으로 저장하고, 커밋 **직후** `clock_timestamp()`로 이 시각을 기록합니다. 그 순간부터 API가 보고서와 영상 등 부가 산출물을 함께 내려주며, 영상만 늦게 붙는 경우는 없습니다. 이 필드가 생기기 전에 저장된 작업은 `report_readable_at`이 `null`이라 `finished_at`(트랜잭션 시작 시각, 실제 커밋보다 1초 미만 이를 수 있음)을 대신 씁니다.
- `retries` = QA 작업 수 − 1. 자동 재생성(`fresh`, 불확실 판정 뒤)은 작업의 `automatic: true`로 구분됩니다. `claims`는 같은 작업을 워커가 가져간 횟수입니다.
- 위젯 중복 접수는 `duplicate_submissions`로만 보이고 QA 건수에 들어가지 않습니다.

### 작업별 구간(`jobs[].timing`, ms)

| 필드 | 구간 |
|---|---|
| `queue_wait_ms` | 작업 생성(`queued_at`) → 워커 claim(`claimed_at`) |
| `setup_ms` | claim → 첫 엔진 이벤트. 계정별 실행 잠금 대기, 테스트 계정 열기, 엔진 준비 |
| `engine_ms` | 첫 엔진 이벤트 → 엔진 종료 이벤트 |
| `model_ms` | 엔진이 잰 모델 호출 시간 합(`timings.model_seconds`) |
| `browser_ms` | 브라우저 재현 시도 2회의 `elapsed_seconds` 합 |
| `login_reset_ms` | 엔진이 따로 보고하지 않아 항상 `null`(unknown) |
| `persist_ms` | 엔진 종료 → 보고서 조회 가능(`report_readable_at`, 없으면 `persisted_at`) |
| `total_ms` | 작업 생성 → 보고서 조회 가능(같은 규칙) |

`timing.persisted_at`은 결과 트랜잭션 시작(`finished_at`), `timing.report_readable_at`은 커밋 직후 시각입니다. 워커 오류처럼 결과를 저장하지 않은 종료는 `report_readable_at`이 `null`입니다.

엔진이 결과를 내지 못한 실패(워커 오류, 리스 만료 `WORKER_LOST` 등)는 엔진 시각·모델 호출이 비어 있고 `result_available: false`입니다. 보존 기간이 지나 원시 결과가 지워진 작업도 `result_available: false`입니다.

### 워커 준비 확인 스냅샷 (`jobs[].readiness`)

워커가 엔진 실행을 시작할 때(claim 뒤, 실행 잠금·엔진 호출 전) **그 워커가 실제로 쓰는 설정**을 작업 행(`validation_jobs.readiness`)에 남깁니다. 레포 `main`과 배포 워커가 같다고 가정하지 않기 위해서입니다. 실행 전 입력 검사에서 실패한 작업(`RESET_ADAPTER_UNKNOWN` 등)과 이 기능 이전 작업은 `null`입니다.

| 키 | 내용 |
|---|---|
| `engine` | `package_version`(워커 가상환경에 설치된 `metaphor-core-engine` 버전), `commit`(워커 코드의 `ENGINE_COMMIT`) |
| `models.primary` / `models.critic` | `provider`(`openai`/`groq`), `model`(설정 모델 ID), `timeout_seconds`. 비판 모델이 없으면 `critic: null` |
| `critic` | `configured_provider`(`QA_CRITIC_PROVIDER` 값, 비어 있으면 `null`), `enabled`(비판 모델을 실제로 만들었는지). 코드 주석이 아니라 이 운영 설정 하나로 켜고 끕니다 |
| `limits` | `qa`(엔진 `QALimits`: 호출 수·수리·실행 시간·토큰·계획 수정), `conditions`(조건 매트릭스일 때 `ConditionLimits`), `browser`(`BROWSER_LIMITS`), `worker_hard_deadline_seconds`, `lease_seconds`, `provider_retry`(아래 429 정책 설정) |
| `browser` | `name`, `width`, `height`, `step_timeout_ms`, `navigation_timeout_ms`, `record_video` (조건 매트릭스는 `conditions[]`에 조건별 브라우저·뷰포트) |
| `scope` | `revision`(작업의 범위 리비전), `origin` |
| `reset_adapter_id`, `state_changes_adapter_id` | 초기화 어댑터 ID와 쓰기 승인에 연결된 어댑터 ID(읽기 전용이면 `null`) |
| `test_account` | `test_context_id`(역할), `login`(저장된 테스트 계정으로 로그인하는지), `session_reference`(세션 참조가 있는지) |

API 키, 세션 헤더, 로그인 ID·비밀번호, 초기화 토큰, 제보 본문은 넣지 않습니다. 데모 서버의 health 커밋은 워커가 대상 서버를 따로 호출하지 않으므로 기록하지 않습니다(평가 실행 전에 별도로 확인).

- `jobs[].critic_config_matches`: 스냅샷의 `critic.enabled`와 실제 실행의 `plan_review.enabled`가 같으면 `true`, 다르면 `false`, 어느 한쪽을 모르면 `null`. 비판 모델이 설정됐는데 단일 모델로 돌았다면 `false`로 드러납니다.
- `totals.readiness_configs`: 스냅샷이 있는 작업들 사이의 서로 다른 설정 조합(엔진·모델·비판 모델·제한) 수. 평가 중 설정을 바꾸지 않았다면 1입니다.
- CSV 열: `engine_version`, `engine_commit`, `primary_model`, `critic_model`, `critic_configured`, `critic_config_matches`(결과를 정한 작업 기준).

### 오류 코드와 모델 호출

- `jobs[].error_codes`는 그 작업에서 나온 코드를 순서대로 모두 남깁니다: 워커(`backend_error_code`) → 엔진(`error_code`) → 재현 시도별 차단 코드(`TRANSPORT_ERROR`, `STEP_OR_READINESS_FAILED` 등) → 계획 검토 오류 → 실패한 모델 호출 상태 → 예산 거절(`model_limit_events[].error_code`). 공급자 429, 엔진 호출/토큰 상한(`MODEL_CALL_LIMIT`·`MODEL_TOKEN_LIMIT`), 브라우저 실패를 하나로 합치지 않습니다. 제보의 `error_codes`는 QA 작업 전체의 합집합입니다.
- `jobs[].model_calls[]`: `phase`, `role`(primary/critic), `status`, `model`(공급자가 돌려준 실제 모델 ID, 실패 시 `null`), `configured_model`(엔진 0.7.0이 기록한 워커 설정 모델 ID. 실패 호출에도 있음, 이전 결과는 `null`), `usage_known`, `input_tokens`/`output_tokens`(사용량이 없으면 `null`, 0으로 취급하지 않음), `latency_ms`, `retry_after_seconds`(429의 Retry-After).
- `jobs[].model_limit_events[]`(엔진 0.7.0): 작업별 호출·토큰 예산 때문에 **시작하지 않은** 호출(`phase`, `role`, `configured_model`, `error_code`=`MODEL_CALL_LIMIT`/`MODEL_TOKEN_LIMIT`, `call_started: false`). 실제 호출이 아니므로 `model_calls`·실패 호출 수·`totals.model_calls`에 넣지 않고 `totals.model_limit_events`와 CSV `model_limit_events` 열로 따로 셉니다.
- `jobs[].model_calls_source`: `result`(저장된 엔진 결과) 또는 `model_usage`. 보존 기간이 지나 원시 결과가 지워져도 `model_usage` 행(단계, 역할, 상태, 실제·설정 모델, 토큰, `latency_ms`, `retry_after_seconds`)은 남으므로 모델 호출 목록을 그 행으로 채웁니다. 둘 다 없으면 `null`이고 목록은 비어 있습니다. 예산 거절은 `model_usage`에 저장하지 않습니다(실제 호출이 아님).
- `plan_review`는 `{enabled, status, error_code}`만 보여 줍니다. 비판 모델 사용 여부는 이 `enabled`로 확인합니다.
- 엔진 job id, 저장 경로, 범위 스냅샷, 모델 요청·응답 본문은 넣지 않습니다. 정제된 JSON 결과는 `result_url`, 증거 목록은 `artifacts_url`로 받습니다.

## 평가 실행 내보내기

`GET /orgs/{org_id}/eval-runs/{eval_run_id}/export` (구성원, `?format=json` 기본 / `?format=csv`)

- 그 회사에서 `eval_run_id` 태그가 붙은 제보를 **상태와 관계없이 모두** 넣습니다: 성공, 실패, 시간 초과, 보류(`needs_info`·`inconclusive`), 취소, 진행 중(`unfinished`), 미실행(`not_run`). 슬롯 하나당 한 행이고 `case_id`, `repeat_id` 순으로 정렬합니다.
- 없는 실행 id는 빈 결과(200)입니다. 회사 구성원이 아니면 404, 실행 id 형식이 틀리면 422. 한 실행에 제보가 2000건을 넘으면 422 `EVAL_RUN_TOO_LARGE`.
- JSON: `{eval_run_id, generated_at, totals, items}`. `items[]`는 위 제보별 평가 기록과 같습니다. `totals`는 제보 수, 중복 접수 수, QA 작업 수, 재검증 수, 모델 호출 수(사용량 unknown 수 포함), 브라우저 시도 수, `report.md`가 저장된 작업 수(`report_files`), 결과별 건수입니다. 신고 건수·모델 호출 수·브라우저 시도 수·보고서 수를 따로 다시 셀 수 있습니다.
- CSV: 제보당 한 행. 시간 구간 열(`queue_wait_ms` 등)은 결과를 정한 작업(처음 완료된 QA 작업, 없으면 마지막 QA 작업) 기준입니다. `input_tokens`/`output_tokens`는 사용량을 보고한 호출만 더하고, 보고한 호출이 없으면 빈 칸(0이 아님)이며 `usage_unknown_calls`로 빠진 수를 셉니다. 여러 값은 `|`로 잇습니다(`models`는 실제 응답 모델, `configured_models`는 설정 모델). `=`·`+`·`-`·`@`로 시작하는 문자열 셀은 스프레드시트 수식으로 실행되지 않게 앞에 `'`를 붙입니다.
- 계정·비밀번호·세션·토큰, 모델 요청·응답 본문, 엔진 job id, 저장 경로는 넣지 않습니다. 보고서와 증거는 `report_url`·`jobs[].result_url`·`jobs[].artifacts_url` 링크로만 연결하며, 받으려면 같은 권한 검사를 다시 거칩니다.

## 공급자 429 정책 (워커)

엔진은 모델 호출을 스스로 재시도하지 않습니다. 워커가 결과를 저장한 뒤 모델 호출 중 `MODEL_RATE_LIMITED`가 있으면 기존 작업 큐(`validation_jobs`) 안에서만 다음처럼 처리합니다. 별도 브로커나 무한 재시도는 없습니다.

1. **공급자 일시 정지**: 그 호출의 공급자(`role=primary` → `openai`, `role=critic` → `QA_CRITIC_PROVIDER` 값)를 `provider_backoffs`에 `지금 + Retry-After`까지 멈춥니다. 숫자 `Retry-After`가 없으면 30초, 최대 24시간입니다. 이미 더 늦은 정지가 있으면 유지합니다. 그 공급자를 쓰는 워커(주 모델은 항상 `openai`)는 정지 중 **어떤 작업도 claim하지 않습니다**. 대기 중인 다른 제보도 같은 한도에 걸릴 것이기 때문입니다.
2. **지연된 자동 재시도 1건**: 작업이 `failed`/`inconclusive`이고 엔진 `error_code`가 `MODEL_RATE_LIMITED`이며, `initial`/`fresh` 작업이고 제보의 최신 작업이면 공용 `create_job`으로 `fresh` 작업을 만들고 `run_after = 지금 + 대기 시간`으로 둡니다. 워커는 `run_after` 전에는 claim하지 않습니다. 감사 기록 `job.provider_retry`(`source_job_id`, `provider`, `attempt`, `delay_seconds`, `chain_started_at`)를 남깁니다.
3. **상한**: 같은 사슬의 자동 재시도는 `QA_PROVIDER_RETRY_MAX`(기본 1, 0이면 끔, 최대 3)회까지, 사슬 첫 작업 생성 후 `QA_PROVIDER_RETRY_DEADLINE_SECONDS`(기본 900초) 안에 시작할 수 있을 때만입니다. `Retry-After`가 `QA_PROVIDER_RETRY_MAX_WAIT_SECONDS`(기본 300초)보다 길면 자동 재시도하지 않습니다(공급자 정지는 그대로). 일일 실행 한도·활성 작업·소유 확인 등 `create_job` 거절도 재시도 없이 끝납니다. 조건 매트릭스, `resume`, `retest` 작업은 자동 재시도하지 않습니다.
4. **비판 모델 실패**: 비판 모델의 429도 작업 실패로 기록됩니다. 엔진은 비판 모델 없이 단일 모델로 바꿔 계속하지 않고, 워커도 재시도 작업을 같은 `QA_CRITIC_PROVIDER` 설정으로 돌립니다. 준비 확인 스냅샷의 `critic.enabled`와 `plan_review.enabled`를 비교한 `critic_config_matches`로 확인합니다.

평가 기록에서 자동 재시도 작업은 `automatic: true`, `automatic_reason: "provider_retry"`(불확실 판정 뒤 자동 재생성은 `"auto_fresh"`), `run_after`로 보이고 `retries`에 들어갑니다. 작업 응답(`GET /validation-jobs/{id}`)에도 `run_after`가 있습니다. 대기 중인 재시도는 다른 작업처럼 취소할 수 있습니다. 사람이 요청하는 재실행은 기존처럼 최신 작업의 `retry_available_at`까지 `429 PROVIDER_RATE_LIMITED`입니다([보고서 흐름](./reports-workflow.md)).

평가 중에는 이 설정과 `QA_CRITIC_PROVIDER`를 바꾸지 마세요. 재시도 설정은 준비 확인 스냅샷의 `limits.provider_retry`에 남으므로, 바뀌면 `totals.readiness_configs`가 1보다 커집니다.
