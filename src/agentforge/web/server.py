"""Explicit single-process dashboard entry point, portable to native Windows."""

import argparse
import ipaddress
import logging
from functools import partial

import uvicorn

from agentforge.application.service import Application
from agentforge.web.app import create_app


class SafeDiagnostics(logging.Filter):
    def filter(self, record):
        if record.name not in {__name__, "agentforge.web.app"}:
            record.msg = "Dashboard server lifecycle diagnostic."
            record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentForge trusted local dashboard")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--workers", required=True)
    parser.add_argument("--coding", help="Explicit trusted coding TOML configuration")
    parser.add_argument("--concurrency", type=int, choices=range(1, 33), default=1)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allowed-host", action="append", default=[])
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if any("*" in host or "/" in host for host in [args.host, *args.allowed_host]):
        parser.error("use explicit bind addresses and host names")
    handler = logging.StreamHandler()
    handler.addFilter(SafeDiagnostics())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback:
        logging.getLogger(__name__).warning(
            "Non-loopback dashboard bind: no authentication layer; protect with "
            "a trusted network/VPN or authenticated reverse proxy."
        )
    host = f"[{args.host}]" if ":" in args.host else args.host
    web = create_app(
        partial(
            Application.from_config,
            database_url=args.database_url,
            workers_path=args.workers,
            coding_path=args.coding,
            concurrency=args.concurrency,
        ),
        allowed_hosts=tuple(
            dict.fromkeys(["127.0.0.1", "localhost", "[::1]", host, *args.allowed_host])
        ),
    )
    try:
        uvicorn.run(
            web,
            host=args.host,
            port=args.port,
            workers=1,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.getLogger(__name__).error(
            "service_unavailable: check dashboard configuration and migrations."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
