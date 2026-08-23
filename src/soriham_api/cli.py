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
    boot = sub.add_parser("bootstrap", help="첫 운영자와 워크스페이스를 만든다")
    boot.add_argument("--email", required=True)
    boot.add_argument("--name", required=True, help="표시 이름")
    boot.add_argument("--workspace-slug", required=True)
    boot.add_argument("--workspace-name", required=True)
    approve_p = sub.add_parser("approve", help="가입 신청을 승인하거나 거절한다")
    approve_p.add_argument("--email", required=True)
    approve_p.add_argument("--reject", action="store_true", help="승인 대신 거절")
    pending_p = sub.add_parser("pending", help="승인 대기 중인 신청을 나열한다")
    pending_p.add_argument("--limit", type=int, default=50)
    quota_p = sub.add_parser("quota", help="워크스페이스 사용량 한도를 정한다")
    quota_p.add_argument("--workspace", required=True, help="슬러그")
    quota_p.add_argument("--minutes", help="전사 시간 한도(분). unlimited면 무제한")
    quota_p.add_argument("--gb", help="저장 용량 한도(GB). unlimited면 무제한")
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

    if args.command == "bootstrap":
        from soriham_api.tenancy import bootstrap

        password = _read_password()
        with session_factory() as session:
            try:
                user, workspace, created = bootstrap(
                    session,
                    email=args.email,
                    password=password,
                    display_name=args.name,
                    workspace_slug=args.workspace_slug,
                    workspace_name=args.workspace_name,
                )
            except ValueError as exc:
                parser.error(str(exc))
            session.commit()
            if created:
                print(f"운영자 {user.email} / 워크스페이스 {workspace.slug} 만들었습니다")
            else:
                print(f"이미 있습니다: {user.email}")
        return 0

    if args.command == "pending":
        from sqlalchemy import select

        from soriham_api.models import User

        with session_factory() as session:
            rows = session.scalars(
                select(User)
                .where(User.status == "pending")
                .order_by(User.created_at)
                .limit(args.limit)
            ).all()
        if not rows:
            print("승인 대기 중인 신청이 없습니다")
            return 0
        for row in rows:
            note = f" — {row.signup_note}" if row.signup_note else ""
            print(f"{row.created_at:%Y-%m-%d %H:%M}  {row.email}  {row.display_name}{note}")
        return 0

    if args.command == "approve":
        from soriham_api.tenancy import approve, find_user, reject

        with session_factory() as session:
            user = find_user(session, args.email)
            if user is None:
                parser.error(f"그런 계정이 없습니다: {args.email}")
            if args.reject:
                reject(session, user)
                session.commit()
                print(f"거절했습니다: {user.email}")
            else:
                approve(session, user)
                session.commit()
                print(f"승인했습니다: {user.email}")
        return 0

    if args.command == "quota":
        from soriham_api.tenancy import WorkspaceNotFound, get_workspace

        with session_factory() as session:
            try:
                workspace = get_workspace(session, args.workspace)
            except WorkspaceNotFound as exc:
                parser.error(str(exc))
            if args.minutes is not None:
                workspace.quota_minutes = _quota_value(parser, args.minutes)
            if args.gb is not None:
                gb = _quota_value(parser, args.gb)
                workspace.quota_bytes = None if gb is None else gb * 1024 * 1024 * 1024
            session.commit()
            print(
                f"{workspace.slug}: 전사 {_show_minutes(workspace.quota_minutes)}, "
                f"저장 {_show_bytes(workspace.quota_bytes)}"
            )
        return 0

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


def _read_password() -> str:
    """비밀번호는 인자로 받지 않는다 — 셸 기록과 프로세스 목록에 남는다."""
    import getpass

    first = getpass.getpass("비밀번호: ")
    if not first:
        raise SystemExit("비밀번호가 비어 있습니다")
    if first != getpass.getpass("다시 한 번: "):
        raise SystemExit("두 번 입력이 다릅니다")
    return first


def _quota_value(parser: argparse.ArgumentParser, raw: str) -> int | None:
    if raw.lower() in ("unlimited", "none", ""):
        return None
    try:
        value = int(raw)
    except ValueError:
        parser.error(f"숫자나 unlimited여야 합니다: {raw}")
    if value < 0:
        parser.error("한도는 음수일 수 없습니다")
    return value


def _show_minutes(value: int | None) -> str:
    return "무제한" if value is None else f"{value}분"


def _show_bytes(value: int | None) -> str:
    if value is None:
        return "무제한"
    gb = value / (1024 * 1024 * 1024)
    return f"{gb:.0f}GB" if gb >= 1 else f"{value}바이트"


if __name__ == "__main__":
    raise SystemExit(main())
