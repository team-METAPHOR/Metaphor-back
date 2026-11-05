# 테스트 조건 비교 API (조건 등록·조건별 실행·조건별 결과)

계정 역할·브라우저·화면 크기·응답 지연을 바꿔 가며 같은 제보를 재현해 보는 기능입니다.
엔진 `ConditionsService`(core_engine 0.8.0, `4c6b024` 고정)를 씁니다.

## 조건 등록 (`/programs/{program_id}/test-conditions`)

| 메서드 | 경로 | 권한 |
|---|---|---|
| GET | `/programs/{program_id}/test-conditions` | 회사 멤버 전원(viewer 포함) |
| POST | `/programs/{program_id}/test-conditions` | admin |
| PATCH | `/programs/{program_id}/test-conditions/{condition_id}` | admin |
| DELETE | `/programs/{program_id}/test-conditions/{condition_id}` | admin (204) |

다른 회사 프로그램은 404, 같은 회사의 admin이 아니면 쓰기 요청이 403입니다.

요청 본문(POST, PATCH는 일부 필드만 보내면 됩니다):

```json
{
  "condition_id": "staff-webkit",
  "label": "일반 직원 · Safari",
  "browser_name": "webkit",
  "width": 390,
  "height": 844,
  "response_delay_ms": 800,
  "test_context_id": "staff-1",
  "record_video": false,
  "reset_adapter": "stateless"
}
```

- `condition_id`: `[A-Za-z0-9_-]{1,64}`. 바꿀 수 없으니 바꾸려면 지우고 다시 등록합니다.
- `label`: 1~200자 공개 문구. 이메일·전화번호 같은 개인정보가 들어 있으면 422입니다.
- `browser_name`: `chromium` / `firefox` / `webkit`.
- 화면 크기: 320x320 ~ 1920x1080. `response_delay_ms`: 0~2000(응답 전달 지연).
- `reset_adapter`: 워커에 등록된 이름만 받습니다(`stateless`, `buglab-v1`). 프로그램에 쓰기 승인(`state_changes`)이 있으면 모든 조건이 같은 상태 초기화 어댑터여야 하고 운영자 세션을 쓸 수 없습니다. 아니면 작업이 엔진 실행 전에 `STATE_RESET_REQUIRED`/`STATE_CHANGES_SESSION_CONFLICT`로 실패합니다([승인된 테스트 쓰기](./state-changes.md)).
- 프로그램 하나에 조건은 최대 16개입니다. 넘으면 409 `CONDITION_LIMIT_EXCEEDED`, 같은 ID가
  이미 있으면 409 `CONDITION_EXISTS`, 엔진 규칙에 어긋나면 422 `INVALID_CONDITION`입니다.
- 모르는 필드는 거절합니다(예: `session_secret_ref`, 환경변수, 파일 경로).

### 세션(로그인 계정)

조건별 세션은 운영자가 따로 연결합니다. 고객 API로는 설정할 수 없고 응답에도
`has_session`(true/false)만 나옵니다. 워커는 `QA_SESSION_<ORG_ID>_<REF>` 환경변수에서
헤더를 읽습니다. `test_context_id`를 바꾸면 그 조건의 세션 연결은 풀립니다(다른 계정이므로).
프로그램 `base_url`이 바뀌면 모든 조건의 세션 연결이 풀립니다.
세션은 HTTPS 원점에서만 씁니다(엔진 0.4.1). 세션이 연결된 조건이 있으면 `base_url`을 HTTP로 바꿀 수 없고,
HTTP 원점에서 세션이 연결된 조건을 수정하면 422 `CREDENTIALS_REQUIRE_HTTPS`입니다([테스트 계정 API](test-account.md#https-필수-엔진-041) 참고).

## 조건 비교 실행 (`POST /reports/{report_id}/validation-jobs`)

`mode: "fresh"`에 아래 필드를 더합니다(developer, admin).

```json
{"mode": "fresh", "condition_ids": ["admin", "staff"]}
{"mode": "fresh", "auto_select_conditions": true, "max_conditions": 4}
```

- `condition_ids`만 주면 그 순서대로 모두 실행합니다(최대 8개).
- `auto_select_conditions: true`면 AI가 등록된 조건(또는 `condition_ids`로 준 후보) 중에서
  최대 `max_conditions`개(기본 4, 최대 8)를 고릅니다. AI는 등록된 ID만 고를 수 있습니다.
- 등록되지 않은 ID(다른 프로그램·회사의 조건 포함)는 422 `CONDITION_UNKNOWN`, 등록 조건이
  없으면 422 `NO_TEST_CONDITIONS`입니다.
- 고른 조건 설정은 작업 생성 시점에 작업에 고정됩니다. 나중에 조건을 고치거나 지워도 지난
  실행과 그 결과는 바뀌지 않습니다.
- 조건을 여러 개 돌려도 백엔드 작업은 하나이고, 제보당 진행 중 작업 1개 규칙도 그대로입니다.
- 실행 중 취소·권한 철회·공용 호출/시간 한도는 남은 조건에도 적용됩니다. 그때까지 끝난
  조건의 증거는 보존되고, 실행하지 못한 조건은 `not_run`으로 남습니다(성공으로 세지 않음).

작업 응답(`JobOut`)의 `has_conditions`가 true면 조건 비교 작업입니다. 작업 전체 `verdict`는
한 조건이라도 재현되면 `reproduced`, 실행한 모든 조건에서 재현되지 않으면 `not_reproduced`,
그 밖에는 `inconclusive`입니다.

### 조건 비교 작업 재검증

조건 비교 작업을 기준으로 재검증(`mode: "retest"`)하면 기준 작업에서 재현된 조건을 모두 다시 실행합니다. 조건마다 저장된 계획과 조건 설정을 그대로 써서 한 작업 안에서 차례로 돌립니다. 결과 행은 `GET /validation-jobs/{id}/conditions`로 확인합니다.

- 판정: 재현된 조건이 하나라도 있으면 `reproduced`, 필요한 조건이 모두 `not_reproduced`일 때만 `not_reproduced`, 나머지(불일치·미실행·취소·실패)는 `inconclusive`입니다.
- `fix_verified`는 필요한 조건이 모두 통과해야 참이 됩니다. 하나라도 통과하지 못하면 `closed` 변경은 409 `FIX_NOT_VERIFIED`입니다.
- 재현된 조건이 없는 작업, 조건 재검증 작업 자체는 기준으로 쓸 수 없습니다 → 409 `BASELINE_NOT_COMPARABLE`. 원래의 조건 비교 작업을 기준으로 지정하세요.

아직 지원하지 않는 것:

- 조건 비교 작업의 추가 질문 답변 → 409 `CONDITION_RESUME_UNSUPPORTED`. 새 검증을 요청하세요.
- 결론 없음(inconclusive) 뒤 자동 새 검증(auto fresh)은 조건 비교 작업에서는 돌지 않습니다.

## 조건별 결과 (`GET /validation-jobs/{job_id}/conditions`)

회사 멤버 전원(viewer 포함)이 읽을 수 있습니다. 다른 회사 작업은 404, 조건 비교 작업이 아니면
409 `NOT_CONDITION_JOB`입니다. 실행 중에도 부를 수 있습니다(시작된 조건까지 `running`으로 보임).

```json
{
  "items": [
    {
      "index": 1,
      "condition_id": "staff",
      "condition": {
        "label": "일반 직원", "browser_name": "chromium", "width": 1280, "height": 800,
        "response_delay_ms": 0, "test_context_id": "staff-1", "record_video": false,
        "has_session": true
      },
      "outcome": "reproduced",
      "status": "completed",
      "verdict": "reproduced",
      "error_code": null,
      "stop_point": "4/4단계 완료",
      "duration_ms": 21000
    }
  ],
  "attempted": 3,
  "reproduced": 1,
  "matched_condition_ids": ["staff"],
  "most_progressed_condition_ids": ["staff"],
  "progress_basis": "completed_steps_only_not_symptom_similarity",
  "not_selected_condition_ids": []
}
```

- `outcome`: `reproduced` / `not_reproduced` / `blocked`(오류·막힌 단계로 끝남) /
  `inconclusive` / `not_run`(실행하지 못함) / `running`(실행 중).
- `diagnosis`: 조건 실행(하위 작업)의 실행 진단. 개발자 전달의 `diagnosis`와 같은 형식이며
  `next_actions`, `state_changes`, `attempts`, `observed_facts`를 담습니다. 엔진이 조건 행에 복사하지 않는
  시도별 `blocked_requests`·`state_changing_requests`는 빈 목록이며, 하위 작업의 결과 파일(작업 근거 파일)에 있습니다.
  실행하지 않은 조건은 `null`입니다.
- `stop_point`: 멈춘 지점 요약(예: `1/3단계 후 중단 (SELECTOR_NOT_FOUND): click #pay`).
  일반 텍스트이며 URL은 경로만, 입력 값과 쿼리 문자열은 넣지 않습니다.
- `attempted`: 실제로 실행한 조건 수(`not_run` 제외). `reproduced`: 재현된 조건 수.
- `most_progressed_condition_ids`: **완료한 단계 수**가 가장 많은 조건입니다. 신고 증상과
  얼마나 비슷한지를 뜻하지 않습니다.
- `not_selected_condition_ids`: AI 자동 선택에서 고르지 않아 검사하지 않은 조건입니다.
