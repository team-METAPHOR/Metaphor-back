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
- `report_ready_at`은 `validation_jobs.finished_at`입니다. 워커가 결과·보고서(`report.md`)·스크린샷·영상·JSON 산출물을 **한 트랜잭션**으로 저장하므로, 이 트랜잭션이 커밋되면 보고서와 영상 등 부가 산출물이 함께 조회 가능해집니다. 영상만 늦게 붙는 경우는 없습니다. 이 값은 트랜잭션 시작 시각(PostgreSQL `now()`)이라 실제 커밋보다 산출물 저장 시간만큼 이를 수 있습니다(보통 1초 미만).
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
| `persist_ms` | 엔진 종료 → 결과 저장 |
| `total_ms` | 작업 생성 → 결과 저장 |

엔진이 결과를 내지 못한 실패(워커 오류, 리스 만료 `WORKER_LOST` 등)는 엔진 시각·모델 호출이 비어 있고 `result_available: false`입니다. 보존 기간이 지나 원시 결과가 지워진 작업도 `result_available: false`입니다.

### 오류 코드와 모델 호출

- `jobs[].error_codes`는 그 작업에서 나온 코드를 순서대로 모두 남깁니다: 워커(`backend_error_code`) → 엔진(`error_code`) → 재현 시도별 차단 코드(`TRANSPORT_ERROR`, `STEP_OR_READINESS_FAILED` 등) → 계획 검토 오류 → 실패한 모델 호출 상태 → 예산 거절(`model_limit_events[].error_code`). 공급자 429, 엔진 호출/토큰 상한(`MODEL_CALL_LIMIT`·`MODEL_TOKEN_LIMIT`), 브라우저 실패를 하나로 합치지 않습니다. 제보의 `error_codes`는 QA 작업 전체의 합집합입니다.
- `jobs[].model_calls[]`: `phase`, `role`(primary/critic), `status`, `model`(공급자가 돌려준 실제 모델 ID, 실패 시 `null`), `configured_model`(엔진 0.7.0이 기록한 워커 설정 모델 ID. 실패 호출에도 있음, 이전 결과는 `null`), `usage_known`, `input_tokens`/`output_tokens`(사용량이 없으면 `null`, 0으로 취급하지 않음), `latency_ms`, `retry_after_seconds`(429의 Retry-After).
- `jobs[].model_limit_events[]`(엔진 0.7.0): 작업별 호출·토큰 예산 때문에 **시작하지 않은** 호출(`phase`, `role`, `configured_model`, `error_code`=`MODEL_CALL_LIMIT`/`MODEL_TOKEN_LIMIT`, `call_started: false`). 실제 호출이 아니므로 `model_calls`·실패 호출 수·`totals.model_calls`에 넣지 않고 `totals.model_limit_events`와 CSV `model_limit_events` 열로 따로 셉니다.
- `plan_review`는 `{enabled, status, error_code}`만 보여 줍니다. 비판 모델 사용 여부는 이 `enabled`로 확인합니다.
- 엔진 job id, 저장 경로, 범위 스냅샷, 모델 요청·응답 본문은 넣지 않습니다. 정제된 JSON 결과는 `result_url`, 증거 목록은 `artifacts_url`로 받습니다.

## 평가 실행 내보내기

`GET /orgs/{org_id}/eval-runs/{eval_run_id}/export` (구성원, `?format=json` 기본 / `?format=csv`)

- 그 회사에서 `eval_run_id` 태그가 붙은 제보를 **상태와 관계없이 모두** 넣습니다: 성공, 실패, 시간 초과, 보류(`needs_info`·`inconclusive`), 취소, 진행 중(`unfinished`), 미실행(`not_run`). 슬롯 하나당 한 행이고 `case_id`, `repeat_id` 순으로 정렬합니다.
- 없는 실행 id는 빈 결과(200)입니다. 회사 구성원이 아니면 404, 실행 id 형식이 틀리면 422. 한 실행에 제보가 2000건을 넘으면 422 `EVAL_RUN_TOO_LARGE`.
- JSON: `{eval_run_id, generated_at, totals, items}`. `items[]`는 위 제보별 평가 기록과 같습니다. `totals`는 제보 수, 중복 접수 수, QA 작업 수, 재검증 수, 모델 호출 수(사용량 unknown 수 포함), 브라우저 시도 수, `report.md`가 저장된 작업 수(`report_files`), 결과별 건수입니다. 신고 건수·모델 호출 수·브라우저 시도 수·보고서 수를 따로 다시 셀 수 있습니다.
- CSV: 제보당 한 행. 시간 구간 열(`queue_wait_ms` 등)은 결과를 정한 작업(처음 완료된 QA 작업, 없으면 마지막 QA 작업) 기준입니다. `input_tokens`/`output_tokens`는 사용량을 보고한 호출만 더하고, 보고한 호출이 없으면 빈 칸(0이 아님)이며 `usage_unknown_calls`로 빠진 수를 셉니다. 여러 값은 `|`로 잇습니다(`models`는 실제 응답 모델, `configured_models`는 설정 모델). `=`·`+`·`-`·`@`로 시작하는 문자열 셀은 스프레드시트 수식으로 실행되지 않게 앞에 `'`를 붙입니다.
- 계정·비밀번호·세션·토큰, 모델 요청·응답 본문, 엔진 job id, 저장 경로는 넣지 않습니다. 보고서와 증거는 `report_url`·`jobs[].result_url`·`jobs[].artifacts_url` 링크로만 연결하며, 받으려면 같은 권한 검사를 다시 거칩니다.
