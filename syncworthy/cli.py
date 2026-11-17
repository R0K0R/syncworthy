"""``syncworthy serve <project-dir>``"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="syncworthy",
                                     description="Real-time collaborative editor for any project.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="serve a project directory")
    serve.add_argument("project", type=Path, help="the project directory")
    serve.add_argument("--bind", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    serve.add_argument("-p", "--port", type=int, default=8000, help="port (default: 8000)")
    serve.add_argument("--headless", action="store_true", help="do not open a browser")
    serve.add_argument("--packet-log", action="store_true", help="log low-level Yjs packets")
    serve.add_argument("--plugin", action="append", default=[], metavar="NAME",
                       help="load a plugin (entry-point name or module:attr) in addition "
                            "to those in syncworthy.toml; repeatable")

    sub.add_parser("plugins", help="list installed plugins (entry-point group syncworthy.plugins)")

    args = parser.parse_args(argv)

    if args.command == "plugins":
        from importlib import metadata
        from .plugins import ENTRY_POINT_GROUP
        for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
            print(f"{ep.name}\t{ep.value}")
        return 0

    from . import run_server
    from .config import Config

    if not args.project.is_dir():
        print(f"syncworthy: {args.project} is not a directory", file=sys.stderr)
        return 2
    config = Config.load(args.project)
    config.plugins += [p for p in args.plugin if p not in config.plugins]
    run_server(args.project, host=args.bind, port=args.port, open_browser=not args.headless,
               packet_log=args.packet_log, config=config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
