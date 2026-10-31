"""WSGI application: the offline shopper web workflow.

Framework-free on purpose.  The tree supports the standard library only, and a
plain WSGI callable can be exercised in-process by the test harness with sockets
blocked, so the evidence for "zero network" is the same code path the browser
would run.

Routing table
-------------
GET  /                          start a return (structured reasons, optional notes)
POST /returns                   create the session; server evaluates eligibility
GET  /refund-host               independent merchant refund route (no store access)
GET  /ledger                    local outcome ledger (simulated)
GET  /media/<handle>            session-scoped placeholder bytes
GET  /s/<sid>                   state router; also polls bounded mock previews
GET  /s/<sid>/consent-panel     consent ledger + revocation controls
POST /s/<sid>/opt-in            rescue opt-in (separate from image consent)
POST /s/<sid>/consent           image consent + mock acknowledgement (two controls)
POST /s/<sid>/withdraw          withdraw consent before it is recorded
POST /s/<sid>/revoke            revoke image consent after it was recorded
POST /s/<sid>/delete-media      delete local placeholders only
POST /s/<sid>/candidates        change size / preferences (never relaxes filters)
POST /s/<sid>/preview           create the bounded mock preview plan
POST /s/<sid>/choose            choose a candidate; fresh quote
POST /s/<sid>/requote           mint a successor quote after expiry
POST /s/<sid>/approve           explicit APPROVE_EXCHANGE for the exact quote
POST /s/<sid>/refund            continue refund from any precommit state
POST /s/<sid>/reconcile         resolve an in-doubt commit by its existing id
POST /uploads                   bounded UTF-8 JSON scenario upload (own inputs)
"""

from __future__ import annotations

import pathlib
import sys
import traceback
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from ui import views
from ui.media import MediaError
from ui.security import (
    SESSION_COOKIE,
    clear_session_cookie_header,
    constant_time_eq,
    parse_cookies,
    same_origin,
    session_cookie_header,
    utc_now,
    url_authority_rejected,
)
from ui.views import esc
from ui.service import CaseView, ReturnService, ServiceError, ScenarioInput
from adapters.youcam_stub import MAX_MEDIA_BYTES, AdapterError
from ui.uploads import UploadError, MAX_UPLOAD_BYTES, parse_upload, store_upload

MAX_FORM_BYTES = 64 * 1024
MAX_UPLOAD_REQUEST_BYTES = MAX_UPLOAD_BYTES + 8 * 1024

ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/"),
    ("POST", "/returns"),
    ("GET", "/catalog"),
    ("GET", "/catalog/<collection>"),
    ("GET", "/catalog/<collection>/thumb/<sku>"),
    ("POST", "/catalog/<collection>/select"),
    ("GET", "/refund-host"),
    ("GET", "/ledger"),
    ("POST", "/uploads"),
    ("GET", "/media/<handle>"),
    ("GET", "/s/<session_id>"),
    ("GET", "/s/<session_id>/consent-panel"),
    ("GET", "/s/<session_id>/preview/<candidate>"),
    ("POST", "/s/<session_id>/photo"),
    ("POST", "/s/<session_id>/opt-in"),
    ("POST", "/s/<session_id>/consent"),
    ("POST", "/s/<session_id>/choose-garment"),
    ("POST", "/s/<session_id>/change-garment"),
    ("POST", "/s/<session_id>/again"),
    ("POST", "/s/<session_id>/change-photo"),
    ("POST", "/s/<session_id>/withdraw"),
    ("POST", "/s/<session_id>/revoke"),
    ("POST", "/s/<session_id>/delete-media"),
    ("POST", "/s/<session_id>/candidates"),
    ("POST", "/s/<session_id>/preview"),
    ("POST", "/s/<session_id>/choose"),
    ("POST", "/s/<session_id>/requote"),
    ("POST", "/s/<session_id>/approve"),
    ("POST", "/s/<session_id>/refund"),
    ("POST", "/s/<session_id>/reconcile"),
)

MUTATING_ROUTES: tuple[str, ...] = tuple(path for method, path in ROUTES if method == "POST")

_SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
     "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
    ("X-Frame-Options", "DENY"),
)


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()

    def with_headers(self, extra: Iterable[tuple[str, str]]) -> "Response":
        merged = {name.lower(): (name, value) for name, value in self.headers}
        for name, value in extra:
            merged[name.lower()] = (name, value)
        return Response(self.status, self.body, tuple(merged.values()))


def _html(status: int, body: str) -> Response:
    return Response(
        status,
        body.encode("utf-8"),
        (("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body.encode("utf-8"))))),
    )


def _redirect(location: str, *, cookie: str | None = None) -> Response:
    headers = [("Location", location), ("Content-Length", "0")]
    if cookie:
        headers.append(("Set-Cookie", cookie))
    return Response(303, b"", tuple(headers))


class Application:
    """The whole web surface.  ``service`` is injected so tests can drive it."""

    def __init__(
        self,
        service: ReturnService,
        *,
        allowed_hosts: frozenset[str] = frozenset({"127.0.0.1:8000", "localhost:8000"}),
        enforce_host: bool = False,
    ) -> None:
        self.service = service
        self.allowed_hosts = allowed_hosts
        # A hosted deployment sits behind a proxy that may forward any Host it is given, and a
        # rebound DNS name would otherwise reach the app under an unknown host on read paths too.
        self.enforce_host = enforce_host

    # -- WSGI ------------------------------------------------------------ #
    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        try:
            response = self._dispatch(environ)
        except ServiceError as exc:
            response = _html(
                exc.status,
                views.error_page(
                    code=exc.code,
                    message=exc.message,
                    status=exc.status,
                    details=exc.details,
                    session_id=_path_session(environ),
                    mode=self._mode(),
                ),
            )
        except MediaError as exc:
            response = _html(
                exc.status,
                views.error_page(
                    code=exc.code, message=exc.message, status=exc.status,
                    mode=self._mode(),
                ),
            )
        except UploadError as exc:
            response = _html(
                exc.status,
                views.error_page(
                    code=exc.code,
                    message=exc.message,
                    status=exc.status,
                    details=exc.details,
                    session_id=None,
                    mode=self._mode(),
                ),
            )
        except Exception:  # pragma: no cover - last-resort containment
            traceback.print_exc(file=sys.stderr)
            response = _html(
                500,
                views.error_page(
                    code="INTERNAL_ERROR",
                    message="the request could not be completed",
                    status=500,
                    mode=self._mode(),
                ),
            )
        response = response.with_headers(_SECURITY_HEADERS)
        start_response(
            f"{response.status} {_reason(response.status)}",
            list(response.headers),
        )
        return [response.body]

    # -- dispatch -------------------------------------------------------- #
    def _mode(self) -> str:
        """The configured preview mode, or MOCK if the service cannot answer at all.

        Error handling must still render when the service is broken: a containment page that raises
        would replace the real refusal with an unhandled 500.
        """
        try:
            return str(self.service.preview_mode)
        except Exception:                            # noqa: BLE001 - deliberately total
            return "MOCK"

    def _dispatch(self, environ: Mapping[str, Any]) -> Response:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/")) or "/"
        headers = _headers(environ)
        query = urllib.parse.parse_qs(str(environ.get("QUERY_STRING", "")), keep_blank_values=True)
        cookies = parse_cookies(headers.get("Cookie"))

        if self.enforce_host:
            host = (headers.get("Host") or "").strip().lower()
            if host not in self.allowed_hosts:
                raise ServiceError(
                    "HOST_NOT_ALLOWED",
                    "this deployment answers only on its own host name",
                    status=403,
                )

        scheme = (environ.get("HTTP_X_FORWARDED_PROTO")
                  or environ.get("wsgi.url_scheme") or "http")
        ok, reason = same_origin(method, headers, self.allowed_hosts,
                                 str(scheme).split(",")[0].strip().lower())
        if not ok:
            raise ServiceError(
                "CROSS_ORIGIN_DENIED",
                f"mutation refused without a same-origin proof ({reason})",
                status=403,
            )

        if method == "GET" and path == "/":
            return self._home()
        segments = [part for part in path.strip("/").split("/") if part]
        if method == "GET" and path == "/catalog":
            return _html(200, views.catalog_index_page(mode=self._mode()))
        if method == "GET" and len(segments) == 2 and segments[0] == "catalog":
            collection = segments[1]
            if collection not in ("women", "men"):
                raise ServiceError("UNKNOWN_COLLECTION", "that collection is not bundled", status=404)
            return _html(200, views.catalog_collection_page(collection, mode=self._mode()))
        if method == "GET" and len(segments) == 4 and segments[0] == "catalog" and segments[2] == "thumb":
            collection, sku = segments[1], segments[3]
            if collection not in ("women", "men"):
                raise ServiceError("UNKNOWN_COLLECTION", "that collection is not bundled", status=404)
            from domain.samples import item_for
            try:
                item_for(collection, sku)
            except KeyError:
                raise ServiceError("UNKNOWN_GARMENT", "that garment is not in this collection", status=404)
            reference = self.service.references.thumb(sku)
            return Response(200, reference["bytes"],
                            (("content-type", reference["content_type"]),
                             ("cache-control", "private, max-age=300")))
        if method == "POST" and len(segments) == 3 and segments[0] == "catalog" and segments[2] == "select":
            body = _read_body(environ, MAX_FORM_BYTES)
            form = _parse_form(body, headers)
            sku = (form.get("sku", [""])[0] or "").strip()
            session_id = self.service.start_sample_return(segments[1], sku)
            return _redirect(f"/s/{session_id}", cookie=session_cookie_header(session_id))
        if method == "POST" and path == "/returns":
            return self._create_return(environ, headers)
        if method == "GET" and path == "/refund-host":
            # Deliberately independent: no cookie, no store, no session lookup.
            reference = (query.get("ref") or [None])[0]
            return _html(200, views.refund_host_page(reference=reference))
        if method == "GET" and path == "/ledger":
            return self._ledger()
        if method == "POST" and path == "/uploads":
            return self._upload(environ, headers, cookies)
        if method == "GET" and path.startswith("/media/"):
            return self._media(path.rsplit("/", 1)[-1], cookies)

        if path.startswith("/s/"):
            rest = path[len("/s/"):]
            parts = [part for part in rest.split("/") if part]
            if not parts:
                raise ServiceError("UNKNOWN_SESSION", "no return session in this URL", status=404)
            session_id, *action = parts
            boundary = _bind_session(session_id, cookies)
            if method == "GET" and not action:
                return self._session_page(boundary,
                                          preselect=_query_value(environ, "sku"))
            if method == "GET" and action == ["consent-panel"]:
                return self._consent_panel(boundary)
            if method == "GET" and len(action) == 2 and action[0] == "preview":
                return self._preview_image(boundary, action[1])
            if method == "GET":
                raise ServiceError("UNKNOWN_ROUTE", "no such page", status=404)
            return self._session_action(environ, headers, boundary, "/".join(action))

        raise ServiceError("UNKNOWN_ROUTE", f"no route for {method} {path}", status=404)

    # -- pages ----------------------------------------------------------- #
    def _home(self) -> Response:
        from adapters.mock.fixtures import load_catalog_fixture
        from specs import policy as load_policy

        fixture = load_catalog_fixture()
        options = sorted(
            ((code, status) for code, status in load_policy()["reasonMap"].items()),
            key=lambda pair: (pair[1] != "ELIGIBLE_VISUAL_RESCUE", pair[0]),
        )
        return _html(
            200,
            views.home_page(order=fixture.order.as_dict(), reason_options=options,
                            mode=self.service.preview_mode),
        )

    def _create_return(self, environ: Mapping[str, Any], headers: Mapping[str, str]) -> Response:
        body = _read_body(environ, MAX_FORM_BYTES)
        form = _parse_form(body, headers)
        reasons = form.get("reason", [])
        immediate = any(value.lower() in ("true", "on", "1") for value in form.get("immediateRefund", []))
        notes = (form.get("notes", [""])[0] or "")[:2000]
        upload_handle = (form.get("uploadHandle", [""])[0] or "").strip() or None

        scenario: ScenarioInput | None = None
        if upload_handle:
            scenario = self._load_upload(upload_handle)
            if not reasons:
                reasons = list(scenario.reason_codes)

        try:
            session_id = self.service.create_return(
                reason_codes=reasons,
                immediate_refund=immediate,
                notes=notes,
                scenario=scenario,
            )
        except ServiceError as exc:
            if exc.code == "SKU_CONFLICT" or exc.code == "ORDER_CONFLICT":
                return _html(
                    exc.status,
                    views.home_page(
                        order=_home_order_for(scenario),
                        reason_options=_reason_options(),
                        error=f"{exc.code}: {exc.message}",
                        upload_handle=upload_handle,
                        upload_summary=self._upload_summary(upload_handle),
                    ),
                )
            raise
        return _redirect(f"/s/{session_id}", cookie=session_cookie_header(session_id))

    def _session_page(self, session_id: str, *, preselect: str | None = None) -> Response:
        view = self.service.get_case(session_id)
        if view.state == "PREVIEW_PENDING":
            view = self.service.advance_previews(session_id)
        chosen = self.service.selected_garment(session_id)
        from domain.samples import collection_for

        return _html(200, _render_state(view, mode=self.service.preview_mode,
                                         preselect=preselect, now=self.service.now(),
                                         chosen=chosen, collection=collection_for(chosen or "")))

    def _consent_panel(self, session_id: str) -> Response:
        view = self.service.get_case(session_id)
        return _html(200, views.consent_panel_page(view, mode=self.service.preview_mode))

    def _ledger(self) -> Response:
        return _html(
            200,
            views.ledger_page(
                rows=self.service.store.ledger_rows(),
                totals=self.service.ledger_totals(),
            ),
        )

    def _photo_upload(self, environ: Mapping[str, Any], headers: Mapping[str, str],
                      session_id: str) -> Response:
        """Session-bound, consent-gated upload of the shopper's own photo."""
        raw = str(environ.get("CONTENT_LENGTH") or "0").strip() or "0"
        if not raw.isdigit():
            raise ServiceError("MALFORMED_UPLOAD", "content length must be numeric", status=400)
        length = int(raw)
        if length <= 0 or length > MAX_MEDIA_BYTES + 4096:
            raise ServiceError("UPLOAD_TOO_LARGE", "photo upload is empty or too large", status=400)
        body = environ["wsgi.input"].read(length)
        payload, content_type, fields = _parse_multipart(body, headers.get("Content-Type") or "")
        view = self.service.get_case(session_id)
        token = (fields.get("request_token") or "").strip()
        if not constant_time_eq(token, view.case.request_token):
            raise ServiceError(
                "REQUEST_TOKEN_INVALID",
                "the form token for this return is missing or wrong",
                status=403,
            )
        try:
            self.service.submit_photo(session_id, payload, content_type)
        except AdapterError as exc:
            raise ServiceError(exc.code.value, str(exc), status=400) from exc
        return _redirect(f"/s/{session_id}")

    def _preview_image(self, session_id: str, candidate: str) -> Response:
        payload, content_type = self.service.result_image(session_id, candidate)
        return Response(
            200,
            payload,
            (
                ("Content-Type", content_type),
                ("Content-Security-Policy", "default-src 'none'; img-src 'self'"),
                ("X-Content-Type-Options", "nosniff"),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
            ),
        )

    def _media(self, handle: str, cookies: Mapping[str, str]) -> Response:
        session_id = cookies.get(SESSION_COOKIE)
        if not session_id:
            raise ServiceError("MEDIA_SESSION_DENIED", "no return session in this browser", status=403)
        _, payload = self.service.media.resolve(handle, session_id=session_id)
        return Response(
            200,
            payload,
            (
                ("Content-Type", "image/svg+xml"),
                ("Content-Length", str(len(payload))),
                ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"),
                ("X-Content-Type-Options", "nosniff"),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
            ),
        )

    # -- actions --------------------------------------------------------- #
    def _session_action(
        self,
        environ: Mapping[str, Any],
        headers: Mapping[str, str],
        session_id: str,
        action: str,
    ) -> Response:
        if action == "photo":
            return self._photo_upload(environ, headers, session_id)
        limit = MAX_UPLOAD_REQUEST_BYTES if action == "upload" else MAX_FORM_BYTES
        form = _parse_form(_read_body(environ, limit), headers)
        view = self.service.get_case(session_id)
        token = (form.get("request_token", [""])[0] or "")
        if not constant_time_eq(token, view.case.request_token):
            raise ServiceError(
                "REQUEST_TOKEN_INVALID",
                "the form token for this return is missing or wrong",
                status=403,
            )
        # A URL field is never fetched.  Reject it visibly if a client sends one.
        for name in ("imageUrl", "sourceUrl", "callbackUrl"):
            if name in form:
                why = url_authority_rejected(form[name][0] or "")
                raise ServiceError(
                    "URL_INPUT_REJECTED",
                    f"this form does not accept URLs ({why})",
                    status=400,
                )

        if action == "choose-garment":
            self.service.choose_garment(session_id, (form.get("sku", [""])[0] or "").strip())
            return _redirect(f"/s/{session_id}")
        if action == "change-garment":
            self.service.clear_garment(session_id)
            return _redirect(f"/s/{session_id}")
        if action == "again":
            self.service.try_another(session_id)
            return _redirect(f"/s/{session_id}")
        if action == "change-photo":
            self.service.change_photo(session_id)
            return _redirect(f"/s/{session_id}")
        if action == "opt-in":
            self.service.opt_in(session_id)
            return _redirect(f"/s/{session_id}")
        if action == "consent":
            self.service.record_consent(
                session_id,
                image_consent=_truthy(form, "imageConsent"),
                mock_acknowledged=_truthy(form, "mockAcknowledged"),
            )
            return _redirect(f"/s/{session_id}")
        if action == "withdraw":
            self.service.withdraw_consent(session_id)
            return _redirect(f"/s/{session_id}")
        if action == "revoke":
            # The service owns local deletion (photo, result cache, tombstone) so direct callers
            # behave exactly like this route.
            outcome = self.service.revoke_image_consent(session_id)
            view = self.service.get_case(session_id)
            if view.state == "REFUND_HANDOFF":
                return _redirect("/refund-host")
            return _redirect(f"/s/{session_id}")
        if action == "delete-media":
            outcome = self.service.delete_previews(session_id)
            view = self.service.get_case(session_id)
            deleted = int(outcome.get("localDeleted", 0))
            pending = int(outcome.get("pendingRetry", 0))
            if pending:
                status_line = ("Deletion pending retry: " + str(pending) + " local file(s) could not be "
                               "removed yet, so local deletion is not complete.")
            elif deleted:
                status_line = "Local deletion completed: " + str(deleted) + " local file(s) deleted."
            else:
                status_line = ("Local deletion completed. Nothing in the local placeholder store "
                               "needed deleting.")
            mode = self.service.preview_mode
            provider_note = (
                "Preview images in this mode are generated locally; nothing was sent to a provider."
                if mode != "LIVE" else
                "Your photo and the garment reference were sent to the provider when you generated a "
                "preview, so copies the provider already received are governed by its own retention "
                "terms and are not deleted by this action."
            )
            note = "".join([
                '<div class="status" role="status">',
                "<h2>Local copies deleted</h2>",
                "<p>" + esc(status_line) + "</p>",
                "<p>This deletes what this server held: the photo it stored and any cached result "
                "bytes. The alternatives, the refund route and the commerce record are unaffected.</p>",
                '<p class="muted">Current mode: ' + esc(mode) + ". " + esc(provider_note) + "</p></div>",
            ])
            page = views.consent_panel_page(view, mode=mode).replace(
                "<h1>Consent and image controls</h1>",
                "<h1>Local preview images deleted</h1>" + note,
            )
            return _html(200, page)
        if action == "candidates":
            size_value = (form.get("size", [""])[0] or "").strip()
            self.service.set_preferences(session_id, size=size_value)
            for field_name, key in (
                ("colorFamily", "preferColor"),
                ("style", "preferStyle"),
                ("occasion", "preferOccasion"),
            ):
                value = (form.get(key, [""])[0] or "").strip()
                self.service.set_preferences(
                    session_id,
                    size=size_value,
                    preference_field=field_name,
                    preference_value=value or None,
                )
            return _redirect(f"/s/{session_id}")
        if action == "preview":
            claimed = (form.get("mode", [""])[0] or "").strip().upper()
            if claimed and claimed != self.service.preview_mode:
                # A request cannot talk the server into, or out of, spending provider units.
                raise ServiceError(
                    "PREVIEW_MODE_MISMATCH",
                    f"this server runs in {self.service.preview_mode} mode; the form claimed {claimed}",
                    status=409,
                )
            sku = (form.get("sku", [""])[0] or "").strip() or None
            self.service.start_previews(session_id, sku=sku)
            return _redirect(f"/s/{session_id}")
        if action == "choose":
            self.service.choose(session_id, (form.get("sku", [""])[0] or ""))
            return _redirect(f"/s/{session_id}")
        if action == "requote":
            self.service.requote(session_id)
            return _redirect(f"/s/{session_id}")
        if action == "approve":
            result = self.service.approve(
                session_id, nonce=(form.get("approval_nonce", [""])[0] or "")
            )
            view = self.service.get_case(session_id)
            if result.committed:
                return _redirect(f"/s/{session_id}")
            if view.state == "QUOTE_READY" and view.session.quote is not None:
                return _html(
                    409,
                    views.quote_page(
                        view,
                        view.session.quote,
                        error=f"{result.reason or 'REFUSED'} — nothing was committed.",
                    ),
                )
            raise ServiceError(
                result.reason or "APPROVAL_REFUSED",
                "the exchange was not committed",
                status=409,
            )
        if action == "refund":
            result = self.service.refund(session_id)
            if result.outcome in ("REFUND_HANDOFF_STARTED",):
                return _redirect("/refund-host")
            if result.outcome == "REFUND_AFTER_COMMIT":
                return _redirect(f"/s/{session_id}")
            raise ServiceError(
                result.reason or "REFUND_REFUSED", "the refund route could not be started", status=409
            )
        if action == "reconcile":
            result = self.service.sim.reconcile(session_id)
            self.service._sync()
            return _redirect(f"/s/{session_id}")
        raise ServiceError("UNKNOWN_ROUTE", f"no action {action!r}", status=404)

    # -- uploads --------------------------------------------------------- #
    def _upload(self, environ: Mapping[str, Any], headers: Mapping[str, str], cookies: Mapping[str, str]) -> Response:
        body = _read_body(environ, MAX_UPLOAD_REQUEST_BYTES)
        scenario = parse_upload(body, content_type=headers.get("Content-Type", ""))
        record = store_upload(
            scenario,
            data_dir=self.service.data_dir / "uploads",
            session_id=cookies.get(SESSION_COOKIE),
            now=utc_now(),
        )
        self.service.store.add_upload(record)
        return _html(
            200,
            views.upload_page(
                record=record,
                order=scenario.order.as_dict(),
                rows=scenario.rows,
                reason_codes=scenario.reason_codes,
            ),
        )

    def _load_upload(self, handle: str) -> ScenarioInput:
        record = self.service.store.get_upload(handle)
        if record is None:
            raise ServiceError("UNKNOWN_UPLOAD", "that uploaded scenario is not known", status=404)
        try:
            raw = pathlib.Path(str(record["path"])).read_bytes()
        except OSError:
            raise ServiceError("UPLOAD_UNAVAILABLE", "the uploaded file is no longer present", status=410)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise ServiceError("UPLOAD_TOO_LARGE", "the stored upload is larger than the limit", status=413)
        scenario = parse_upload(raw, content_type="application/json")
        return ScenarioInput(
            order=scenario.order,
            rows=scenario.rows,
            reason_codes=scenario.reason_codes,
            upload_handle=handle,
            sha256=record["sha256"],
            preferences=scenario.preferences,
        )

    def _upload_summary(self, handle: str | None) -> Mapping[str, Any] | None:
        if not handle:
            return None
        record = self.service.store.get_upload(handle)
        if record is None:
            return None
        return {
            "filename": record["filename"],
            "byteSize": record["byte_size"],
            "sha256": record["sha256"],
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _render_state(view: CaseView, mode: str = "MOCK",
                  preselect: str | None = None, now=None,
                  chosen: str | None = None,
                  collection: str | None = None) -> str:
    state = view.state
    if state == "ELIGIBILITY_CHECKED":
        return views.decision_page(view)
    if state == "CONSENT_PENDING":
        return views.consent_page(view, mode=mode)
    if state == "CANDIDATES_READY":
        return views.candidates_page(view, mode=mode, preselect=preselect,
                                     chosen=chosen, collection=collection)
    if state == "PREVIEW_PENDING":
        return views.preview_pending_page(view)
    if state == "DECISION_READY":
        return views.decision_ready_page(view, now=now)
    if state == "QUOTE_READY":
        quote = view.session.quote
        if quote is None:  # pragma: no cover - reducer invariant
            return views.error_page(code="NO_QUOTE", message="quote missing", status=409)
        return views.quote_page(view, quote)
    if state == "EXCHANGE_COMMITTED":
        receipt = view.receipt or view.session.receipt
        if receipt is None:
            return views.error_page(code="NO_RECEIPT", message="receipt missing", status=500)
        return views.receipt_page(view, receipt)
    if state == "REFUND_HANDOFF":
        return views.refund_page(view)
    if state in ("COMMIT_PENDING", "RECONCILING"):
        return views.inflight_page(view)
    return views.error_page(
        code="UNEXPECTED_STATE", message=f"session is in {state}", status=409, session_id=view.session.session_id
    )


def _bind_session(session_id: str, cookies: Mapping[str, str]) -> str:
    cookie = cookies.get(SESSION_COOKIE)
    if not cookie:
        raise ServiceError(
            "UNKNOWN_SESSION", "no return session in this browser", status=404
        )
    if not constant_time_eq(cookie, session_id):
        raise ServiceError(
            "SESSION_DENIED", "this return belongs to another session", status=403
        )
    return cookie


def _query_value(environ: Mapping[str, Any], name: str) -> str | None:
    raw = environ.get("QUERY_STRING") or ""
    if not raw:
        return None
    values = urllib.parse.parse_qs(raw, keep_blank_values=False).get(name) or []
    return values[0] if values else None


def _path_session(environ: Mapping[str, Any]) -> str | None:
    path = str(environ.get("PATH_INFO", ""))
    if path.startswith("/s/"):
        rest = path[len("/s/"):].split("/")
        return rest[0] or None
    return None


def _headers(environ: Mapping[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[len("HTTP_"):].replace("_", "-").title()
            mapping[name] = str(value)
    if environ.get("CONTENT_TYPE"):
        mapping["Content-Type"] = str(environ["CONTENT_TYPE"])
    if environ.get("CONTENT_LENGTH"):
        mapping["Content-Length"] = str(environ["CONTENT_LENGTH"])
    return mapping


def _read_body(environ: Mapping[str, Any], limit: int) -> bytes:
    raw_length = str(environ.get("CONTENT_LENGTH") or "0")
    try:
        length = int(raw_length)
    except ValueError:
        raise ServiceError("MALFORMED_REQUEST", "invalid Content-Length", status=400) from None
    if length < 0:
        raise ServiceError("MALFORMED_REQUEST", "invalid Content-Length", status=400)
    if length > limit:
        raise ServiceError(
            "REQUEST_TOO_LARGE",
            f"request body exceeds the {limit}-byte limit",
            status=413,
        )
    stream = environ.get("wsgi.input")
    if stream is None:
        return b""
    body = stream.read(length) if length else b""
    if isinstance(body, str):  # defensive: a broken server
        body = body.encode("utf-8")
    if len(body) < length:
        raise ServiceError("MALFORMED_REQUEST", "truncated request body", status=400)
    return body


def _parse_multipart(body: bytes, content_type_header: str) -> tuple[bytes, str, dict[str, str]]:
    """Extract the single ``photo`` part of a bounded multipart upload."""
    from email.parser import BytesParser
    from email.policy import default as email_policy

    msg = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + content_type_header.encode("latin-1")
        + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    fields: dict[str, str] = {}
    photo: tuple[bytes, str] | None = None
    for part in msg.walk():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition") or ""
        if name == "photo":
            photo = (part.get_payload(decode=True) or b"", (part.get_content_type() or "").lower())
        elif name:
            raw = part.get_payload(decode=True) or b""
            fields[name] = raw.decode("utf-8", "replace")
    if photo is None:
        raise ServiceError("PHOTO_REQUIRED", "the form must carry a photo part", status=400)
    return photo[0], photo[1], fields


def _parse_form(body: bytes, headers: Mapping[str, str]) -> dict[str, list[str]]:
    content_type = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type not in ("application/x-www-form-urlencoded", ""):
        raise ServiceError(
            "UNSUPPORTED_MEDIA_TYPE",
            "forms must be application/x-www-form-urlencoded",
            status=415,
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ServiceError("MALFORMED_FORM", "form body is not valid UTF-8", status=400) from None
    return urllib.parse.parse_qs(text, keep_blank_values=True)


def _truthy(form: Mapping[str, list[str]], name: str) -> bool:
    return any(value.lower() in ("true", "on", "1") for value in form.get(name, []))


def _reason_options() -> list[tuple[str, str]]:
    from specs import policy as load_policy

    return sorted(
        ((code, status) for code, status in load_policy()["reasonMap"].items()),
        key=lambda pair: (pair[1] != "ELIGIBLE_VISUAL_RESCUE", pair[0]),
    )


def _home_order_for(scenario: ScenarioInput | None) -> Mapping[str, Any]:
    if scenario is not None:
        return scenario.order.as_dict()
    from adapters.mock.fixtures import load_catalog_fixture

    return load_catalog_fixture().order.as_dict()


def _reason(status: int) -> str:
    return {
        200: "OK",
        303: "See Other",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
        409: "Conflict",
        410: "Gone",
        413: "Payload Too Large",
        415: "Unsupported Media Type",
        500: "Internal Server Error",
    }.get(status, "OK")
