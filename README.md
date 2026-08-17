# soriham-api

소리함 — 음성 녹음 아카이브(로컬 STT, 화자분리, AI 요약, 검색) — 의 백엔드입니다.
녹음 폴더를 스캔·감시해 데이터베이스에 등록하고, 변환·요약 파이프라인을 진행하는
워커와 검색·재생용 REST API를 제공합니다.

스택: Python 3.12+, FastAPI, PostgreSQL(alembic 마이그레이션, pg_trgm 검색).

## 실행

```bash
cp .env.example .env   # 값 채우기
docker compose up -d   # postgres
uv sync
uv run alembic upgrade head

uv run soriham-api scan     # 녹음 폴더 스캔 등록
uv run soriham-api watch    # 새 파일 감시 등록
uv run soriham-api worker   # 변환 파이프라인 워커 (stt 러너 필요)
                            # 제목/요약/태그는 기본 Ollama(qwen3), ENRICH_BACKEND로 교체
uv run soriham-api serve    # REST API (기본 8200 포트)
```

## REST API

| 메서드·경로 | 역할 |
|---|---|
| `GET /api/recordings` | 목록 (q, status, tag 필터, 페이지네이션) |
| `GET /api/recordings/{id}` | 상세 (세그먼트, 화자 이름, 태그) |
| `PATCH /api/recordings/{id}` | 제목 수정 |
| `PUT /api/recordings/{id}/speakers/{key}` | 화자 표시 이름 수정 |
| `GET /api/recordings/{id}/audio` | 오디오 스트리밍 (Range 지원) |
| `POST·DELETE /api/recordings/{id}/tags…` | 태그 추가·제거 |
| `GET /api/tags` | 태그 목록 |
| `GET /api/search?q=` | 전체 검색 (세그먼트·파일명·제목·요약) |
| `GET /api/stats` | 상태별 집계, 처리 배속, ETA, 최근 에러 |

id는 전부 uuid(공개 식별자)입니다. 원본 오디오는 제자리 인덱싱하며 이동·복사하지
않습니다.

## 전체 아키텍처

<!-- arch:begin -->
```
[녹음 폴더] ──스캔·감시──▶ [soriham-api: 인제스트 + PostgreSQL]
                                     │
                                     ▼
                            [soriham-api: 워커] ──HTTP 잡 API──▶ [soriham-stt: 변환 러너]
                                     │                            (whisper + 화자분리)
                                     ▼
[브라우저] ◀──▶ [soriham-console 웹 UI] ──REST──▶ [soriham-api: FastAPI]
```

| 레포지토리 | 역할 |
|---|---|
| [soriham-api](https://github.com/yessjun/soriham-api) | FastAPI 백엔드와 처리 워커 (Python, PostgreSQL) |
| [soriham-console](https://github.com/yessjun/soriham-console) | 웹 콘솔 (React, TypeScript) |
| [soriham-stt](https://github.com/yessjun/soriham-stt) | 음성 변환 러너 (whisper 계열, pyannote) |
<!-- arch:end -->
