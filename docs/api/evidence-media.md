# 재현 영상·Playwright 테스트 API

엔진(core_engine 0.8.2, `308a9c0` 고정)이 만든 마스킹 재현 영상(WebM)과 내보낸 Playwright 테스트(`reproduction.spec.ts`)를 근거 파일로 저장하고 내려줍니다. 권한·만료·감사 기록은 다른 근거 파일과 같습니다(소유 조직 활성 멤버만, 다른 조직·만료 파일은 404, 내려받을 때마다 `evidence.download` 감사 기록).

## 영상 켜기 (운영자)

| 환경변수 (워커 전용) | 기본값 | 의미 |
|---|---|---|
| `QA_VIDEO_ENABLED` | 꺼짐 | `1`/`true`/`yes`/`on`이면 엔진 `BrowserOptions.record_video`를 켭니다. |
| `FFMPEG_BINARY` | 없음 | 엔진이 쓰는 인코더 경로. 없으면 `imageio-ffmpeg`, 그다음 `PATH`의 `ffmpeg`를 씁니다. |

워커는 시작할 때 영상이 켜져 있는데 인코더를 못 찾으면 `VIDEO_ENCODER_UNAVAILABLE` 오류 로그를 남깁니다. 워커는 계속 돌고, 각 영상은 `status: "unavailable"`로 기록됩니다. 영상 실패는 QA 판정에 영향을 주지 않습니다.

## 근거 종류

| `kind` | 파일 | `Content-Type` | 내려주는 방식 |
|---|---|---|---|
| `video` | 마스킹 화면 샘플링 WebM (오디오 없음) | `video/webm` | `inline`, `Range` 지원 |
| `playwright_spec` | `reproduction.spec.ts` | `text/plain; charset=utf-8` | `attachment`, `nosniff` |

두 종류 모두 엔진 원본 바이트를 그대로 저장합니다(텍스트 마스킹을 하지 않음). 저장한 `sha256`이 엔진이 보고한 값과 다르면 등록하지 않습니다. Playwright trace(`trace.zip`)·HAR는 엔진이 만들지 않으므로 제공하지 않습니다(`media.trace_available`은 항상 `false`).

## `GET /validation-jobs/{job_id}/artifacts`의 `media`

```json
{"items": [...],
 "media": {
   "video_enabled": true,
   "videos": [{"attempt": 1, "revision": 0, "plan_sha256": "…", "status": "ready",
               "truncated": false, "capture_error": null, "has_audio": false,
               "sampling_fps": 4, "frames_captured": 40, "start_offset_seconds": 0.5,
               "duration_seconds": 10.0, "artifact_id": "…"}],
   "test_export": {"status": "ready", "plan_sha256": "…", "artifact_id": "…",
                   "required_environment": ["QA_RESET_MODULE"], "error_code": null},
   "trace_available": false}}
```

- `video_enabled`: `false`는 영상이 꺼진 실행(이때 `videos`는 빈 목록), `null`은 작업이 아직 진행 중이거나 보존 기간이 지나 결과가 지워진 경우입니다.
- `videos[].status`: `ready`(재생 가능), `failed`(인코딩·저장 실패), `unavailable`(인코더 없음), `empty`(프레임 없음), `interrupted`(마무리 중 취소). `truncated: true`는 길이·용량 한도로 잘린 부분 녹화이며 이유는 `capture_error`(`VIDEO_FRAME_BUDGET`, `VIDEO_CAPTURE_INTERRUPTED` 등)에 있습니다.
- `artifact_id`는 저장·해시 검증이 끝났고 만료되지 않은 파일일 때만 값이 있습니다. 다운로드는 이 id로만 하며, 엔진 파일명은 응답에 나오지 않습니다.
- `test_export`: 재현된 실행에서만 나옵니다(그 외 `null`). `status`는 `ready` 또는 `failed`(`error_code: "TEST_EXPORT_FAILED"`). `required_environment`는 테스트를 돌릴 때 설정할 환경변수 이름입니다(초기화 모듈 `QA_RESET_MODULE`, 인증이 필요한 경우 `QA_AUTHORIZATION`/`QA_COOKIE`/`QA_STORAGE_STATE` 중 하나). 테스트에는 계정 비밀값이 들어가지 않습니다. 정상 동작을 검사하므로 결함 버전에서는 실패하고 수정 버전에서는 통과해야 합니다. 이 동작은 `tests/e2e/test_export_check.py`가 실제 엔진·API로 내려받은 테스트를 `npx playwright test`로 돌려 확인합니다(`npm ci --prefix tests/e2e/js` 필요, 없으면 해시·환경변수 검사만 하고 실행 단계는 건너뜀).

## `GET /evidence/{artifact_id}/download`의 `Range`

`video`만 단일 바이트 범위를 지원합니다. 다른 종류는 `Range`를 무시하고 전체 파일(200)을 줍니다.

| 요청 | 응답 |
|---|---|
| `Range` 없음 | 200, 전체 파일, `Accept-Ranges: bytes` |
| `bytes=0-99`, `bytes=1000-`, `bytes=-500` | 206, `Content-Range: bytes 시작-끝/전체크기` (끝이 크기를 넘으면 마지막 바이트로 맞춤) |
| 범위가 파일 밖, `끝 < 시작`, `bytes=-0`, 여러 범위, 다른 단위, 형식 오류 | 416 `RANGE_NOT_SATISFIABLE`, `Content-Range: bytes */전체크기` |

`report.md`의 영상·테스트 링크도 스크린샷처럼 이 다운로드 URL로 바뀝니다.
