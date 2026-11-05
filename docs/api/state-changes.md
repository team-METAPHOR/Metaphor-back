# 승인된 테스트 쓰기 (`state_changes`)

엔진(core_engine 0.6.0, `2c21df2`)은 기본적으로 읽기 전용입니다. 장바구니 담기·수량 변경처럼
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
스냅샷에 값이 없으면(이 기능 이전 작업 포함) 쓰기는 계속 `METHOD_NOT_ALLOWED`로 막힙니다.
