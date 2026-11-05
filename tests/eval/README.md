# 재현 정확도 평가 하네스 (`tests/eval`)

백엔드 전체 경로를 실제로 거쳐 재현 정확도를 잰다.
위젯 API(`POST /api/v1/widget/reports`) → 저장된 신고·엔진 설명(breadcrumbs 포함) → 큐 → 워커
`execute_job` → (자동 재생성 작업) → 최종 판정. 그리고 백엔드 설정(variant)끼리 비교한다.

## 엔진 평가(`Core_engine` `core_engine.evaluation`)와 다른 점

엔진 팀의 `qa-small-v1`은 `QAService`만 직접 호출해서 엔진·프롬프트 성능을 잰다. 독립 채점기와
reference plan도 쓴다. 이 하네스는 엔진만의 평가를 다시 하지 않는다. 고정 엔진(2ccf627)을 그대로
두고, **백엔드가 엔진에 무엇을 넘기는지**에 따라 결과가 어떻게 바뀌는지를 본다.

- 입력은 SDK와 같은 위젯 payload다. 마스킹·`redact`·엔진 설명 조립·동의/설정 검사를 모두 거친다.
- variant는 백엔드 설정이다(breadcrumbs 수신, inconclusive 뒤 자동 fresh).
- 판정은 엔진 verdict를 그대로 쓴다. 독립 채점기는 없다. 증상만 맞히고 절차가 틀린 경우를
  가려내려면 엔진 평가를 본다.
- `core_engine.evaluation`은 고정한 wheel에 없어서 import하지 않는다. 대신 관례만 따랐다.
  버그/정상 쌍은 같은 신고 텍스트를 쓰고(정상 = 수정된 버전), URL 경로는 무작위로 만들어 레이블
  힌트를 없앴다. 모델에는 케이스 id·정답을 보내지 않는다. 고정 모델 결과에는
  "고정 모델 연결 검증 — AI 성능 점수 아님" 표시를 붙이고, 전체 모델 호출 상한을 두고, 결과는
  실행마다 따로 보관한다.

## 구성

| 파일 | 역할 |
|---|---|
| `bug_site.py` | stdlib `http.server` 합성 쇼핑몰. 페이지마다 버그 하나. 경로는 실행마다 무작위(`/<run>/<page>`), 버그/수정 전환은 서버 메모리에서만 |
| `probes.py` | 평가자 전용 Playwright 조작. 버그가 실제로 나타나는지 확인(`test_bug_site_bugs_are_real`) |
| `cases.json` | 32케이스 = 버그 8개 × {vague, detailed, vague+crumbs, vague+crumbs 수정 버전} |
| `harness.py` | `run_case`, `summarize`, `to_markdown`, `write_run`, variant 정의와 기능 확인 |
| `test_harness_offline.py` | 키 없이 CI에서 돈다(사이트 자체 검증, 지표 계산, ScriptedModel로 run_case) |
| `test_repro_eval.py` | `@pytest.mark.eval`. 실제 모델로 측정만 하고 assert는 없다 |

### 심은 버그

| page | 버그 | 엔진에서 보이는 방식 |
|---|---|---|
| search | 검색어 "노트북"만 0건 | `#count` 텍스트 |
| cart | 수량 2 이상이면 합계가 갱신되지 않음 | `#total` 텍스트 |
| like | 두 번째 클릭부터 무반응 | `#likes` 텍스트 |
| products | 신발 + 할인 필터 조합에서 API 500 | **엔진이 HTTP ≥400을 `HTTP_ERROR` blocker로 처리하므로 현재는 보류(inconclusive)가 정상 결과** |
| detail | 모달을 열 때 TypeError | **`pageerror`는 `PAGE_SCRIPT_ERROR` blocker라서 현재는 보류가 정상 결과** |
| checkout | 폭 ≤480px에서 쿠폰 배너가 결제 버튼을 가림 | `overlap` 검사. 엔진은 scope revision viewport(1280×800)로 돌고, 신고자 viewport는 설명 텍스트로만 전달된다. 그래서 현재는 재현되지 않는 것이 예상 결과다(엔진 요청 3번 근거) |
| login | 로그인 뒤 주문 내역 대신 홈으로 이동 | `#heading` 텍스트 |
| signup | 위자드 3단계에서 올바른 5자리 우편번호를 거부 | `#error` 텍스트 |

정상 케이스는 따로 정상 페이지를 두지 않는다. 같은 신고를 **수정된 버전**에 보낸다. 오탐률의 분모가
이 케이스들이다.

## variant와 기능 확인

| variant | 필요한 기능 | 상태 |
|---|---|---|
| `baseline` | 없음(`auto_fresh_retries` 컬럼이 있으면 0으로 둔다) | 항상 실행 |
| `breadcrumbs` | payload `breadcrumbs`, `consent.breadcrumbs`, 위젯 설정 `breadcrumbs_enabled` (Task 1·2) | 없으면 skip |
| `breadcrumbs+auto_fresh` | 위 기능 + `programs.auto_fresh_retries` (Task 4), 큐에 쌓인 fresh 작업을 따라 실행 | 없으면 skip |

`harness.missing_features()`가 Pydantic 스키마 필드와 ORM 컬럼을 직접 확인한다. 빠진 것이 있으면
`pytest.skip`에 빠진 항목 이름을 적고, `summary.md`의 "건너뛴 variant"에도 남긴다. 스택을 합치면
코드를 고치지 않아도 그대로 켜진다.

**스택을 합친 뒤 확인할 것:** 하네스는 `runner.execute_job`을 직접 부른 다음, 같은 신고에
`queued` 상태로 생긴 작업을 따라간다. Task 4 훅이 `run_job`(supervisor 진입점)에만 있고
`execute_job`에는 없으면 자동 fresh가 생기지 않는다. 그러면 `breadcrumbs+auto_fresh`가
`breadcrumbs`와 똑같은 결과를 낸다. 그 경우 훅을 `execute_job` 끝으로 옮기거나, `run_case`의 작업
루프에서 훅을 호출한다.

## 실행

```bash
# 오프라인(CI 기본 실행에 포함)
TEST_DATABASE_URL=... uv run pytest -q tests/eval

# 실제 모델(기본 addopts에서 제외. 키·모델이 없으면 skip)
OPENAI_API_KEY=... OPENAI_MODEL=... TEST_DATABASE_URL=... \
  uv run pytest -m eval tests/eval -q -s

# 일부 케이스만 적은 비용으로
EVAL_CASES=search-detailed,search-crumbs-fixed EVAL_MAX_MODEL_CALLS=20 ... uv run pytest -m eval tests/eval -s
```

| 환경 변수 | 기본값 | 의미 |
|---|---|---|
| `EVAL_MAX_MODEL_CALLS` | 400 | 실행 전체의 공급자 호출 상한. 케이스 시작 전에 검사하고, 넘으면 나머지는 `not_run`. 진행 중인 케이스는 엔진 한도(작업당 6회)까지 더 쓸 수 있다 |
| `EVAL_CASES` | 전체 | 쉼표로 구분한 케이스 id |

결과는 `var/eval/<UTC시각>-<git sha>/`(`summary.md`, `summary.json`, `results.json`)에 남는다.
`var/`는 gitignore 대상이고, 디렉터리 권한은 0700, 파일 권한은 0600이다. `not_run`이 하나라도
있으면 `valid_for_comparison=false`다.

### 비용 추정

작업 하나는 모델 호출 최대 6회다(intake, plan, repair ≤2, report). 대개 3회다.

- baseline·breadcrumbs: 32케이스 × 최대 6회 = 최대 192회씩
- breadcrumbs+auto_fresh: 작업이 최대 2개(`auto_fresh_retries=1`)라서 최대 384회
- 세 variant 전체를 돌리면 최대 약 770회. 대개 300회 안팎이다. 기본 상한 400은 대략적인 측정용이므로
  전체 비교를 하려면 `EVAL_MAX_MODEL_CALLS=800`으로 올린다.

## 지표

variant별 값과, 설명 종류(vague / detailed / vague+crumbs)별 값을 함께 낸다.

- **재현율:** expect=reproduced 중 `reproduced` 비율
- **오탐률:** expect=not_reproduced(수정 버전) 중 `reproduced` 비율
- 보류(`inconclusive`)·추가 질문(`needs_info`) 비율, 케이스당 평균 토큰·작업 수·모델 호출·시간

보류와 실패도 분모에 들어간다. 오탐률만 보면 전부 보류하는 쪽이 좋아 보이므로 재현율과 보류율을
함께 본다. 32케이스는 공개 개발용 세트라서, 이 숫자로 기본값을 정한 뒤에는 미공개 케이스로 다시
확인해야 한다.
