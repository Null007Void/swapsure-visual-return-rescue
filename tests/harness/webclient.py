"""In-process WSGI client for the offline web tests.

No socket is opened: the application is called directly, exactly as a server
would call it, with a browser-shaped header set.  ``FORM_HEADERS`` is the single
place that models the same-origin browser proof, so a guard can never be relaxed
just to keep an old test green.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Iterable, Mapping

from wsgiref.util import setup_testing_defaults

HOST = "127.0.0.1:8000"
ORIGIN = f"http://{HOST}"

FORM_HEADERS = {
    "Host": HOST,
    "Origin": ORIGIN,
    "Sec-Fetch-Site": "same-origin",
}

_HIDDEN = re.compile(
    r'<input[^>]*name="(?P<name>[^"]+)"[^>]*value="(?P<value>[^"]*)"[^>]*>', re.IGNORECASE
)
_HIDDEN_REVERSED = re.compile(
    r'<input[^>]*value="(?P<value>[^"]*)"[^>]*name="(?P<name>[^"]+)"[^>]*>', re.IGNORECASE
)


@dataclass(slots=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(slots=True)
class Client:
    app: Any
    host: str = HOST
    cookies: dict[str, str] = field(default_factory=dict)

    # -- plumbing -------------------------------------------------------- #
    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        form: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content_type: str | None = None,
        send_origin: bool = True,
        follow_redirects: bool = False,
    ) -> Response:
        parsed = urllib.parse.urlsplit(path)
        if form is not None:
            pairs: list[tuple[str, str]] = []
            for key, value in form.items():
                if isinstance(value, (list, tuple)):
                    pairs.extend((key, str(item)) for item in value)
                else:
                    pairs.append((key, str(value)))
            body = urllib.parse.urlencode(pairs).encode("utf-8")
            content_type = content_type or "application/x-www-form-urlencoded"

        environ: dict[str, Any] = {}
        setup_testing_defaults(environ)
        environ.update(
            {
                "REQUEST_METHOD": method.upper(),
                "PATH_INFO": parsed.path or "/",
                "QUERY_STRING": parsed.query,
                "CONTENT_TYPE": content_type or "",
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": BytesIO(body),
                "HTTP_HOST": self.host,
                "SERVER_NAME": self.host.split(":")[0],
                "SERVER_PORT": self.host.split(":")[1] if ":" in self.host else "80",
            }
        )
        if self.cookies:
            environ["HTTP_COOKIE"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        base = dict(FORM_HEADERS)
        base["Host"] = self.host
        if not send_origin:
            base.pop("Origin", None)
        for name, value in (headers or {}).items():
            if value is None:
                base.pop(name, None)
            else:
                base[name] = value
        for name, value in base.items():
            key = "HTTP_" + name.upper().replace("-", "_")
            environ[key] = value

        captured: dict[str, Any] = {}

        def start_response(status: str, response_headers: Iterable[tuple[str, str]]) -> None:
            captured["status"] = status
            captured["headers"] = list(response_headers)

        chunks = self.app(environ, start_response)
        payload = b"".join(chunks)
        status_code = int(captured["status"].split(" ", 1)[0])
        header_map: dict[str, str] = {}
        for name, value in captured["headers"]:
            lower = name.lower()
            if lower == "set-cookie":
                cookie = value.split(";", 1)[0]
                if "=" in cookie:
                    key, val = cookie.split("=", 1)
                    if val:
                        self.cookies[key.strip()] = val
                    else:
                        self.cookies.pop(key.strip(), None)
                header_map.setdefault("set-cookie", value)
            else:
                header_map[lower] = value
        response = Response(status_code, header_map, payload)
        if follow_redirects and 300 <= status_code < 400 and response.header("location"):
            return self.request("GET", response.header("location") or "/")
        return response

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, form: Mapping[str, Any] | None = None, **kwargs: Any) -> Response:
        return self.request("POST", path, form=form, **kwargs)

    # -- HTML helpers ---------------------------------------------------- #
    @staticmethod
    def hidden(text: str, name: str) -> str | None:
        for pattern in (_HIDDEN, _HIDDEN_REVERSED):
            for match in pattern.finditer(text):
                if match.group("name") == name:
                    return _unescape(match.group("value"))
        return None

    def form_token(self, text: str) -> str:
        token = self.hidden(text, "request_token")
        assert token, "page has no request_token hidden field"
        return token

    def approval_nonce(self, text: str) -> str:
        nonce = self.hidden(text, "approval_nonce")
        assert nonce, "page has no approval_nonce hidden field"
        return nonce

    def session_id(self) -> str:
        value = self.cookies.get("swapsure_session")
        assert value, "client has no session cookie"
        return value


def _unescape(value: str) -> str:
    import html

    return html.unescape(value)


def multipart(field_name: str, filename: str, payload: bytes, *, content_type: str = "application/json") -> tuple[bytes, str]:
    boundary = "----swapsure-test-boundary"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            payload,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"
