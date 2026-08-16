# soriham-api

소리함 — 음성 녹음 아카이브(로컬 STT, 화자분리, AI 요약, 검색) — 의 백엔드입니다.
녹음 폴더를 스캔·감시해 데이터베이스에 등록하고, 변환·요약 파이프라인을 진행하는
워커와 검색·재생용 REST API를 제공합니다.

아직 코드가 없는 부트스트랩 상태입니다. 예정 스택: Python 3.12+, FastAPI,
PostgreSQL(alembic 마이그레이션, pg_trgm 검색).

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
