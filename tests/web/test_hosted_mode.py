"""Hosted-mode behaviour, tested through the WSGI application rather than by grepping source."""

from __future__ import annotations

import io

import pytest

from harness.webclient import Client
from ui.server import build_app
from ui.service import ReturnService
from ui.webapp import Application


def wsgi_get(app, path: str = "/", host: str = "127.0.0.1:8000", *,
             scheme: str = "http", origin: str | None = None):
    env = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "SERVER_NAME": host.split(":")[0],
        "SERVER_PORT": host.split(":")[-1],
        "HTTP_HOST": host,
        "wsgi.url_scheme": scheme,
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": io.StringIO(),
        "CONTENT_LENGTH": "0",
    }
    if origin is not None:
        env["HTTP_ORIGIN"] = origin
    captured: list[str] = []

    def start_response(status, headers, exc_info=None):
        captured.append(status)
        return lambda chunk: None

    body = b"".join(app(env, start_response))
    return captured[0], body.decode("utf-8", "replace")


def test_the_default_allow_list_is_loopback_only(tmp_path):
    app, service = build_app(tmp_path, port=8000, preview_mode="MOCK")
    try:
        assert app.allowed_hosts == frozenset(
            {"127.0.0.1:8000", "localhost:8000", "[::1]:8000"}
        )
        status, _ = wsgi_get(app, host="127.0.0.1:8000")
        assert status.startswith("200")
    finally:
        service.close()


def test_a_public_host_must_be_named_and_nothing_else_is_accepted(tmp_path):
    app, service = build_app(tmp_path, port=8080, preview_mode="MOCK",
                             public_host="judge.example.org")
    try:
        assert "judge.example.org" in app.allowed_hosts
        assert "judge.example.org:443" in app.allowed_hosts
        assert not any("*" in host for host in app.allowed_hosts)
        ok, _ = wsgi_get(app, host="judge.example.org", scheme="https")
        assert ok.startswith("200")
        bad, _ = wsgi_get(app, host="judge.example.org.evil.test")
        assert not bad.startswith("200")
    finally:
        service.close()


def test_a_foreign_host_header_is_refused_even_on_an_open_path(tmp_path):
    app, service = build_app(tmp_path, port=8000, preview_mode="MOCK")
    try:
        status, body = wsgi_get(app, host="attacker.example")
        assert status.startswith("403"), status
        assert "/catalog/women/thumb" not in body
    finally:
        service.close()


def test_proxy_https_requests_are_served_and_cross_origin_posts_are_refused(tmp_path):
    app, service = build_app(tmp_path, port=8000, preview_mode="MOCK",
                             public_host="judge.example.org")
    try:
        status, _ = wsgi_get(app, host="judge.example.org", scheme="https")
        assert status.startswith("200")
        # a cross-site form post must be refused even when the Host is the real one
        status, _ = wsgi_get(app, path="/returns", host="judge.example.org", scheme="https",
                             origin="https://attacker.example")
        assert not status.startswith("200")
    finally:
        service.close()


def test_the_bind_interface_is_an_explicit_argument_and_defaults_to_loopback(tmp_path, monkeypatch):
    import ui.server as server_module

    seen: dict[str, object] = {}

    class Stopped(Exception):
        pass

    def fake_make_server(host, port, app, server_class=None):
        seen["host"] = host
        seen["port"] = port
        seen["class"] = server_class

        class Server:
            def serve_forever(self_inner):
                raise KeyboardInterrupt

            def server_close(self_inner):
                pass

        return Server()

    monkeypatch.setattr(server_module, "make_server", fake_make_server)
    monkeypatch.chdir(tmp_path)
    assert server_module.main(["--port", "8123", "--data-dir", str(tmp_path / "d")]) == 0
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 8123
    assert seen["class"] is server_module.WSGIServer

    assert server_module.main(["--port", "8124", "--bind", "0.0.0.0",
                               "--threaded", "--data-dir", str(tmp_path / "d2")]) == 0
    assert seen["host"] == "0.0.0.0"
    assert seen["class"] is server_module.ThreadingWSGIServer
