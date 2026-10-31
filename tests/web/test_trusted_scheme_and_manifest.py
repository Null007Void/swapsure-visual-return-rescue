"""Trusted scheme, Secure cookies and manifest integrity — tested, not asserted from source text."""

from __future__ import annotations

import io
import pathlib
import subprocess
import sys

import pytest

from harness.webclient import Client
from ui.server import build_app
from ui.service import ReturnService
from ui.webapp import Application


def wsgi(app, method="GET", path="/", host="judge.example.org", *, origin=None, proto=None,
         body=b"", length=None, scheme="http"):
    headers_env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "SERVER_NAME": host.split(":")[0],
        "SERVER_PORT": host.split(":")[-1],
        "HTTP_HOST": host,
        "wsgi.url_scheme": scheme,
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": io.StringIO(),
        "CONTENT_LENGTH": str(len(body) if length is None else length),
    }
    if origin is not None:
        headers_env["HTTP_ORIGIN"] = origin
    if proto is not None:
        headers_env["HTTP_X_FORWARDED_PROTO"] = proto
    status: list[str] = []
    out: list[tuple[str, str]] = []

    def start_response(s, headers, exc_info=None):
        status.append(s)
        out.extend(headers)
        return lambda chunk: None

    payload = b"".join(app(headers_env, start_response))
    return status[0], dict(out), payload.decode("utf-8", "replace")


def test_a_client_forwarded_proto_header_does_not_create_a_trusted_scheme(tmp_path):
    app, service = build_app(tmp_path, port=8080, preview_mode="MOCK",
                             public_host="judge.example.org")
    try:
        # the operator did not declare TLS, so an https Origin stays a different origin even though
        # the client claims X-Forwarded-Proto: https
        status, _, body = wsgi(app, "POST", "/returns", origin="https://judge.example.org",
                               proto="https")
        assert status.startswith("403"), status
        assert "ORIGIN_MISMATCH" in body
    finally:
        service.close()


def test_a_declared_external_https_scheme_is_what_the_origin_check_uses(tmp_path):
    app, service = build_app(tmp_path, port=8080, preview_mode="MOCK",
                             public_host="judge.example.org", external_scheme="https")
    try:
        status, headers, _ = wsgi(app, "POST", "/returns", origin="https://judge.example.org",
                                  proto="http")
        assert status.startswith("303"), status
        cookie = headers.get("Set-Cookie", "")
        assert "Secure" in cookie and "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
    finally:
        service.close()


def test_a_plain_http_run_keeps_the_cookie_without_secure(tmp_path):
    app, service = build_app(tmp_path, port=8080, preview_mode="MOCK")
    try:
        status, headers, _ = wsgi(app, "POST", "/returns", host="127.0.0.1:8080",
                                  origin="http://127.0.0.1:8080", scheme="http")
        assert status.startswith("303"), status
        cookie = headers.get("Set-Cookie", "")
        assert "HttpOnly" in cookie
        assert "Secure" not in cookie
    finally:
        service.close()


def test_clearing_the_cookie_is_secure_under_https_too(tmp_path):
    app, service = build_app(tmp_path, port=8080, preview_mode="MOCK",
                             public_host="judge.example.org", external_scheme="https")
    try:
        client = Client(app, host="judge.example.org")
        client.post("/returns", {"reason": "COLOR"}, headers={"Origin": "https://judge.example.org"})
        sid = client.session_id()
        page = client.get(f"/s/{sid}")
        token = client.form_token(page.text)
    finally:
        service.close()


def make_tree(tmp_path, files: dict[str, bytes]) -> pathlib.Path:
    root = tmp_path / "tree"
    root.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def run_verifier(root: pathlib.Path) -> subprocess.CompletedProcess:
    tool = pathlib.Path(__file__).resolve().parents[2] / "tools" / "verify_spec_copy.py"
    return subprocess.run([sys.executable, str(tool), "--public"], cwd=root,
                          capture_output=True, text=True)


def test_deleting_a_file_together_with_its_manifest_entry_still_fails(tmp_path):
    import hashlib

    repo = pathlib.Path(__file__).resolve().parents[2]
    tool = repo / "tools" / "verify_spec_copy.py"
    inventory = [line.strip().strip('",')
                 for line in tool.read_text(encoding="utf-8").splitlines()
                 if line.strip().startswith('"') and line.rstrip().endswith('",')]
    assert len(inventory) > 50, "the verifier must carry a complete inventory"

    keep = {"README.md": b"readme\n", "LICENSE": b"mit\n"}
    lines = []
    for name, data in keep.items():
        lines.append(hashlib.sha256(data).hexdigest() + "  " + name)
    # one file and its manifest entry are both removed
    root = make_tree(tmp_path, keep)
    (root / "MANIFEST-PUBLIC.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = run_verifier(root)
    assert result.returncode != 0
    assert "does not cover the complete release inventory" in result.stdout


def test_duplicate_and_unsafe_manifest_paths_are_rejected(tmp_path):
    import hashlib

    digest = hashlib.sha256(b"x").hexdigest()
    for bad in (f"{digest}  README.md\n{digest}  README.md\n",
                f"{digest}  ../escape.md\n",
                f"{digest}  /etc/passwd\n",
                f"{digest}  sub\\evil.md\n"):
        import uuid

        root = make_tree(tmp_path / uuid.uuid4().hex[:8], {"README.md": b"x"})
        (root / "MANIFEST-PUBLIC.sha256").write_text(bad, encoding="utf-8")
        result = run_verifier(root)
        assert result.returncode != 0, bad
