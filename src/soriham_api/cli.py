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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if not settings.audio_dirs:
        parser.error("AUDIO_DIRS가 설정돼 있지 않습니다 (.env 확인)")
    session_factory = make_session_factory(settings)

    if args.command == "scan":
        from soriham_api.ingest import scan

        with session_factory() as session:
            stats = scan(session, settings.audio_dirs)
        print(
            "스캔 완료: 신규 {new}, 중복 {duplicate}, 재등장 {reappeared}, 유실 {missing}".format(
                **stats
            )
        )
        return 0

    if args.command == "watch":
        from soriham_api.watch import watch

        watch(session_factory, settings.audio_dirs)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
