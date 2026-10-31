"""Zero-network proof.

`docs/09-evaluation-plan.md` requires the offline suite to block outbound network
at the harness and assert zero requests.  Two independent checks:

1. a socket guard installed for the whole suite (``tests/conftest.py``) that fails
   the run if any test attempts a connection — proven live by a positive control;
2. a static audit that the product source contains no network client at all, so
   the guard is not merely catching luck.

The policy contract is also asserted here: zero live calls, zero live units.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import socket

import pytest

from harness.netblock import NetworkBlocked, NetworkGuard
from specs import policy as load_policy

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

# Dotted module prefixes that can open an outbound connection, or shell out to
# something that can.  `urllib.parse` is deliberately not listed: it is pure string
# parsing with no transport, which is why the audit matches whole prefixes rather
# than the bare package name.  `urllib.request` and `http.client` are listed.
FORBIDDEN_MODULES = (
    "socket",
    "ssl",
    "http",
    "urllib.request",
    "urllib.error",
    "urllib3",
    "requests",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc",
    "websocket",
    "websockets",
    "boto3",
    "botocore",
    "grpc",
    "subprocess",
    "pty",
    "asyncio",
)


def python_files() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module)
    return modules


def is_forbidden(module: str) -> bool:
    # `http.cookies` and `urllib.parse` are pure parsers with no transport: the
    # cookiejar/querystring work the Phase C web layer needs.  The rest of `http`
    # (client, server) stays forbidden, and this allow-list is asserted below.
    if module in PURE_PARSER_MODULES:
        return False
    return any(module == prefix or module.startswith(prefix + ".") for prefix in FORBIDDEN_MODULES)


PURE_PARSER_MODULES = frozenset({"http.cookies", "urllib.parse"})

# The only URL-shaped literal allowed in product source is the SVG XML namespace,
# which is an identifier, not a fetchable destination.
ALLOWED_URL_LITERALS = ("http://www.w3.org/",)

# Phase D: exactly one source file may speak to the provider. The exception is named, singular and
# asserted below; MOCK mode must remain unable to reach it, which test_mode_switch_* in
# tests/provider/test_provider_offline_boundary.py proves.
LIVE_TRANSPORT_MODULE = "adapters/youcam_live.py"
LIVE_TRANSPORT_IMPORTS = frozenset({
    "urllib", "urllib.request", "urllib.error", "urllib.parse",
    "socket", "ssl", "http", "http.client",
})
# Third-party clients stay banned even in the transport module: the provider path uses stdlib only.
BANNED_ANYWHERE = frozenset({"requests", "aiohttp", "httpx", "urllib3", "httplib2"})


def url_literals(text: str) -> list[str]:
    import re

    return [
        token
        for token in re.findall(r"(?:https?|wss?)://[^\s\"'<>)]+", text)
        if not token.startswith(ALLOWED_URL_LITERALS)
    ]


# --------------------------------------------------------------------------- #
# The guard actually works
# --------------------------------------------------------------------------- #
def test_network_guard_blocks_and_counts_attempts():
    guard = NetworkGuard()
    with guard:
        with pytest.raises(NetworkBlocked):
            socket.create_connection(("example.com", 443), timeout=0.05)
        probe = socket.socket()
        try:
            with pytest.raises(NetworkBlocked):
                probe.connect(("93.184.216.34", 80))
        finally:
            probe.close()
        with pytest.raises(NetworkBlocked):
            socket.getaddrinfo("example.com", 443)
    assert guard.attempts == ["create_connection", "socket.connect", "getaddrinfo"]


def test_guard_is_restored_after_use():
    original = socket.create_connection
    with NetworkGuard():
        pass
    assert socket.create_connection is original


# --------------------------------------------------------------------------- #
# Static audit of the product source
# --------------------------------------------------------------------------- #
def test_src_has_python_files():
    assert len(python_files()) >= 10


def test_no_forbidden_imports_in_product_source():
    offenders = {}
    for path in python_files():
        forbidden = sorted(module for module in imported_modules(path) if is_forbidden(module))
        if path.relative_to(SRC).as_posix() == LIVE_TRANSPORT_MODULE:
            allowed = LIVE_TRANSPORT_IMPORTS
            forbidden = [m for m in forbidden if m not in allowed]
            # the transport may use stdlib sockets, but never a third-party client
            assert not (set(imported_modules(path)) & BANNED_ANYWHERE), path
        if forbidden:
            offenders[path.relative_to(SRC).as_posix()] = forbidden
    assert not offenders, offenders


def test_no_url_literals_in_product_source():
    offenders = {}
    for path in python_files():
        if path.relative_to(SRC).as_posix() == LIVE_TRANSPORT_MODULE:
            continue  # documented provider endpoints live here and nowhere else
        text = path.read_text(encoding="utf-8")
        hits = url_literals(text)
        if hits:
            offenders[path.relative_to(SRC).as_posix()] = hits
    assert not offenders, offenders
    # The single allowed literal must be the SVG namespace exactly once, so a
    # second URL cannot be hidden behind the same prefix.
    media = (SRC / "ui" / "media.py").read_text(encoding="utf-8")
    assert media.count('xmlns="http://www.w3.org/2000/svg"') == 1


def test_every_product_module_imports_cleanly():
    modules = []
    for path in python_files():
        relative = path.relative_to(SRC).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        modules.append(".".join(parts))
    assert modules
    for name in modules:
        importlib.import_module(name)


# --------------------------------------------------------------------------- #
# Policy contract: no live anything
# --------------------------------------------------------------------------- #
def test_policy_forbids_live_calls_and_units():
    policy = load_policy()
    assert policy["mode"] == "MOCK"
    assert policy["liveApiAllowed"] is False
    assert policy["liveCallCap"] == 0
    assert policy["liveUnitCap"] == 0
    assert policy["secretsAllowed"] is False
    assert policy["deploymentAllowed"] is False
    assert policy["submissionAllowed"] is False
    assert policy["permanentInfrastructureAllowed"] is False


def test_adapter_exposes_no_transport_configuration():
    from adapters import youcam_stub

    for name in ("BASE_URL", "ENDPOINT", "API_URL", "AUTH_TOKEN", "API_KEY"):
        assert not hasattr(youcam_stub, name)
    assert youcam_stub.live_transport_available() is False


def test_ui_package_is_offline_only_with_declared_routes():
    """Phase C replaces the Phase B "no screens" gate with real invariants.

    The point of the Phase B assertion was that nothing in ``src/ui`` could talk to
    a network or a provider.  That property is now asserted directly: fixed module
    set, declared routes, every mutation declared POST, no live transport, and a
    loopback-only demo server.
    """

    ui = SRC / "ui"
    contents = {path.name for path in ui.glob("*.py")}
    assert contents == {
        "__init__.py",
        "demo.py",
        "media.py",
        "photo.py",
        "previews.py",
        "references.py",
        "security.py",
        "server.py",
        "service.py",
        "store.py",
        "uploads.py",
        "views.py",
        "webapp.py",
    }
    from ui import webapp

    assert webapp.ROUTES
    assert webapp.MUTATING_ROUTES
    assert all(path.startswith("/") for _method, path in webapp.ROUTES)
    assert set(webapp.MUTATING_ROUTES) <= {path for method, path in webapp.ROUTES if method == "POST"}

    # No live transport is importable from the UI. The demo server still defaults to loopback; a
    # hosted deployment must name a bind interface and a public host explicitly, never by default.
    server_source = (ui / "server.py").read_text(encoding="utf-8")
    assert 'default=os.environ.get("SWAPSURE_BIND", "127.0.0.1")' in server_source
    assert 'public_host: str | None = None' in server_source
    assert 'make_server(host, args.port, app, server_class=server_class)' in server_source
    assert "0.0.0.0" not in server_source
    for path in ui.glob("*.py"):
        imports = imported_modules(path)
        assert not any(name.startswith(("adapters.youcam_stub.live", "requests", "httpx")) for name in imports)
