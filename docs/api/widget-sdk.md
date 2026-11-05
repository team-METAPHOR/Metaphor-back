# Metaphor Web SDK 연동 API

고객사 웹사이트에 설치되는 Metaphor Web SDK(위젯)가 호출하는 공개 API 명세입니다.
백엔드 코드를 몰라도 SDK를 구현할 수 있도록 요청·응답·오류·보안 규칙을 모두 적었습니다.
기준 코드는 `app/api/v1/endpoints/widget.py`, `app/services/widget.py`, `app/schemas/widget.py`,
`app/core/widget_cors.py`, SDK 번들 서빙은 `app/api/sdk.py`입니다. 이 문서와 코드가 다르면 코드가 맞고, 이 문서를 고쳐야 합니다.

## 1. 개요

```text
고객사 페이지(SDK)                                   Metaphor API
──────────────────                                   ────────────
1. 페이지 로드
   GET /api/v1/widget/config?project_key=mpk_...  →  키·Origin 확인 → 위젯 설정
   ← 200 { reporting_enabled, screenshot_allowed, auto_detect, limits, mask_selectors }

2. 사용자가 "버그 신고" 제출 (또는 SDK가 오류 자동 감지)
   POST /api/v1/widget/reports?project_key=mpk_... → 검사 → 제보 저장 → 검증 작업 대기열 등록
   (multipart: payload JSON + 선택 screenshot)
   ← 201 { receipt_id, status: "received", created_at }

3. 백엔드 워커가 승인된 스테이징 환경에서 버그를 재현합니다.
   결과는 고객사 대시보드에서만 볼 수 있습니다. 익명 신고자는 결과를 조회할 수 없습니다.
```

- **인증이 없습니다.** 쿠키·세션·토큰을 쓰지 않습니다. 로그인 쿠키가 함께 와도 무시합니다.
- **결과는 신고자에게 공개되지 않습니다.** 응답의 `receipt_id`는 "접수됨"을 보여 주는 용도이고,
  이 값으로 조회할 수 있는 API는 없습니다.
- 모든 시각은 UTC ISO 8601, 모든 오류 응답은 아래 공통 형식입니다.

```json
{"error": {"code": "ORIGIN_NOT_ALLOWED", "message": "허용되지 않은 사이트입니다.", "request_id": "req_…"}}
```

`message`는 사람이 읽는 한국어 설명이며 바뀔 수 있습니다. **SDK는 `code`로 분기하세요.**
모든 응답에는 `X-Request-Id` 헤더가 있습니다. 문의할 때 이 값을 함께 전달하면 됩니다.

### SDK 설치

SDK 번들은 API 서버가 직접 서빙하므로 고객사가 허용할 origin은 API origin 하나입니다.

| 경로 | 캐시 | 용도 |
|---|---|---|
| `GET /sdk/v1/metaphor-<version>.js` | `public, max-age=31536000, immutable` | 설치 코드용. 현재 버전만 있고 다른 버전은 `404` |
| `GET /sdk/v1/metaphor.js` | `public, max-age=300` | 항상 최신 버전. 내용이 바뀌므로 `integrity`를 붙이지 마세요 |

- 인증·쿠키·CSRF가 없고 `GET`/`HEAD`를 받습니다. `ETag`가 있어 `If-None-Match`면 `304`입니다.
- 응답 헤더: `Content-Type: text/javascript; charset=utf-8`, `Access-Control-Allow-Origin: *`,
  `Cross-Origin-Resource-Policy: cross-origin`, `X-Content-Type-Options: nosniff`.
- 번들 파일이 서버에 없으면 `503 SDK_UNAVAILABLE`입니다.

설치 코드는 `GET /api/v1/programs/{id}/widget-settings` 응답 값으로 만듭니다.

| 필드 | 예 | 설명 |
|---|---|---|
| `api_base` | `https://api.metaphor.example` | 서버 설정 `PUBLIC_API_BASE`. 끝에 `/` 없음 |
| `sdk_url` | `https://api.metaphor.example/sdk/v1/metaphor-0.1.0.js` | 버전 고정 URL |
| `sdk_integrity` | `sha384-…` | `sdk_url` 파일의 SRI 해시 |

`PUBLIC_API_BASE`가 비어 있으면 세 값 모두 `null`입니다. 이때는 설치 코드를 보여 주지 말고
운영자에게 설정을 요청하세요(서버는 요청 주소로 추측하지 않습니다).

```html
<script src="{sdk_url}"
        integrity="{sdk_integrity}"
        crossorigin="anonymous"
        data-metaphor-project-key="{public_project_key}"
        data-metaphor-api-base="{api_base}" defer></script>
```

- `crossorigin="anonymous"`가 없으면 브라우저가 `integrity`를 검사하지 못해 스크립트를 막습니다.
- SDK 버전이 오르면 `sdk_url`과 `sdk_integrity`가 함께 바뀝니다. 고객사는 새 설치 코드로 교체합니다.
  교체 전까지는 이전 코드가 `404`가 되므로 버전을 올릴 때 고객사에 안내해야 합니다.

고객사 사이트에 CSP가 있으면 아래처럼 API origin을 허용합니다.

```text
Content-Security-Policy: script-src 'self' https://api.metaphor.example;
                         connect-src 'self' https://api.metaphor.example;
                         img-src 'self' data:
```

- `script-src`: SDK 번들 로드. `connect-src`: `/api/v1/widget/*` 호출.
- `img-src data:`: SDK가 화면을 캡처할 때 `data:` URL 이미지를 씁니다. 없으면 스크린샷 없이 텍스트 신고만 됩니다.

## 2. 프로젝트 키와 Origin

### 프로젝트 키 (`project_key`)

- 형식: `mpk_` + 무작위 문자열. 고객사 관리자가 Metaphor 플랫폼에서 위젯을 켤 때 발급됩니다.
- **비밀 값이 아닙니다.** 페이지 소스에 그대로 넣어도 됩니다. 어느 프로그램의 위젯인지 가리키는
  식별자일 뿐이고, 실제 보호는 Origin 검사·신고 한도·테스트 범위 검사가 맡습니다.
- 관리자가 키를 **회전**하면 이전 키는 즉시 쓸 수 없게 됩니다(유예 기간 없음). 이전 키로 요청하면
  `404 WIDGET_NOT_FOUND`가 옵니다. SDK 설정의 키를 새 값으로 바꿔 배포해야 합니다.
- 키는 두 엔드포인트 모두 **쿼리 파라미터**로 보냅니다(CORS preflight도 URL만 보고 판단해야
  하므로 헤더가 아니라 쿼리입니다).

### Origin 등록

- 관리자가 위젯 설정의 `allowed_origins`(최대 20개)에 SDK가 설치될 사이트의 원점을 등록해야 합니다.
- 원점 형식: `scheme://host[:port]`. 경로·쿼리·fragment 없음. 소문자, 기본 포트(443/80)는 생략.
  `https`만 허용하며, 개발용으로 `http://localhost`, `http://127.0.0.1`(포트 포함 가능)만 예외입니다.
- 브라우저가 보내는 `Origin` 헤더가 등록된 값과 **정확히 같아야** 합니다. 다르거나 `Origin` 헤더가
  없으면 `403 ORIGIN_NOT_ALLOWED`입니다. `www` 유무, 포트 차이도 다른 원점입니다.
- Origin 검사는 브라우저 쪽 보호 장치이지 인증이 아닙니다. 서버 간 호출로 Origin을 위조할 수
  있지만, 그런 요청도 같은 신고 한도와 범위 검사를 받습니다.

### 승인 원점(approved)과 운영 원점(mapped)

Metaphor는 버그를 항상 고객사가 승인한 **테스트 환경**(프로그램의 `base_url`, 예: 스테이징)에서
재현합니다. SDK는 그 테스트 환경에도, 실제 운영 사이트에도 설치할 수 있습니다.

| 원점 종류 | 조건 | 처리 |
|---|---|---|
| `approved` | 요청 Origin = 프로그램 `base_url`의 원점 | `page_url`을 그대로 재현 시작 주소로 사용 |
| `mapped` | 그 밖에 `allowed_origins`에 등록된 원점(운영 사이트 등) | `page_url`의 **원점만** `base_url`로 바꾸고 경로·쿼리는 유지해 재현 |

- 예: `base_url = https://staging.example.com`, 운영 사이트 `https://www.example.com`에서
  `page_url = https://www.example.com/search?q=cat` → 재현 주소 `https://staging.example.com/search?q=cat`.
- 바뀐 주소의 경로가 승인된 테스트 범위 밖이면 `422 SCOPE_VIOLATION`입니다.
- 운영 사이트에는 실제 사용자 데이터가 있으므로 재현 결과가 `not_reproduced`나 `needs_info`로
  나올 수 있습니다. 이는 정상입니다.

### 스크린샷 허용 여부

| 원점 | 스크린샷 |
|---|---|
| `approved` | 항상 허용 |
| `mapped` | 관리자가 `live_screenshot_enabled`를 켠 경우에만 허용. 꺼져 있으면 `403 LIVE_SCREENSHOT_DISABLED` |

`GET /config` 응답의 `screenshot_allowed`가 **현재 페이지 Origin 기준**으로 이 값을 알려 줍니다.
`false`이면 SDK는 스크린샷 첨부 UI를 숨기고, 스크린샷을 보내지 마세요.

## 3. `GET /api/v1/widget/config`

페이지 로드 시 한 번 호출해 위젯을 어떻게 보여 줄지 결정합니다.

**요청**

| 위치 | 이름 | 필수 | 설명 |
|---|---|---|---|
| query | `project_key` | 예 | 1–64자 |
| header | `Origin` | 예 | 브라우저가 자동으로 붙임 |

**응답 200**

```json
{
  "widget_enabled": true,
  "reporting_enabled": true,
  "screenshot_allowed": true,
  "auto_detect": {"enabled": true, "js_error": true, "unhandled_rejection": true, "http_5xx": true},
  "limits": {"max_screenshot_bytes": 4194304, "max_description": 8000, "max_mask_boxes": 20},
  "mask_selectors": [
    "[data-private]", "[data-sensitive]", "input[type=password]", "input[type=hidden]", "iframe",
    "#card-number"
  ]
}
```

| 필드 | 의미 | SDK 동작 |
|---|---|---|
| `widget_enabled` | 관리자가 위젯을 켰는지 | `false`면 위젯을 표시하지 않음 |
| `reporting_enabled` | `widget_enabled && public_reporting_enabled` && 승인 원점 소유 확인 완료 | `false`면 신고 버튼을 숨김 |
| `screenshot_allowed` | 이 Origin에서 스크린샷을 받을지 | `false`면 스크린샷 UI 숨김 |
| `auto_detect.*` | 자동 오류 감지 허용 여부(현재 네 값은 항상 같음) | `false`면 오류 자동 신고를 하지 않음 |
| `limits` | 스크린샷 바이트, 설명 글자 수, 마스킹 박스 개수 상한 | 제출 전에 클라이언트에서 미리 검사 |
| `mask_selectors` | 스크린샷에서 가려야 할 요소의 CSS 선택자 목록 | 캡처 전에 일치하는 요소를 검게 칠함([스크린샷 마스킹](#스크린샷-마스킹)) |

### 스크린샷 마스킹

`mask_selectors`는 Metaphor 기본 민감 선택자(`[data-private]`, `[data-sensitive]`,
`input[type=password]`, `input[type=hidden]`, `iframe`) 뒤에 관리자가 테스트 범위에 등록한
선택자를 이어 붙인 목록입니다. 중복은 빠지고 순서는 유지됩니다. 검증 워커가 브라우저에서 찍는
스크린샷도 같은 선택자로 가려지므로, 운영 사이트 화면도 같은 기준으로 맞춥니다.

SDK가 페이지 화면을 직접 캡처할 때(특히 운영 원점에서 `live_screenshot_enabled`가 켜진 경우)는
반드시 아래를 지키세요.

- 캡처하기 **전에** 클라이언트에서 `mask_selectors`와 일치하는 모든 요소의 영역을 검은색으로 칠합니다.
- 모든 `input`·`textarea`의 입력 값도 가립니다(값을 지우거나 영역을 칠함). 선택자에 없는 칸도 포함합니다.
- 서버로는 **마스킹된 이미지만** 업로드합니다. 원본 캡처는 메모리에서 버리고, 저장하거나 전송하지 마세요.
- 사용자가 그린 `mask_boxes`는 이 자동 마스킹에 **추가로** 적용됩니다. 대신하지 않습니다.
- 선택자가 잘못되어 `querySelectorAll`이 예외를 던지면 그 선택자만 건너뛰지 말고 캡처를 중단하세요.

- 응답에는 프로그램 id·이름·테스트 범위·조직 정보가 들어 있지 않습니다.
- `Cache-Control: public, max-age=60`, `Vary: Origin`. 설정 변경은 최대 60초 뒤에 반영될 수 있습니다.
- 오류: `404 WIDGET_NOT_FOUND`, `403 ORIGIN_NOT_ALLOWED`, `422 VALIDATION_FAILED`(키 누락·길이 초과).

## 4. `POST /api/v1/widget/reports`

**요청**

- `Content-Type: multipart/form-data` (브라우저 `FormData`를 그대로 보내면 됩니다)
- `Content-Length` 필수, 전체 요청 **5 MiB 이하**. 넘으면 본문을 읽기 전에 거절합니다.

| 위치 | 이름 | 필수 | 설명 |
|---|---|---|---|
| query | `project_key` | 예 | 1–64자 |
| header | `Origin` | 예 | 브라우저가 자동으로 붙임 |
| header | `Idempotency-Key` | 아니오(권장) | 1–128자 출력 가능한 문자. 6절 참고 |
| form | `payload` | 예 | 아래 스키마의 JSON 문자열, UTF-8 기준 **32 KiB 이하** |
| form | `screenshot` | 아니오 | 이미지 파일 1개. PNG·JPEG·WebP 정지 이미지, **4 MiB 이하**, 각 변 4096px 이하, 전체 800만 화소 이하. 서버가 PNG로 정규화한 결과도 4 MiB 이하여야 함(넘으면 422 `INVALID_SCREENSHOT`) |

### `payload` JSON 스키마

알 수 없는 필드가 하나라도 있으면(중첩 객체 포함) 요청 전체가 `422 VALIDATION_FAILED`입니다.
서버는 거절한 값을 응답에 되돌려 주지 않습니다.

```jsonc
{
  "title": "검색 결과가 비어 있음",                     // 선택, 기본 "", ≤ 200자
  "description": "cat 검색 시 결과가 안 나와요",        // 선택, 기본 "", ≤ 8000자
  "page_url": "https://staging.example.com/search?q=cat", // 필수, ≤ 2048자. 보통 location.href
  "error_type": "manual",                               // "manual"(기본) | "js_error" | "unhandled_rejection" | "http_5xx"
  "error": {                                            // error_type ≠ "manual"일 때 필수, "manual"이면 넣지 않음
    "message": "TypeError: x is undefined",             // 필수, ≤ 1000자
    "stack_top": "https://staging.example.com/app.js:120:15", // 선택, ≤ 300자, "<위치>:<줄>:<열>" 형식
    "http": {                                           // error_type = "http_5xx"일 때만, 그때는 필수
      "method": "GET",                                  // GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS
      "path": "/api/search",                            // ≤ 2048자. 쿼리는 서버가 제거
      "status": 502                                     // 500–599
    }
  },
  "environment": {                                      // 선택. 보내려면 consent.environment = true
    "user_agent": "Mozilla/5.0 …",                      // 선택, ≤ 512자
    "language": "ko-KR",                                // 선택, ≤ 35자
    "timezone": "Asia/Seoul",                           // 선택, ≤ 64자
    "viewport": {"width": 1280, "height": 800}          // 선택, 각 1–10000 정수
  },
  "mask_boxes": [[0, 0, 200, 40]],                      // 선택, 최대 20개. screenshot과 함께만
  "consent": {"environment": true, "screenshot": true}  // 선택, 두 값 모두 기본 false
}
```

**필드 규칙**

- 스크린샷이 없으면 `description`이 비어 있으면 안 됩니다(공백만 있어도 비어 있는 것으로 봄)
  → `422 SCREENSHOT_OR_DESCRIPTION_REQUIRED`. 자동 감지 신고는 서버가 오류 요약을 설명에 붙이므로
  `description`을 비워도 됩니다.
- `page_url`
  - 원점(scheme·host·port)이 요청 `Origin`과 같아야 합니다. 다르면 `422 PAGE_URL_ORIGIN_MISMATCH`.
  - fragment(`#…`)는 서버가 버립니다.
  - 쿼리 키에 `token`, `secret`, `pass`, `auth`, `session`, `cookie`, `action`, `delete`,
    `redirect`, `callback`, `url`이 들어 있으면 서버가 **쿼리 전체를 지우고** 접수합니다(오류 아님).
    그래도 SDK는 가능하면 민감한 쿼리를 빼고 보내세요.
  - 경로가 승인된 테스트 범위 밖이거나 로그아웃·삭제·결제 같은 상태 변경 경로
    (`logout`, `signout`, `delete`, `remove`, `purchase`, `checkout`, `unsubscribe`)이면
    `422 SCOPE_VIOLATION`.
- 모든 문자열은 서버에서 토큰·키·비밀번호·이메일·URL 쿼리를 가린 뒤 저장합니다. 그렇다고 SDK가
  민감 정보를 보내도 된다는 뜻은 아닙니다(8절).

### `error_type` 규칙

| `error_type` | `error` | `error.http` | 비고 |
|---|---|---|---|
| `manual` | 넣지 않음 | – | 사용자가 직접 쓴 신고 |
| `js_error` | 필수 | 넣지 않음 | `window.onerror` |
| `unhandled_rejection` | 필수 | 넣지 않음 | `unhandledrejection` 이벤트 |
| `http_5xx` | 필수 | 필수, `status` 500–599 | fetch/XHR 응답이 5xx일 때 |

어기면 `422 ERROR_DETAILS_INVALID`입니다. `manual`이 아닌 신고는 관리자가 자동 감지를 켜 두어야
받습니다(`403 AUTO_DETECT_DISABLED`). `config.auto_detect.enabled`를 먼저 확인하세요.

**중복 병합 (자동 감지 신고만):** 같은 프로그램에서 24시간 안에 같은 오류가 다시 오면 새 제보를
만들지 않고 원래 제보의 중복 횟수만 올립니다. 같은 오류인지는 오류 유형, 숫자를 뺀 메시지,
`stack_top`, 페이지 경로, HTTP 메서드·경로·상태로 판단합니다. 수동 신고는 병합하지 않습니다.

### 동의(consent) 규칙

| 보내는 것 | 필요한 동의 | 없으면 |
|---|---|---|
| `screenshot` 파일 | `consent.screenshot = true` | `422 CONSENT_REQUIRED` |
| `environment` 객체 | `consent.environment = true` | `422 CONSENT_REQUIRED` |

SDK는 사용자가 화면에서 직접 체크한 경우에만 동의 값을 `true`로 보내야 합니다.

### `mask_boxes` 좌표

- 각 박스는 `[x1, y1, x2, y2]` 정수 4개. `0 ≤ x1 < x2 ≤ 이미지 너비`, `0 ≤ y1 < y2 ≤ 이미지 높이`.
- 좌표 단위는 **업로드하는 이미지 파일의 실제 픽셀**입니다. 브라우저에서는
  `img.naturalWidth` / `img.naturalHeight` 기준이며, 화면에 축소해 보여 준 크기(CSS 픽셀)가 아닙니다.
  미리보기를 축소했다면 `natural / 표시 크기` 비율을 곱해 변환하세요.
- 서버가 박스 영역을 검은색으로 칠한 **마스킹본만** 저장하고 AI 분석에 사용합니다. 원본은 저장하지 않습니다.
- 스크린샷 없이 박스만 보내거나, 박스 값이 4개가 아니거나, 범위를 벗어나면 `422 MASK_BOX_INVALID`.
  21개 이상이거나 좌표가 정수가 아니면 스키마 위반 `422 VALIDATION_FAILED`.

### 응답

**201 접수됨**

```json
{"receipt_id": "rcpt_3kZ…", "status": "received", "created_at": "2026-11-05T09:12:03.120000+00:00"}
```

제보 id, 작업 id, 결과 링크는 주지 않습니다. 사용자에게 "접수되었습니다"와 `receipt_id`를 보여 주면 됩니다.

**200 중복 병합됨** (자동 감지 신고만)

```json
{"status": "duplicate", "receipt_id": null}
```

정상 처리된 것입니다. 다시 보내지 마세요.

같은 `Idempotency-Key`로 다시 보낸 경우에는 처음 응답(201 또는 200)이 그대로 다시 옵니다.

### 서버 검사 순서

먼저 실패한 검사의 오류가 반환됩니다. 디버깅할 때 참고하세요.

1. 요청 크기: `Content-Length` 없음 411, 5 MiB 초과 413
2. 폼 형식: `project_key`·`payload` 누락, `payload` 32768자 초과 → 422 `VALIDATION_FAILED`(이 응답에는 CORS 허용 헤더가 없음)
3. 키·Origin: 404 `WIDGET_NOT_FOUND`, 403 `ORIGIN_NOT_ALLOWED`
4. 업로드한 스크린샷 파일이 4 MiB 초과: 413 `UPLOAD_TOO_LARGE` (파일은 4 MiB 이하지만 PNG로 정규화한 결과가 4 MiB를 넘으면 6단계에서 422 `INVALID_SCREENSHOT`)
5. `payload` 형식(JSON, 32 KiB, 스키마): 422 `VALIDATION_FAILED`
6. 동의 → 오류 정보 → 마스킹 박스 → 이미지 형식: 422 `CONSENT_REQUIRED` / `ERROR_DETAILS_INVALID` / `MASK_BOX_INVALID` / `INVALID_SCREENSHOT`
7. 서버 설정: 503 `WIDGET_UNAVAILABLE`
8. 위젯·신고 꺼짐 403 `WIDGET_REPORTING_DISABLED`, 자동 감지 꺼짐 403 `AUTO_DETECT_DISABLED`
9. 원점 일치 422 `PAGE_URL_ORIGIN_MISMATCH` → 운영 스크린샷 403 `LIVE_SCREENSHOT_DISABLED` → 범위 422 `SCOPE_VIOLATION`
10. 같은 `Idempotency-Key` 재전송이면 처음 응답 반환(다른 내용이면 409, 키 형식 오류는 400)
11. 시간당 한도 429 → 일일 저장 상한 429 `WIDGET_DAILY_LIMITED` → 중복 병합 200 → 프로그램 상태·AI 허용 422 → 스크린샷·설명 둘 다 없음 422 → 도메인 소유 미확인 422 → 201

위젯 일일 상한(프로그램 `daily_job_limit`의 절반, 최소 1)을 넘어도 바로 오류가 되지는 않습니다. 제보는 저장되고 같은 201 접수 응답(`status: "received"`)을 받습니다. 고객사는 검증 대기 상태로 보고 나중에 직접 실행합니다.

다만 저장 공간을 지키기 위해 오늘(UTC 기준) 만들어진 위젯 제보가 위젯 일일 상한의 **4배**에 이르면 그 뒤 제보는 저장하지 않고 `429 WIDGET_DAILY_LIMITED`로 거절합니다. 이 상한은 UTC 자정에 초기화됩니다.

## 5. 오류 코드

| HTTP | code | 의미 | SDK가 할 일 |
|---|---|---|---|
| 400 | `IDEMPOTENCY_KEY_INVALID` | `Idempotency-Key`가 비었거나 128자 초과, 출력할 수 없는 문자 포함 | 키 생성 코드 수정(UUID 권장) |
| 403 | `ORIGIN_NOT_ALLOWED` | `Origin` 헤더가 없거나 등록되지 않은 원점. preflight 거절도 이 코드 | 위젯 비활성화. 고객사 관리자에게 원점 등록 안내 |
| 403 | `WIDGET_REPORTING_DISABLED` | 위젯 또는 공개 신고가 꺼져 있음 | 신고 버튼 숨김. 재시도하지 않음 |
| 403 | `AUTO_DETECT_DISABLED` | 자동 감지 신고인데 자동 감지가 꺼져 있음 | 자동 신고 중단(`config.auto_detect` 확인) |
| 403 | `LIVE_SCREENSHOT_DISABLED` | 운영(mapped) 원점에서 스크린샷을 보냈는데 운영 스크린샷이 꺼져 있음 | 스크린샷 빼고 다시 제출(`config.screenshot_allowed` 확인) |
| 404 | `WIDGET_NOT_FOUND` | 키가 없거나 회전되어 무효 | 위젯 비활성화. 고객사에 키 갱신 안내 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 같은 `Idempotency-Key`로 다른 내용을 보냄 | 사용자가 내용을 바꿨다면 새 키 발급 |
| 411 | `LENGTH_REQUIRED` | `Content-Length` 헤더 없음(스트리밍 업로드 등) | 본문 길이를 알 수 있게 `FormData`/`Blob`으로 전송 |
| 413 | `REQUEST_TOO_LARGE` | 전체 요청이 5 MiB 초과 | 스크린샷을 줄이거나 빼고 재시도 |
| 413 | `UPLOAD_TOO_LARGE` | 업로드한 스크린샷 파일이 4 MiB 초과 | 이미지 압축·축소 후 재시도 |
| 422 | `VALIDATION_FAILED` | `payload` 누락, JSON 아님, 32 KiB 초과, 스키마 위반(모르는 필드, 길이·형식 초과), `project_key` 누락 | 버그로 보고 수정. 같은 요청을 재시도하지 않음 |
| 422 | `CONSENT_REQUIRED` | 스크린샷이나 환경 정보를 보냈는데 해당 동의가 `true`가 아님 | 동의 UI 확인 후 다시 제출 |
| 422 | `ERROR_DETAILS_INVALID` | `error_type`에 맞지 않는 `error`/`error.http`, 또는 `status`가 500–599가 아님 | 자동 감지 코드 수정 |
| 422 | `MASK_BOX_INVALID` | 박스 값이 4개가 아님, 이미지 범위 밖(`x1 < x2`, `y1 < y2` 아님 포함), 스크린샷 없이 박스만 보냄. 21개 이상이거나 정수가 아니면 `VALIDATION_FAILED` | 좌표 변환(naturalWidth 기준) 확인 |
| 422 | `INVALID_SCREENSHOT` | PNG·JPEG·WebP 정지 이미지가 아님, 또는 크기 제한(각 변 4096px, 800만 화소) 초과, 또는 PNG로 정규화한 결과가 4 MiB 초과 | 지원 형식으로 변환하거나 스크린샷 없이 제출 |
| 422 | `SCREENSHOT_OR_DESCRIPTION_REQUIRED` | 스크린샷도 설명도 없음 | 사용자에게 설명 입력 요청 |
| 422 | `PAGE_URL_ORIGIN_MISMATCH` | `page_url`의 원점이 요청 `Origin`과 다름 | `location.href`를 그대로 보냈는지 확인 |
| 422 | `SCOPE_VIOLATION` | 재현 주소가 승인된 테스트 범위 밖. `error.detail.reason`에 이유(`PATH_OUT_OF_SCOPE`, `PATH_EXCLUDED`, `STATE_CHANGING_PATH`, `INVALID_URL`, `AMBIGUOUS_PATH` 등) | 이 페이지에서는 신고 불가 안내. 재시도하지 않음 |
| 422 | `PROGRAM_NOT_ACTIVE` | 프로그램이 일시중지·종료 상태 | 신고 버튼 숨김 |
| 422 | `EXTERNAL_AI_NOT_ALLOWED` | 고객사가 AI 분석을 허용하지 않음 | 신고 버튼 숨김 |
| 422 | `ORIGIN_NOT_VERIFIED` | 고객사가 테스트 대상 도메인의 소유 확인을 아직 마치지 않음 | 신고 버튼 숨김(`config.reporting_enabled`가 `false`) |
| 429 | `WIDGET_IP_RATE_LIMITED` | 같은 IP에서 이 프로젝트로 1시간에 10건 초과 | 7절대로 대기. 사용자에게 "잠시 후 다시" 안내 |
| 429 | `WIDGET_PROJECT_RATE_LIMITED` | 이 프로젝트 전체가 1시간에 100건 초과 | 7절대로 대기 |
| 429 | `WIDGET_DAILY_LIMITED` | 오늘(UTC) 저장된 위젯 제보가 위젯 일일 상한의 4배에 도달 | 오늘은 더 보내지 않음. 사용자에게 "오늘은 접수가 마감됐다"고 안내 |
| 503 | `WIDGET_UNAVAILABLE` | 서버 설정 문제로 위젯 신고를 받을 수 없음 | 잠시 뒤 재시도(7절 백오프) |
| 500 | `INTERNAL_ERROR` | 서버 내부 오류 | 같은 `Idempotency-Key`로 백오프 재시도 |

- 시간당 한도는 실제로 만들어진 위젯 제보 수로 셉니다. 중복으로 병합된 신고는 세지 않습니다.
- `403 ORIGIN_NOT_ALLOWED`, 요청 크기 오류(411·`REQUEST_TOO_LARGE`), 폼 필드 누락 422, 500 응답에는 CORS 허용 헤더가 붙지 않아
  브라우저 JavaScript에서 본문을 읽지 못하고 네트워크 오류처럼 보일 수 있습니다. 그 밖의 오류는
  Origin 확인 뒤에 나므로 SDK가 본문의 `code`를 읽을 수 있습니다.

## 6. Idempotency-Key

- 사용자가 **제출 버튼을 한 번 누를 때마다** UUID 하나를 만듭니다(`crypto.randomUUID()`).
- 네트워크 오류·타임아웃·5xx로 **같은 제출을 다시 보낼 때는 같은 키를 그대로** 씁니다. 서버는 처음
  응답을 다시 돌려주고 제보를 두 번 만들지 않습니다(24시간 보관).
- 사용자가 내용을 고쳐 다시 제출하면 **새 키**를 만드세요. 같은 키로 다른 내용을 보내면 `409 IDEMPOTENCY_KEY_REUSED`.
- 자동 감지 신고는 서버가 중복을 병합하지만, 재시도 시에는 마찬가지로 같은 키를 쓰세요.

## 7. 재시도와 429

- 4xx는 대부분 요청을 고쳐야 하는 오류입니다. **같은 요청을 자동으로 반복하지 마세요.**
- 429(`WIDGET_DAILY_LIMITED` 제외)·503·500·네트워크 오류만 재시도 대상입니다. **지수 백오프 + 지터**로 최대 3번 정도만 시도하고,
  루프로 계속 보내지 마세요(예: 30초, 2분, 10분).
- 429를 받으면 그 페이지 세션에서는 자동 감지 신고를 멈추고, 사용자에게 잠시 후 다시 시도하라고 알리세요.
- `429 WIDGET_DAILY_LIMITED`는 UTC 자정까지 풀리지 않으므로 재시도하지 마세요.
- 서버는 `Retry-After` 헤더를 보내지 않습니다.

## 8. 절대 보내지 말 것

`payload` 스키마에는 아래 정보를 넣을 필드가 없고, 모르는 필드를 보내면 요청이 거절됩니다.
`message`, `description` 같은 자유 텍스트에도 넣지 마세요.

- 쿠키(`document.cookie`), 세션 id
- 토큰·API 키·`Authorization` 헤더 값, 그 밖의 요청/응답 헤더
- 요청·응답 **본문**(fetch/XHR body, GraphQL 변수 등)
- 사용자가 입력창에 쓴 값(폼 값, 비밀번호 칸, 검색어 이외의 개인 정보)
- `localStorage`, `sessionStorage`, IndexedDB 내용
- 민감한 쿼리가 담긴 전체 URL. `error.http.path`에는 경로만, `stack_top`에는 스크립트 위치만 넣으세요.

스크린샷을 찍을 때는 `config.mask_selectors`와 입력창 값을 먼저 가리고([스크린샷 마스킹](#스크린샷-마스킹)),
그 밖의 개인 정보 영역은 사용자가 `mask_boxes`로 더 가릴 수 있게 해 주세요.

## 9. CORS

- `/api/v1/widget/*`에만 적용됩니다.
- preflight(`OPTIONS`): URL에 `project_key`가 있어야 합니다. 키가 맞고 `Origin`이 등록되어 있으면
  `204`와 아래 헤더를 줍니다. 아니면 `403 ORIGIN_NOT_ALLOWED`이며 키가 틀린 경우와 Origin이 틀린 경우를
  구분하지 않습니다. `project_key` 파라미터 자체가 없으면 `422`입니다.

  ```text
  Access-Control-Allow-Origin: <요청 Origin>
  Access-Control-Allow-Methods: GET, POST
  Access-Control-Allow-Headers: Content-Type, Idempotency-Key
  Access-Control-Max-Age: 600
  Vary: Origin
  ```

- 실제 응답에도 Origin 확인 뒤에는 `Access-Control-Allow-Origin: <요청 Origin>`과 `Vary: Origin`이 붙습니다.
- **`Access-Control-Allow-Credentials`는 절대 보내지 않습니다.** 그러므로 반드시
  `fetch(url, {credentials: "omit"})`로 호출하세요. `credentials: "include"`로 보내면 브라우저가 응답을 막습니다.
- 허용 헤더는 `Content-Type`, `Idempotency-Key`뿐입니다. 다른 사용자 정의 헤더를 넣으면 preflight가 실패합니다.

## 10. 예시

### curl

```bash
KEY=mpk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
API=https://api.metaphor.example

curl -s "$API/api/v1/widget/config?project_key=$KEY" \
  -H "Origin: https://www.example.com"

curl -s -X POST "$API/api/v1/widget/reports?project_key=$KEY" \
  -H "Origin: https://www.example.com" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F 'payload={"description":"cat 검색 시 결과가 안 나와요","page_url":"https://www.example.com/search?q=cat","consent":{"screenshot":true}}' \
  -F "screenshot=@screen.png;type=image/png"
```

### 브라우저 `fetch`

```js
const API = "https://api.metaphor.example/api/v1/widget";
const KEY = "mpk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx";

async function loadConfig() {
  const res = await fetch(`${API}/config?project_key=${encodeURIComponent(KEY)}`, {
    credentials: "omit",
  });
  if (!res.ok) return null; // 404/403이면 위젯을 띄우지 않음
  return res.json();
}

// 사용자가 제출 버튼을 누를 때마다 한 번 호출. 재시도에는 같은 idempotencyKey를 넘긴다.
async function submitReport({ description, screenshotBlob, maskBoxes, consent }, idempotencyKey) {
  const payload = {
    description,
    page_url: location.href,
    consent, // { screenshot: boolean, environment: boolean } — 사용자가 직접 체크한 값
  };
  if (consent.environment) {
    payload.environment = {
      user_agent: navigator.userAgent,
      language: navigator.language,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      viewport: { width: innerWidth, height: innerHeight },
    };
  }
  const form = new FormData();
  if (screenshotBlob && consent.screenshot) {
    form.append("screenshot", screenshotBlob, "screenshot.png");
    if (maskBoxes?.length) payload.mask_boxes = maskBoxes; // naturalWidth/Height 픽셀 기준
  }
  form.append("payload", JSON.stringify(payload));

  const res = await fetch(`${API}/reports?project_key=${encodeURIComponent(KEY)}`, {
    method: "POST",
    body: form, // Content-Type은 브라우저가 boundary와 함께 자동 설정
    headers: { "Idempotency-Key": idempotencyKey },
    credentials: "omit",
  });
  const body = await res.json().catch(() => null);
  if (res.status === 201 || res.status === 200) return body; // received | duplicate
  throw Object.assign(new Error(body?.error?.code ?? `HTTP_${res.status}`), { status: res.status, body });
}

// 자동 감지 예시(관리자가 auto_detect를 켠 경우에만)
function reportJsError(event, config) {
  if (!config?.auto_detect?.js_error) return;
  const payload = {
    page_url: location.href,
    error_type: "js_error",
    error: {
      message: String(event.message).slice(0, 1000),
      stack_top: event.filename ? `${event.filename.split("?")[0]}:${event.lineno}:${event.colno}` : undefined,
    },
  };
  const form = new FormData();
  form.append("payload", JSON.stringify(payload));
  fetch(`${API}/reports?project_key=${encodeURIComponent(KEY)}`, {
    method: "POST",
    body: form,
    headers: { "Idempotency-Key": crypto.randomUUID() },
    credentials: "omit",
  }).catch(() => {}); // 자동 신고 실패는 사용자 경험을 막지 않음
}
```
