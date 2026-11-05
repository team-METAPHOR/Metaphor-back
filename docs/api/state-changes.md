# 승인된 테스트 쓰기 (`state_changes`)

엔진(core_engine 0.8.5, `9e8e5c2`)은 기본적으로 읽기 전용입니다. 장바구니 담기·수량 변경처럼
서버 상태를 바꾸는 요청(POST/PUT/PATCH/DELETE)은 **관리자가 프로그램 범위에 승인한 경로만**,
**실제 서버 초기화 어댑터가 연결된 경우에만** 보냅니다. 엔진 명세:
Core_engine `docs/browser-request-policy.md`.

## 설정

`POST /api/v1/programs`, `PATCH /api/v1/programs/{program_id}`의 `state_changes` 필드입니다.
두 엔드포인트 모두 `admin`만 쓸 수 있습니다(`developer`·`viewer`는 403, 다른 회사 프로그램은 404).

```json
{
  "reset_adapter": "buglab-v1",
  "state_changes": {
    "environment": "test",
    "reset_adapter_id": "buglab-v1",
    "allowed_requests": [
      ["POST", "/api/cart/items"],
      ["PATCH", "/api/cart/items"]
    ]
  }
}
```

- 기본값은 `null`(읽기 전용)입니다. `PATCH`에 `"state_changes": null`을 보내면 승인을 끕니다.
- 승인 내용은 범위 리비전의 일부입니다. 켜기·끄기·규칙 변경은 새 리비전을 만들고, 이전 리비전과 그 리비전으로 만든 작업은 바뀌지 않습니다.
  각 작업은 생성 시점의 승인을 스냅샷으로 고정합니다. 재검증(`retest`)은 기준 작업의 스냅샷을 그대로 쓰므로, 승인이 바뀐 뒤에는 엔진이 `BASELINE_ENVIRONMENT_MISMATCH`로 거절합니다. 새 설정으로 기준 재현을 다시 만드세요.
- 승인 출처는 이 관리자 설정 하나뿐입니다. 신고 본문·SDK payload·모델 출력에 같은 키가 있어도 읽지 않습니다.

### 검증 (모두 422)

| 코드 | 조건 |
|---|---|
| `VALIDATION_FAILED` | `environment`가 `test`/`staging`이 아님(production 불가), 규칙 0개 또는 32개 초과, 쓰기가 아닌 메서드, 정의되지 않은 필드 |
| `STATE_RESET_REQUIRED` | `reset_adapter_id`가 워커에 등록된 **상태 초기화** 어댑터가 아님(`stateless`·미등록 이름 포함) |
| `STATE_RESET_MISMATCH` | 프로그램의 `reset_adapter`와 `state_changes.reset_adapter_id`가 다름 |
| `STATE_CHANGES_SESSION_CONFLICT` | 운영자 세션(`session_secret_ref`)이 연결된 범위. 서버 초기화가 그 계정의 세션을 끊으므로, 쓰기는 저장된 테스트 계정 로그인(초기화 → 새 로그인)으로만 씁니다 |
| `INVALID_STATE_CHANGE_POLICY`, `INVALID_PATH_PREFIX` | 엔진 형식 검사 실패(예: 쓰기 전체 `/` 허용) |
| `STATE_CHANGE_OUT_OF_SCOPE` | 규칙 경로가 `allowed_path_prefixes` 밖이거나 `excluded_path_prefixes`와 겹침 |
| `STATE_CHANGE_PATH_RESERVED` | `/api/test`(서버 초기화 제어 API)와 겹치는 경로, 또는 `signup`/`sign-up`/`register` 경로 |

규칙은 메서드와 **경로 세그먼트 접두사**의 쌍입니다. `/api/cart/items`는 `/api/cart/items/2`를 포함하지만 `/api/cart/items-extra`는 포함하지 않습니다.
로그인 POST는 이 규칙이 아니라 테스트 계정의 `submit_path` 예외로 처리됩니다.
계정 생성·테스트 제어 경로는 `excluded_path_prefixes`에도 넣어 두세요(예: `["/api/test", "/api/auth/signup"]`). 제외 경로와 겹치는 규칙은 엔진이 거절합니다.

## 응답

프로그램 응답의 `scope.state_changes`에 엔진이 정규화한 값(규칙 정렬)을 그대로 돌려줍니다. 비밀 값은 없습니다.

```json
"scope": {
  "reset_adapter": "buglab-v1",
  "state_changes": {
    "environment": "test",
    "reset_adapter_id": "buglab-v1",
    "allowed_requests": [["PATCH", "/api/cart/items"], ["POST", "/api/cart/items"]]
  }
}
```

## 워커

워커는 작업 스냅샷의 `state_changes`로 `StateChangePolicy`를 만들어 `RunPolicy`에 넣습니다.
스냅샷에 값이 없으면(이 기능 이전 작업 포함) 쓰기는 계속 `METHOD_NOT_ALLOWED`로 막히고, 초기화 콜백도 기존처럼 일반 함수로 넘깁니다.

승인된 작업은 초기화 어댑터를 엔진 `StateReset(<reset_adapter_id>, callback)`으로 감싸 `run`/`resume`/`retest`에 넘깁니다(콜백 `(attempt)`).
조건 비교 작업은 `ConditionsService`에 `(condition_id, attempt)` 콜백을 같은 방식으로 넘기고, 엔진이 조건별로 묶습니다.
엔진은 탐색(0)과 독립 시도(1, 2), 재검증마다 **초기화 → 새 브라우저 컨텍스트 → 새 로그인** 순서로 실행하고, 초기화가 `True`를 돌려주지 않으면 `RESET_FAILED`로 멈추며 쓰기를 보내지 않습니다.

엔진 실행 전 워커 실패 코드(`backend_error_code`):

| 코드 | 원인 |
|---|---|
| `STATE_RESET_REQUIRED` | 스냅샷의 프로그램/조건 `reset_adapter`가 승인 어댑터와 다르거나 상태 초기화 어댑터가 아님 |
| `STATE_CHANGES_SESSION_CONFLICT` | 운영자 세션이 연결된 스냅샷(DB에서 직접 연결한 경우 포함) |
| `RESET_ACCOUNT_MISSING` | 계정별 초기화인데 저장된 테스트 계정(로그인 경로·성공 신호 포함)이 없음 |
| `RESET_TOKEN_MISSING` | 워커에 어댑터 토큰 환경변수가 없음 |
| `RESET_TARGET_NOT_APPROVED` | `base_url` 원점이 어댑터의 승인 HTTPS 원점 목록에 없음(토큰을 보내지 않음) |
| `RESET_ACCOUNT_NOT_APPROVED` | 테스트 계정이 어댑터의 승인 계정 목록에 없음(토큰을 보내지 않음) |

상태 초기화 어댑터(프로그램 또는 조건)를 쓰는 작업은 쓰기 승인 여부와 관계없이, 같은 초기화 대상 계정이면 프로그램·조직·테스트 문맥이 달라도 한 번에 하나만 실행합니다(어댑터·정규화된 원점·계정 해시 기준 advisory lock). 서로 다른 계정이면 병렬로 실행됩니다.

## `buglab-v1` 어댑터 (BugLab Shop dev)

`POST <base_url 원점>/api/test/reset`에 `X-Test-Token`과 `{"email": <테스트 계정 아이디(소문자)>}`를 보냅니다.
응답이 200이고 같은 이메일, 빈 장바구니(`cart.items == []`), 적용 쿠폰 없음(`cart.appliedCoupons == []`)일 때만 성공입니다. 그 외 상태 코드·형식·네트워크 오류는 모두 실패(`False`)입니다.
BugLab은 이 호출로 해당 계정의 장바구니·쿠폰·찜·세션·로그인 실패 기록을 지웁니다.

- 워커 전용 환경변수: `QA_RESET_BUGLAB_TOKEN`(토큰), `QA_RESET_BUGLAB_ORIGINS`(토큰을 보낼 수 있는 정확한 HTTPS 원점, 쉼표 구분), `QA_RESET_BUGLAB_ACCOUNTS`(초기화할 수 있는 테스트 계정 이메일 또는 `@도메인`, 쉼표 구분). API 프로세스에는 두지 않습니다.
- HTTPS 인증서 검증, 리다이렉트·프록시 미사용, 10초 제한. 토큰·계정·URL은 로그·결과·이벤트에 남기지 않고, 브라우저 계획·모델 입력에도 넣지 않습니다. 브라우저 쪽 `/api/test`는 범위에서 제외하고 쓰기 승인도 거절합니다.
- 고객 관리자가 어댑터 이름과 테스트 계정을 고를 수 있으므로, 토큰을 쓸 원점과 계정은 운영자 환경변수로만 정합니다. 목록 밖이면 엔진 실행 전에 실패하고 토큰을 보내지 않습니다. 원점은 소유 확인(`ORIGIN_NOT_VERIFIED`)도 통과해야 작업이 만들어집니다.

### B02 데모 설정 예

```json
{
  "excluded_path_prefixes": ["/api/test", "/api/auth/signup"],
  "reset_adapter": "buglab-v1",
  "state_changes": {
    "environment": "test",
    "reset_adapter_id": "buglab-v1",
    "allowed_requests": [["POST", "/api/cart/items"], ["PATCH", "/api/cart/items"]]
  }
}
```

쿠폰·찜 데모를 켤 때만 `["POST", "/api/cart/coupons"]`, `["POST", "/api/favorites"]`, `["DELETE", "/api/favorites"]`를 추가합니다.
테스트 계정은 `unauthenticated_paths: ["/api/auth/me"]`로 저장합니다([테스트 계정 API](./test-account.md)).
