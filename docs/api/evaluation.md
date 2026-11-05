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
