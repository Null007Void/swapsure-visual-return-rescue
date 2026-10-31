"""Transport-level trust helpers for the offline web build.

Three rules are enforced here rather than in the handlers:

* **Same-origin mutation.**  Every state-changing request must prove it came from
  the local application page.  A browser-sent ``Origin`` must match the request
  ``Host`` exactly; a request without ``Origin`` (an opaque-origin form post from
  a ``no-referrer`` page) is accepted only when Fetch Metadata reports
  ``Sec-Fetch-Site: same-origin`` or ``none``.  Anything else is refused.
* **Opaque identifiers.**  Session ids, request tokens and approval nonces are
  generated from ``secrets`` and compared in constant time.  The client never
  chooses an identifier that authorises work.
* **No URL authority.**  ``url_authority_rejected`` classifies any caller-supplied
  URL; the application never fetches one.  It reuses the provider boundary's
  SSRF predicate so a private/loopback destination is refused by construction.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from typing import Mapping
from urllib.parse import urlunsplit

from adapters.youcam_stub import is_permitted_media_url

SESSION_COOKIE = "swapsure_session"
COOKIE_MAX_AGE = 60 * 60 * 8

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Fetch Metadata values a same-origin navigation/form post may carry.
_SAME_SITE_VALUES = frozenset({"same-origin", "none"})


def new_id(prefix: str) -> str:
    """A fresh opaque identifier; never derived from shopper input."""

    return f"{prefix}_{secrets.token_hex(16)}"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_eq(left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def parse_cookies(header: str | None) -> dict[str, str]:
    if not header:
        return {}
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(header)
    except Exception:  # malformed Cookie header is simply ignored
        return {}
    return {name: morsel.value for name, morsel in jar.items()}


def session_cookie_header(session_id: str, *, secure: bool = False) -> str:
    """HttpOnly, same-site cookie. Not Secure by default, because the demo is plain HTTP on loopback;
    a deployment that declares an external HTTPS scheme sets ``secure=True``.
    """

    parts = [
        f"{SESSION_COOKIE}={session_id}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={COOKIE_MAX_AGE}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_session_cookie_header() -> str:
    parts = [f"{SESSION_COOKIE}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def same_origin(method: str, headers: Mapping[str, str], allowed_hosts: frozenset[str],
                scheme: str = "http") -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a request's origin proof.

    ``headers`` is a case-insensitive mapping supplied by the web adapter.
    """

    if method.upper() not in MUTATING_METHODS:
        return True, "READ"

    host = (headers.get("Host") or "").strip().lower()
    if not host:
        return False, "MISSING_HOST"
    if host not in allowed_hosts:
        return False, "HOST_NOT_ALLOWED"

    origin = headers.get("Origin")
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()

    if origin is not None:
        # An explicit opaque origin is refused unconditionally, whatever Fetch Metadata claims: the
        # server cannot distinguish a sandboxed or foreign document from a same-site one, so "null" is
        # never treated as an absent header. Browsers are kept out of this state by the response's
        # `Referrer-Policy: same-origin`, which makes a same-origin form post carry its real origin.
        if origin.strip().lower() == "null":
            return False, "OPAQUE_ORIGIN"
        # Behind a TLS proxy the browser sends a secure-scheme origin even though this process
        # speaks plain HTTP; a local run without a proxy sends the plain one. Both are this origin.
        seen = origin.strip().lower()
        # The origin scheme must match the scheme this request actually arrived on. A TLS proxy
        # reports https through wsgi.url_scheme or X-Forwarded-Proto; a plain local run reports http,
        # so a secure-scheme origin on a plain server is still a different origin and stays refused.
        expected = urlunsplit((scheme if scheme in ("http", "https") else "http", host, "", "", ""))
        if not constant_time_eq(seen, expected):
            return False, "ORIGIN_MISMATCH"
        if site and site not in _SAME_SITE_VALUES:
            return False, "CROSS_SITE_FETCH"
        return True, "ORIGIN"

    # No Origin header at all: require Fetch Metadata proof of a same-origin post.
    if site in _SAME_SITE_VALUES:
        return True, "FETCH_METADATA"
    return False, "NO_ORIGIN_PROOF"


def url_authority_rejected(value: str) -> str | None:
    """Why a caller-supplied URL cannot be used, or ``None`` if it is permitted.

    The application never fetches URLs at all; this predicate exists so a route
    that receives one can reject it with a typed reason instead of silently
    ignoring it.
    """

    if not isinstance(value, str) or not value.strip():
        return "EMPTY_URL"
    if is_permitted_media_url(value):
        # Even a permitted media URL is not a shopper input this build fetches.
        return "URL_INPUT_NOT_SUPPORTED_OFFLINE"
    return "DISALLOWED_DESTINATION"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
