"""Local development server for the Phase C offline build.

Binds loopback only, speaks plain HTTP, serves no static directory and makes no
outbound connection.  This is a demo convenience, not a deployment: publishing,
hosting or exposing this process is Phase E work with separate owner approval.
"""

from __future__ import annotations

import argparse
import os
import socketserver
import pathlib
import sys
from urllib.parse import urlunsplit
from wsgiref.simple_server import WSGIServer, make_server


class ThreadingWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True

from adapters.youcam_stub import AdapterError, LiveTransportAbsent
from ui.service import ReturnService
from ui.webapp import Application


def build_app(
    data_dir: str | pathlib.Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    preview_mode: str = "MOCK",
    public_host: str | None = None,
) -> tuple[Application, ReturnService]:
    service = ReturnService(data_dir, preview_mode=preview_mode)
    names = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if public_host:
        # A hosted deployment sits behind a TLS proxy, so the Host header is the public name, not
        # the loopback address. Only the explicitly named host is accepted; nothing is wildcarded.
        names.add(public_host)
        names.add(f"{public_host}:443")
        names.add(f"{public_host}:80")
    allowed = frozenset(names)
    return Application(service, allowed_hosts=allowed, enforce_host=True), service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SwapSure offline Phase C demo server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument(
        "--bind",
        default=os.environ.get("SWAPSURE_BIND", "127.0.0.1"),
        help="interface to bind; keep 127.0.0.1 unless a TLS proxy fronts this process",
    )
    parser.add_argument(
        "--public-host",
        default=os.environ.get("SWAPSURE_PUBLIC_HOST") or None,
        help="public hostname the proxy sends in the Host header; enables hosted mode",
    )
    parser.add_argument(
        "--threaded",
        action="store_true",
        default=os.environ.get("SWAPSURE_THREADED") == "1",
        help="serve concurrent requests; SQLite runs in WAL mode behind a per-store lock",
    )
    parser.add_argument(
        "--preview-mode",
        default=os.environ.get("SWAPSURE_PREVIEW_MODE", "MOCK"),
        choices=("MOCK", "RECORDED", "LIVE"),
    )
    args = parser.parse_args(argv)

    try:
        app, service = build_app(
            args.data_dir,
            port=args.port,
            preview_mode=args.preview_mode,
            public_host=args.public_host,
        )
    except (LiveTransportAbsent, AdapterError) as exc:
        print(f"PREVIEW_MODE_UNAVAILABLE: {exc}", file=sys.stderr)
        print("No live call was made. Run with --preview-mode MOCK (zero units).", file=sys.stderr)
        return 2
    host = args.bind
    address = f"{host}:{args.port}"
    server_class = ThreadingWSGIServer if args.threaded else WSGIServer
    httpd = make_server(host, args.port, app, server_class=server_class)
    try:
        print(f"SwapSure demo: {urlunsplit(('http', address, '/', '', ''))}")
        if args.public_host:
            print(f"Public host: {args.public_host} (only this name is accepted in Host)")
        print(f"Data directory: {pathlib.Path(args.data_dir).resolve()}")
        print(f"Concurrency: {'threaded' if args.threaded else 'single request at a time'}")
        print(f"Mode: {args.preview_mode}. " + ("Zero provider units." if args.preview_mode != "LIVE"
                                                 else "LIVE: provider units may be spent."))
        print("Press Ctrl+C to stop.")
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
        service.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
