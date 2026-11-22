"""
QueryBridge: natural-language to SQL intelligence system.

Start the web interface with:

    python app.py                 # opens http://127.0.0.1:8501 in your browser
    python app.py --port 8600     # another port (default: $PORT if set, else 8501)
    python app.py --no-browser    # do not open a browser window

The server only listens on this computer (127.0.0.1) unless --host is given.
On a host such as Render: python app.py --host 0.0.0.0 --no-browser, with
QB_HOSTED=true (see render.yaml); the public host name is accepted
automatically from RENDER_EXTERNAL_HOSTNAME, or set QB_ALLOWED_HOSTS.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import sys
import threading
import webbrowser

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the QueryBridge web interface.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Address to listen on (default 127.0.0.1: only this computer can connect).")
    parser.add_argument("--port", type=int, default=_default_port(),
                        help="Port to listen on (default: the PORT environment variable if set, else 8501).")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window.")
    return parser.parse_args(argv)


def _default_port() -> int:
    try:
        return int(os.environ.get("PORT", "") or 8501)
    except ValueError:
        return 8501


def allowed_hosts_for(host: str, extra: list[str]) -> list[str]:
    """Host names the server accepts: the local ones, ``extra`` (the public name) and the listen address."""
    from web.server import LOCAL_HOSTS

    allowed = list(LOCAL_HOSTS)
    for name in extra:
        if name not in allowed:
            allowed.append(name)
    if not _is_loopback(host):
        name = socket.gethostname() if host in ("0.0.0.0", "::") else host
        if name not in allowed:
            allowed.append(name)
    return allowed


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not 1 <= args.port <= 65535:
        print("The port must be between 1 and 65535.", file=sys.stderr)
        return 2

    import uvicorn

    from config import get_settings
    from utils.logger import get_logger, setup_logging
    from web.server import create_app

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)
    logger = get_logger("querybridge.app")

    if not _port_free(args.host, args.port):
        print(f"Port {args.port} is already in use (is QueryBridge already running?). "
              f"Open http://127.0.0.1:{args.port} or start with --port {args.port + 1}.", file=sys.stderr)
        return 1

    allowed = allowed_hosts_for(args.host, settings.extra_allowed_hosts)
    if settings.hosted:
        logger.info("Hosted mode: changing the database from the browser is turned off. Accepting hosts: %s",
                    ", ".join(allowed))
    elif not _is_loopback(args.host):
        logger.warning("Listening on %s: other computers on the network can reach QueryBridge. "
                       "Only do this on a network you trust.", args.host)

    print("Starting QueryBridge… (the first start builds the sample database, which takes a few seconds)")
    app = create_app(allowed_hosts=allowed)
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{'[' + shown_host + ']' if ':' in shown_host else shown_host}:{args.port}"
    print(f"\n  QueryBridge is running at {url}\n  Press Ctrl+C to stop it.\n")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False,
                server_header=False, date_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
