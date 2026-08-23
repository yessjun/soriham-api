"""soriham-api 명령행 인터페이스."""

from __future__ import annotations

import argparse
import logging

from soriham_api.config import load_settings
from soriham_api.db import make_session_factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="soriham-api", description="소리함 백엔드 도구")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="녹음 폴더를 스캔해 신규 파일을 등록")
    sub.add_parser("watch", help="녹음 폴더를 감시해 새 파일을 자동 등록")
    sub.add_parser("worker", help="큐를 소비해 변환·엔리치먼트를 실행")
    create_ws = sub.add_parser("create-workspace", help="워크스페이스를 만든다")
    create_ws.add_argument("--slug", required=True, help="소문자·숫자·하이픈")
    create_ws.add_argument("--name", required=True)
    create_ws.add_argument("--kind", choices=("personal", "team"), default="team")
    serve = sub.add_parser("serve", help="REST API 서버 실행")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8200)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if args.command in ("scan", "watch") and not settings.audio_dirs:
        parser.error("AUDIO_DIRS가 설정돼 있지 않습니다 (.env 확인)")
    if args.command in ("scan", "watch") and not settings.default_workspace:
        parser.error("DEFAULT_WORKSPACE가 설정돼 있지 않습니다 (.env 확인)")
    session_factory = make_session_factory(settings)

    if args.command == "create-workspace":
        from soriham_api.tenancy import create_workspace, find_workspace

        with session_factory() as session:
            if find_workspace(session, args.slug) is not None:
                print(f"이미 있습니다: {args.slug}")
                return 0
            workspace = create_workspace(session, slug=args.slug, name=args.name, kind=args.kind)
            session.commit()
            print(f"만들었습니다: {workspace.slug} ({workspace.public_id})")
        return 0

    def _workspace_id(session) -> int:
        from soriham_api.tenancy import WorkspaceNotFound, get_workspace

        try:
            return get_workspace(session, settings.default_workspace or "").id
        except WorkspaceNotFound as exc:
            parser.error(str(exc))

    if args.command == "scan":
        from soriham_api.ingest import scan

        with session_factory() as session:
            stats = scan(session, settings.audio_dirs, workspace_id=_workspace_id(session))
        print(
            "스캔 완료: 신규 {new}, 중복 {duplicate}, 재등장 {reappeared}, 유실 {missing}".format(
                **stats
            )
        )
        return 0

    if args.command == "watch":
        from soriham_api.watch import watch

        with session_factory() as session:
            ws_id = _workspace_id(session)
        watch(session_factory, settings.audio_dirs, workspace_id=ws_id)
        return 0

    if args.command == "worker":
        from soriham_api.enrich import build_enricher
        from soriham_api.stt_client import RunnerClient
        from soriham_api.worker import run_worker

        runner = RunnerClient(base_url=settings.runner_url, upload=settings.runner_upload)
        enricher = build_enricher(
            settings.enrich_backend,
            ollama_url=settings.ollama_url,
            ollama_model=settings.ollama_model,
        )
        run_worker(
            session_factory,
            runner,
            model=settings.stt_model,
            language=settings.stt_language,
            enricher=enricher,
        )
        return 0

    if args.command == "serve":
        import uvicorn

        from soriham_api.app import create_app

        uvicorn.run(create_app(settings, session_factory), host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
