# Metaphor Backend

웹 버그 자동 재현 플랫폼의 FastAPI 백엔드입니다.
현재는 초기 레이어드 구조와 실행 설정만 구성한 상태입니다.

## 아키텍처

```text
HTTP 요청
   ↓
API / Router          요청 검증, 응답 변환, 의존성 연결
   ↓
Service               권한·업무 규칙, 상태 전이, 트랜잭션
   ↓
Repository            조회·저장 쿼리
   ↓
Model / PostgreSQL    ORM 모델과 영속 데이터

Schema                API 요청·응답 형식
Core                  공통 설정과 정책
Integration           모델 API·작업 큐·증거 저장소 어댑터
Worker                별도 프로세스에서 실행할 검증 작업
```

```text
app/
├── main.py                 FastAPI 생성, 라우터 등록, 종료 처리
├── api/
│   ├── dependencies.py     요청 단위 의존성
│   └── v1/
│       ├── router.py       /api/v1 라우터 조합
│       └── endpoints/      기능별 HTTP 엔드포인트
├── services/               업무 로직
├── repositories/           데이터 접근
├── models/                 SQLAlchemy 모델과 공통 Base
├── schemas/                Pydantic 요청·응답 스키마
├── core/                   환경 설정
├── db/                     비동기 엔진·세션
├── integrations/           외부 시스템 연동 자리
└── worker/                 검증 워커 자리
migrations/                 Alembic 설정과 마이그레이션
```

### 레이어 규칙

- API는 SQL을 작성하지 않고 Service를 호출합니다. 단순 프로세스 상태 확인은 예외입니다.
- Service는 HTTP 요청 객체에 의존하지 않습니다. Repository와 외부 어댑터를 호출합니다.
- 트랜잭션은 Service에서 `async with session.begin():`으로 묶습니다. Repository는 `commit()`하지 않습니다.
- Repository는 요청에 주입된 세션을 사용합니다. 저장 직후 값이 필요하면 `flush()`합니다.
- ORM 모델을 API 응답으로 직접 노출하지 않고 Schema로 변환합니다.
- 기능 추가 시 각 레이어에 같은 이름의 모듈을 둡니다. 예: `endpoints/reports.py`, `services/reports.py`, `repositories/reports.py`, `models/report.py`, `schemas/report.py`.
- 인증·회사 접근 검사는 구현 시 API 의존성과 Service에 연결합니다. 현재는 인증·권한 기능이 없습니다.
- 브라우저 검증은 API 프로세스에서 실행하지 않습니다. 추후 영속 큐를 통해 별도 Worker로 전달합니다.

DB는 API v1 초안에 맞춰 PostgreSQL을 기준으로 구성했습니다.
도메인 테이블과 구체적인 API 계약은 후속 구현에서 추가합니다.

## 로컬 실행

Python 3.12 이상과 [uv](https://docs.astral.sh/uv/)를 사용합니다. `.python-version`은 3.13입니다.

```bash
cp .env.example .env
uv sync
uv run uvicorn app.main:app --reload
```

- API 상태 확인: `GET http://127.0.0.1:8000/api/v1/health`
- API 문서: `http://127.0.0.1:8000/docs`
- 상태 응답: `{"status":"ok"}`. 프로세스 확인용이며 DB·큐·워커 연결 상태를 의미하지 않습니다.
- `main.py`는 `uvicorn main:app` 실행을 위한 호환 진입점입니다.

### 로컬 DB

DB를 사용하는 기능 개발 시 Docker Compose로 개발용 DB를 실행할 수 있습니다.
Compose의 계정 정보는 로컬 개발용입니다.

```bash
docker compose up -d db
uv run alembic upgrade head
```

현재 도메인 모델과 revision은 없으므로 업무 테이블은 생성되지 않습니다.
모델 추가 후 `app/models/__init__.py`에 해당 모듈을 import하고 마이그레이션을 생성합니다.

```bash
uv run alembic revision --autogenerate -m "create initial domain tables"
uv run alembic upgrade head
```

앱 시작 시 테이블을 자동 생성하지 않습니다. 스키마 변경은 Alembic으로 관리합니다.

## 개발 확인

```bash
docker compose up -d db        # 테스트 DB는 체크아웃(worktree)마다 metaphor_test_<해시>로 자동 생성
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

## 현재 구현 범위

- 구성 완료: 레이어별 패키지, 환경 설정, DB 세션, Alembic, 상태 확인 API.
- 후속 구현: 회사·사용자·인증, 프로그램·범위, 신고·결정, 검증 작업, 근거, 운영 관제.
- 후속 연동: 영속 큐·아웃박스, 격리 브라우저 워커, 모델 어댑터, 비공개 증거 저장소, SSE.

Service·Repository·Integration·Worker는 책임을 나눈 패키지이며 실제 업무 기능은 아직 없습니다.
