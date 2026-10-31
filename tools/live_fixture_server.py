"""Offline LIVE demonstration server — no network, no credential, no provider.

Serves the real application in LIVE mode with a **fixture** provider so the live user interface
(photo upload, consent, provider-shaped previews, same-origin result images) can be exercised in a
browser. It never contacts Perfect Corp and cannot spend a unit.

Seeding uses the application's own public routes over HTTP, not private service methods, so what a
browser then sees is the product's normal path.

    PYTHONPATH=src python tools/live_fixture_server.py --port 8131 --data-dir /tmp/swap-live
"""
from __future__ import annotations

import argparse
import json
import pathlib
import struct
import sys
import threading
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from wsgiref.simple_server import make_server

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from adapters.youcam_live import LiveSettings, Response, Transport, YouCamLivePreviewAdapter  # noqa: E402
from ui.service import ReturnService  # noqa: E402
from ui.webapp import Application  # noqa: E402

HOST = "yce-api-01.makeupar.com"
S3 = "yce-us.s3-accelerate.amazonaws.com"
SKUS = ("NAVY-84", "GREEN-99", "OAT-06", "RUST-11")


def png(width: int = 640, height: int = 480, rgb: tuple[int, int, int] = (40, 120, 200)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


class FixtureProvider(Transport):
    """Answers exactly like the documented API, entirely from local bytes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, *, headers, body=None, timeout):
        self.calls.append((method, url))
        if url.endswith("/file"):
            n = len([c for c in self.calls if c[1].endswith("/file")])
            return Response(200, json.dumps({"data": {"files": [{"file_id": f"f{n}", "requests": [
                {"method": "PUT", "url": f"https://{S3}/u/{n}",
                 "headers": {"Content-Type": "image/png"}}]}]}}).encode())
        if url.startswith(f"https://{S3}/u/"):
            return Response(204, b"")
        if url.endswith("/task/cloth-v4"):
            return Response(200, json.dumps({"data": {"task_id": "fixture-task"}}).encode())
        if url.startswith(f"https://{S3}/out"):
            return Response(200, png(640, 480, (30, 160, 90)))
        return Response(200, json.dumps({"data": {"error": None, "task_status": "success",
                                                  "results": {"url": f"https://{S3}/out.png"}}}).encode())


class Clock:
    def __init__(self) -> None:
        self._now = datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def __call__(self) -> datetime:
        return self._now


def seed(base: str) -> str:
    """Walk the public routes: return -> opt-in -> consent -> photo upload."""
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def post(path: str, form: dict, *, raw: bytes | None = None, ctype: str | None = None):
        data = raw if raw is not None else urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(
            base + path, data=data, method="POST",
            headers={"Content-Type": ctype or "application/x-www-form-urlencoded", "Origin": base})
        return opener.open(req).read()

    post("/returns", {"reason": "COLOR"})
    sid = next(c.value for c in jar if c.name == "swapsure_session")

    def token() -> str:
        text = opener.open(f"{base}/s/{sid}").read().decode()
        marker = 'name="request_token" value="'
        start = text.index(marker) + len(marker)
        return text[start:text.index('"', start)]

    post(f"/s/{sid}/opt-in", {"request_token": token()})
    post(f"/s/{sid}/consent", {"request_token": token(), "imageConsent": "true",
                               "mockAcknowledged": "true"})
    boundary = "----fixture"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"request_token\"\r\n\r\n{token()}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"me.png\"\r\n"
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + png(700, 520) + f"\r\n--{boundary}--\r\n".encode()
    post(f"/s/{sid}/photo", {}, raw=body, ctype=f"multipart/form-data; boundary={boundary}")
    # Start previews through the same public action the page's button uses.
    post(f"/s/{sid}/preview", {"request_token": token()})
    return sid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline LIVE fixture server (never calls the provider)")
    parser.add_argument("--port", type=int, default=8131)
    parser.add_argument("--data-dir", default="data-live-fixture")
    args = parser.parse_args(argv)

    data_dir = pathlib.Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    adapter = YouCamLivePreviewAdapter(
        settings=LiveSettings(api_key="fixture-key", authorization_ref="OWNER-FIXTURE",
                              allowed_media_hosts=frozenset({HOST, S3}), unit_cost=2, unit_cap=8,
                              session_unit_cap=8, ledger_path=str(data_dir / "fixture-units.sqlite3")),
        clock=clock.now, transport=FixtureProvider())
    service = ReturnService(data_dir, clock=clock.now, preview_mode="LIVE", adapter=adapter)

    refs = data_dir / "references"
    refs.mkdir(parents=True, exist_ok=True)
    for sku in SKUS:
        (refs / f"{sku}.png").write_bytes(png(512, 400, (180, 90, 60)))
        (refs / f"{sku}.rights.json").write_text(
            json.dumps({"cleared": True, "rights_ref": f"licensed:{sku}:fixture"}), encoding="utf-8")

    base = f"http://127.0.0.1:{args.port}"
    app = Application(service, allowed_hosts=frozenset({f"127.0.0.1:{args.port}",
                                                       f"localhost:{args.port}"}))
    httpd = make_server("127.0.0.1", args.port, app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    sid = seed(base)
    (data_dir / "session.txt").write_text(f"{base}/s/{sid}", encoding="utf-8")
    print(f"FIXTURE LIVE session: {base}/s/{sid}", flush=True)
    print("Provider: local fixture. Zero units. No outbound connection.", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
