# 테스트 계정 API

로그인이 필요한 화면을 검증하려고 프로그램마다 테스트 계정(로그인 아이디 + 비밀번호)을 하나 저장합니다.
쿠키 세션 API이므로 쓰기 요청에는 허용된 `Origin`이 필요합니다(CSRF).

| 메서드 | 경로 | 권한 |
|---|---|---|
| `GET` | `/api/v1/programs/{program_id}/test-account` | 회사 멤버 전원 |
| `PUT` | `/api/v1/programs/{program_id}/test-account` | `admin` |
| `DELETE` | `/api/v1/programs/{program_id}/test-account` | `admin` |

다른 회사 프로그램이나 없는 프로그램은 404, `developer`·`viewer`의 쓰기는 403입니다.

## 저장·교체 `PUT`

```json
{"login_id": "qa-tester@metaphor.kr", "password": "…", "login_path": "/login"}
```

- `login_id` 1~254자, `password` 1~256자. 정의되지 않은 필드는 거부합니다.
- `login_path`(선택): `/`로 시작하는 경로. 쿼리(`?`)·프래그먼트(`#`)·`//`·백슬래시·공백은 허용하지 않습니다.
  현재 승인 범위의 `allowed_path_prefixes` 안에 있고 `excluded_path_prefixes`에 걸리지 않아야 합니다.
  범위를 벗어나면 422 `LOGIN_PATH_OUT_OF_SCOPE`(`detail.reason`: `PATH_OUT_OF_SCOPE`, `PATH_EXCLUDED` 등)입니다.
- 형식 오류는 422 `VALIDATION_FAILED`이며, 입력값(비밀번호 포함)은 응답에 절대 다시 담지 않습니다.
- 서버에 `TEST_ACCOUNT_PUBLIC_KEY`가 없거나 잘못되면 503 `TEST_ACCOUNT_UNAVAILABLE`입니다.
- 저장·삭제를 합쳐 프로그램당 시간당 10회까지 가능하며, 넘으면 429 `TEST_ACCOUNT_RATE_LIMITED`입니다.
- 다시 저장하면 기존 계정을 바꾸고 내부 버전이 1 오릅니다. 감사 로그(`program.test_account_save`)에는 버전과 키 id만 남깁니다.

## 상태 조회 `GET` (PUT 응답도 같은 형태)

```json
{"configured": true, "in_use": false, "login_id_masked": "qa-***@metaphor.kr",
 "login_path": "/login", "updated_at": "2026-11-05T07:31:50Z"}
```

비밀번호와 전체 로그인 아이디는 어떤 응답에도 포함하지 않습니다. 저장된 계정이 없으면 `configured: false`이고 나머지는 `null`입니다.

- `configured`: 계정이 저장되어 있음.
- `in_use`: 검증 실행에 실제로 쓰이고 있음. 현재 엔진(core_engine 4a710f0)은 아이디·비밀번호 로그인을 지원하지 않아 **항상 `false`** 입니다.
  화면에서는 "저장됨(아직 실행에 적용 전)"으로 보여 주세요.

프로그램 응답의 `scope.has_test_account`는 계정 저장 여부이고, `scope.has_session`은 운영자가 연결한 세션이 있거나 테스트 계정이 저장되어 있으면 `true`입니다.

## 삭제 `DELETE`

204를 반환합니다. 저장된 계정이 없어도 204입니다(감사 로그는 실제로 지웠을 때만 `program.test_account_delete`).

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
