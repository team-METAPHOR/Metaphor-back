# 셀프서비스 온보딩 API (회사·멤버·초대)

고객사가 영업 담당 없이 회사를 만들고 팀원을 합류시키는 플랫폼 API입니다.
기준 코드는 `app/api/v1/endpoints/orgs.py`, `app/services/orgs.py`, `app/services/invites.py`이고,
문서와 코드가 다르면 코드를 따릅니다.

- 경로는 모두 `/api/v1` 아래에 있습니다. 로그인 쿠키(`sid`)가 필요하고, 쓰기 요청은 허용된 `Origin`이어야 합니다(CSRF).
- 회사 구성원이 아니면 회사·멤버·초대 모두 **404 `NOT_FOUND`** 입니다. 구성원이지만 권한이 모자라면 **403 `FORBIDDEN`** 입니다.
- 오류 응답은 공통 형식 `{"error": {"code", "message", "request_id"}}`이고, 시각은 UTC ISO 8601입니다.
- 내 회사 목록은 `GET /auth/me`의 `memberships`(`org_id`, `org_name`, `role`)로 확인합니다.

## 회사

| 요청 | 권한 | 응답 |
|---|---|---|
| `POST /orgs {name}` (1~200자) | 로그인 사용자 | 201 `{id, name, role: "admin", created_at}` |
| `GET /orgs/{org_id}` | 구성원 | 200 `{id, name, role, member_count, created_at}` |
| `PATCH /orgs/{org_id} {name}` | admin | 200 (GET과 같은 형식) |

- 만든 사람이 admin이 됩니다.
- 사용자당 하루 3개까지 만들 수 있습니다. 넘으면 429 `ORG_CREATE_LIMITED`입니다.
- `Idempotency-Key` 헤더를 지원합니다. 같은 키로 다시 보내면 처음 응답을 그대로 돌려주고, 같은 키에 다른 본문을 보내면 409 `IDEMPOTENCY_KEY_REUSED`입니다.

## 멤버

| 요청 | 권한 | 응답 |
|---|---|---|
| `GET /orgs/{org_id}/members` | 구성원 | 200 `[{user_id, email, display_name, role, is_active, joined_at}]` |
| `PATCH /orgs/{org_id}/members/{user_id} {role}` | admin | 200 (멤버 한 명) |
| `DELETE /orgs/{org_id}/members/{user_id}` | admin, 또는 본인(나가기) | 204 |

- 비활성 멤버는 admin에게만 보입니다.
- `role`은 `admin`, `developer`, `viewer` 중 하나입니다(초대의 `role`도 같습니다).

| 역할 | 할 수 있는 일 |
|---|---|
| `admin` | 회사 설정·멤버·초대, 프로그램·테스트 범위, 위젯 설정·키, Origin 소유 확인 + developer의 모든 권한 |
| `developer` | 제보 등록, 재검증(fresh/retest), 추가 답변, 작업 취소, 처리 상태 변경, 담당자 지정 + 회사 안의 모든 조회 |
| `viewer` | 조회만(제보 목록·상세·결과·증거·핸드오프·멤버 목록). 쓰기 요청은 모두 403 `FORBIDDEN`. 스스로 나가기는 가능 |

- 예전 `reviewer`는 `developer`로 바뀌었습니다(마이그레이션 0010).
- 회사의 마지막 활성 admin은 강등하거나 내보내거나 스스로 나갈 수 없습니다(409 `LAST_ADMIN`). 두 admin이 동시에 서로를 강등해도 한 명은 남습니다.
- 내보내거나 나가면 그 사용자의 **모든 로그인 세션이 폐기**됩니다. 다시 로그인해야 하고, 그 뒤에는 이 회사의 자원이 모두 404입니다.

## 초대

| 요청 | 권한 | 응답 |
|---|---|---|
| `POST /orgs/{org_id}/invites {role, expires_in_days}` | admin | 201 `{id, role, expires_at, token}` |
| `GET /orgs/{org_id}/invites` | admin | 200 `[{id, role, created_by, expires_at}]` (대기 중인 초대만) |
| `DELETE /orgs/{org_id}/invites/{invite_id}` | admin | 204 (취소) |
| `POST /invites/accept {token}` | 로그인 사용자 | 200 `{org_id, org_name, role}` |

- `expires_in_days`는 1~30, 기본값 7입니다.
- 대기 중인 초대는 회사당 50개까지입니다. 넘으면 409 `INVITE_LIMIT`입니다.
- `token`은 **생성 응답에서 한 번만** 내려갑니다. 서버에는 sha256 해시만 남고, 목록·로그·감사 기록에는 원문이 없습니다. 잃어버리면 취소하고 새로 만듭니다.
- 초대는 한 번만 쓸 수 있습니다. 동시에 여러 번 수락해도 한 번만 성공합니다.
- 없는 토큰, 이미 쓴 토큰, 취소된 토큰, 만료된 토큰은 모두 같은 404 `INVITE_INVALID`입니다. 어떤 이유인지 구분해 알려 주지 않습니다.
- 이미 활성 멤버이면 409 `ALREADY_MEMBER`이고, 초대는 소비되지 않습니다.
- 예전에 내보내진 멤버가 수락하면 **초대에 적힌 역할**로 다시 활성화됩니다. 예전 역할로 돌아가지 않습니다.
- 수락 실패는 사용자당 1시간에 20번까지입니다. 넘으면 429 `INVITE_ACCEPT_LIMITED`입니다.

### 초대 링크 형식

이메일 발송 기능은 없습니다. admin이 아래 링크를 만들어 직접 전달합니다.

```text
https://<platform>/invite#<token>
```

프론트엔드는 `location.hash`에서 토큰을 읽어 로그인(또는 가입)을 마친 뒤 `POST /api/v1/invites/accept`의 본문으로 보냅니다.

토큰을 쿼리(`?token=`)나 경로가 아니라 **fragment(`#`)** 에 두는 이유는, fragment가 브라우저 밖으로 나가지 않기 때문입니다.
HTTP 요청에 실리지 않으므로 웹 서버·프록시·CDN의 접근 로그에 남지 않고, 다른 사이트로 이동할 때 `Referer` 헤더에도 들어가지 않습니다.
토큰은 요청 본문으로만 서버에 전달되며, 서버는 요청 본문을 로그에 남기지 않습니다.
