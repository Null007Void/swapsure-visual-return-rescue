"""Phase C offline web experience.

Modules:

* :mod:`ui.webapp` — WSGI routes and the same-origin/CSRF boundary.
* :mod:`ui.views` — server-rendered HTML; every dynamic value is escaped and the
  build contains no script element.
* :mod:`ui.service` — the workflow over the frozen Phase B contract.
* :mod:`ui.store` — SQLite application records (WAL, atomic transactions).
* :mod:`ui.media` — local placeholder media, scoped handles, TTL, deletion ledger.
* :mod:`ui.previews` — bounded asynchronous MOCK create/poll coordination.
* :mod:`ui.uploads` — bounded UTF-8 JSON scenario input.
* :mod:`ui.security` — identifiers, cookies and origin proof.
* :mod:`ui.server` / :mod:`ui.demo` — loopback demo server and reset helpers.

This package never imports a live transport, never opens a socket, and never reads
a credential.  It is a local demo, not a deployment.
"""

__all__: tuple[str, ...] = ()
