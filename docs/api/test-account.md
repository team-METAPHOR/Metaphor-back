# 테스트 계정 API

로그인이 필요한 화면을 검증하려고 프로그램마다 테스트 계정(로그인 아이디 + 비밀번호)을 하나 저장합니다.
워커는 엔진(core_engine 0.4.1, 0f833d2)의 폼 로그인으로 탐색과 두 번의 재현 시도 직전에 매번 새로 로그인합니다.
쿠키 세션 API이므로 쓰기 요청에는 허용된 `Origin`이 필요합니다(CSRF).

| 메서드 | 경로 | 권한 |
|---|---|---|
| `GET` | `/api/v1/programs/{program_id}/test-account` | 회사 멤버 전원 |
| `PUT` | `/api/v1/programs/{program_id}/test-account` | `admin` |
| `DELETE` | `/api/v1/programs/{program_id}/test-account` | `admin` |

다른 회사 프로그램이나 없는 프로그램은 404, `developer`·`viewer`의 쓰기는 403입니다.

## HTTPS 필수 (엔진 0.4.1)

테스트 계정과 운영자 세션(`Authorization`/`Cookie` 헤더)은 유효한 인증서가 있는 HTTPS 원점에서만 씁니다.
엔진은 HTTP 대상이면 연결 전에 `CREDENTIALS_REQUIRE_HTTPS`로 거절하며, localhost·사설망 예외나 인증서 검증 해제 옵션은 없습니다. 인증 없는 HTTP 검증은 그대로 가능합니다.

- 프로그램 `base_url`이 HTTP이면 `PUT`은 422 `CREDENTIALS_REQUIRE_HTTPS`입니다.
- 계정·운영자 세션·조건 세션이 있는 프로그램의 `base_url`을 HTTP로 바꾸면 422 `CREDENTIALS_REQUIRE_HTTPS`입니다. 먼저 계정을 삭제하거나 HTTPS 주소를 쓰세요.
- 세션이 연결된 테스트 조건은 프로그램 원점이 HTTP이면 수정할 때 422 `CREDENTIALS_REQUIRE_HTTPS`입니다.
- 0.4.1 이전에 저장된 HTTP+인증 설정은 프로그램 응답의 `scope.credentials_blocked_by_http: true`로 표시합니다(비밀 값은 포함하지 않음). 이 상태의 실행은 워커가 엔진 호출 전에 `CREDENTIALS_REQUIRE_HTTPS`로 실패시키고, `required_actions`에 `USE_HTTPS_FOR_CREDENTIALS`(HTTPS 재등록 안내)가 붙습니다. HTTP로 자동 재시도하지 않습니다.

## 저장·교체 `PUT`

```json
{"login_id": "qa-tester@metaphor.kr", "password": "…", "login_path": "/login",
 "submit_path": "/api/auth", "success_path": "/app/home", "success_selector": null}
```

- `login_id` 1~254자, `password` 1~256자. 정의되지 않은 필드는 거부합니다.
- `login_path`(선택): `/`로 시작하는 경로. 쿼리(`?`)·프래그먼트(`#`)·`//`·백슬래시·공백은 허용하지 않습니다.
  현재 승인 범위의 `allowed_path_prefixes` 안에 있고 `excluded_path_prefixes`에 걸리지 않아야 합니다.
  범위를 벗어나면 422 `LOGIN_PATH_OUT_OF_SCOPE`(`detail.reason`: `PATH_OUT_OF_SCOPE`, `PATH_EXCLUDED` 등)입니다.
- `submit_path`(선택): 로그인 폼이 POST하는 경로. 생략하면 `login_path`로 제출합니다. 형식·범위 규칙은 `login_path`와 같습니다.
- `success_path`(선택): 로그인 성공 후 도착하는 경로. 형식·범위 규칙은 `login_path`와 같고, `login_path`와 같으면 422 `LOGIN_CONFIG_INVALID`입니다.
- `success_selector`(선택, 1~256자): 로그인 성공 후에만 보이는 CSS 선택자(예: `#signed-in`). 로그인 화면에 이미 보이는 요소면 엔진이 실패로 처리합니다.
- 엔진은 클릭만으로 로그인 성공을 추정하지 않습니다. `success_path`나 `success_selector` 중 하나 이상이 있어야 실행에 쓰입니다(둘 다 있으면 둘 다 확인).
- 아이디·비밀번호 입력란과 제출 버튼은 엔진 기본 선택자(`input[autocomplete="username"]`·`input[type="email"]`·`input[name="username"]`, `input[type="password"]`, `button[type="submit"]` 등)로 찾습니다. MFA·CAPTCHA·외부 SSO·다단계 로그인은 지원하지 않습니다.
- 형식 오류는 422 `VALIDATION_FAILED`이며, 입력값(비밀번호 포함)은 응답에 절대 다시 담지 않습니다.
- 서버에 `TEST_ACCOUNT_PUBLIC_KEY`가 없거나 잘못되면 503 `TEST_ACCOUNT_UNAVAILABLE`입니다.
- 저장·삭제를 합쳐 프로그램당 시간당 10회까지 가능하며, 넘으면 429 `TEST_ACCOUNT_RATE_LIMITED`입니다.
- 다시 저장하면 기존 계정을 바꾸고 내부 버전이 1 오릅니다. 감사 로그(`program.test_account_save`)에는 버전과 키 id만 남깁니다.

## 상태 조회 `GET` (PUT 응답도 같은 형태)

```json
{"configured": true, "in_use": true, "login_id_masked": "qa-***@metaphor.kr",
 "login_path": "/login", "submit_path": "/api/auth", "success_path": "/app/home",
 "success_selector": null, "updated_at": "2026-11-05T07:31:50Z"}
```

비밀번호와 전체 로그인 아이디는 어떤 응답에도 포함하지 않습니다. 저장된 계정이 없으면 `configured: false`이고 나머지는 `null`입니다.

- `configured`: 계정이 저장되어 있음.
- `in_use`: 현재 프로그램 설정으로 검증 실행이 이 계정으로 로그인함. 다음을 모두 만족하면 `true`입니다.
  - `login_path`와 성공 신호(`success_path` 또는 `success_selector`)가 저장되어 있음
  - 현재 승인 범위의 `test_context_id`가 `anonymous`가 아님(엔진은 계정·역할·데이터 조건을 식별하는 명시적 컨텍스트를 요구합니다)
  - 운영자가 연결한 세션(`has_session`의 운영자 세션)이 없음. 엔진은 고정 세션 헤더와 폼 로그인을 함께 쓰지 않으므로 운영자 세션이 우선합니다
  `false`이면 화면에서 "저장됨(실행에 적용 안 됨)"으로 보여 주고, 위 조건 중 빠진 것을 안내해 주세요. 조건 매트릭스 작업은 조건마다 같은 규칙(조건의 `test_context_id`)을 따릅니다.

프로그램 응답의 `scope.has_test_account`는 계정 저장 여부, `scope.credentials_blocked_by_http`는 HTTP 원점에 저장된 계정·운영자 세션이 있어 실행이 막힌 상태이고, `scope.has_session`은 운영자가 연결한 세션이 있거나 테스트 계정이 저장되어 있으면 `true`입니다.

## 삭제 `DELETE`

204를 반환합니다. 저장된 계정이 없어도 204입니다(감사 로그는 실제로 지웠을 때만 `program.test_account_delete`).

## 실행 중 동작

- 워커는 실행 시점의 현재 계정을 복호화해 메모리에서만 엔진에 넘깁니다. 비밀번호와 로그인 아이디는 모델 입력·계획·결과(`qa_results`)·이벤트·근거 파일·로그 어디에도 남지 않습니다. 엔진은 로그인 화면의 스크린샷·영상·콘솔·네트워크 기록을 수집하지 않고, 이후 화면에 계정 문자열이 보이면 마스킹합니다.
- 결과의 `environment.session_mode`는 `form_login`, `login_profile_sha256`는 비밀을 뺀 경로·선택자 설정의 해시입니다. 로그인 설정이나 `test_context_id`를 바꾸면 기존 결과의 재검증(`retest`)은 `BASELINE_ENVIRONMENT_MISMATCH`로 보류되니 새 검증을 요청하세요. 비밀번호만 바꾸면 재검증에 영향이 없습니다.
- 실패 코드
  - `LOGIN_FAILED`(엔진): 계정 오류, 폼 불일치, 성공 신호 미확인. 판정은 `inconclusive`이며 `required_actions`에 `CHECK_TEST_ACCOUNT`가 붙습니다.
  - `TEST_ACCOUNT_INVALID`(워커): 저장된 계정을 열 수 없음(키 교체 등). 다시 저장해야 하며 `required_actions`는 `TEST_ACCOUNT_REQUIRED`입니다.
  - `TEST_ACCOUNT_KEY_MISSING`·`TEST_ACCOUNT_KEY_INVALID`(워커): 워커에 개인키가 없거나 잘못됨. 운영자 조치가 필요하며 `required_actions`는 `TEST_ACCOUNT_REQUIRED`입니다.
  - `CREDENTIALS_REQUIRE_HTTPS`(워커 사전 확인·엔진): HTTP 원점에 계정·세션 사용. `required_actions`는 `USE_HTTPS_FOR_CREDENTIALS`입니다.
  - 로그인 경로가 범위를 벗어나면(저장 후 범위가 바뀐 경우) `PATH_OUT_OF_SCOPE` 등 → `CHECK_TARGET_URL`.

## 보관 방식

계정은 승인 범위 리비전이 아니라 프로그램 단위로 저장합니다. 비밀번호를 바꿔도 새 리비전이 생기지 않고, 워커는 실행 시점의 현재 계정을 읽습니다.

- API는 공개키로 암호화만 할 수 있고, 복호화용 개인키는 워커 환경(`TEST_ACCOUNT_PRIVATE_KEY`)에만 있습니다.
- 방식: 요청마다 새 AES-256-GCM 데이터 키로 `{login_id, password}` JSON을 암호화하고, 데이터 키는 RSA-OAEP(SHA-256, RSA 3072비트 이상)로 감쌉니다.
- 연관 데이터 `metaphor:test-account:v1|<program_id>|<version>|<key_id>`를 GCM 태그와 OAEP 라벨 양쪽에 묶습니다. 행을 다른 프로그램으로 옮기거나 다른 버전·키로 바꾸면 복호화에 실패합니다.
- `key_id`는 공개키 DER(SubjectPublicKeyInfo)의 SHA-256입니다. 키를 교체하면 기존 계정은 워커에서 `TEST_ACCOUNT_INVALID`가 되므로 다시 저장해야 합니다.

키 생성:

```bash
uv run python -m app.worker.keys ./keys   # test_account_private.pem(0600), test_account_public.pem
```
