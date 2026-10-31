"""Outbound-network guard.

`docs/09-evaluation-plan.md` requires the offline suite to block outbound network
at the harness and assert zero requests.  This guard replaces the process's socket
entry points with recorders that raise, so a test that *tried* to phone home fails
loudly and the attempt is counted.

It is deliberately installed for every test by ``tests/conftest.py``; the guard's
own self-test nests a second guard so the global counter stays at zero.
"""

from __future__ import annotations

import socket
from types import TracebackType
from typing import Any


class NetworkBlocked(RuntimeError):
    """Raised when any code under test attempts an outbound connection."""


class NetworkGuard:
    """Context manager that makes outbound sockets raise and records attempts."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self._patches: list[tuple[Any, str, Any]] = []

    # -- installation ------------------------------------------------------ #
    def __enter__(self) -> "NetworkGuard":
        self._patch(socket, "create_connection", self._blocked("create_connection"))
        self._patch(socket, "getaddrinfo", self._blocked("getaddrinfo"))
        self._patch(socket.socket, "connect", self._blocked_method("connect"))
        self._patch(socket.socket, "connect_ex", self._blocked_method("connect_ex"))
        self._patch(socket.socket, "sendto", self._blocked_method("sendto"))
        self._patch(socket.socket, "sendall", self._blocked_method("sendall"))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for target, name, original in reversed(self._patches):
            setattr(target, name, original)
        self._patches.clear()

    def _patch(self, target: Any, name: str, replacement: Any) -> None:
        self._patches.append((target, name, getattr(target, name)))
        setattr(target, name, replacement)

    # -- blockers ---------------------------------------------------------- #
    def _blocked(self, label: str):
        def blocker(*args: Any, **kwargs: Any) -> Any:
            self.attempts.append(label)
            raise NetworkBlocked(f"outbound network is blocked in the offline suite ({label})")

        return blocker

    def _blocked_method(self, label: str):
        guard = self

        def blocker(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN001
            guard.attempts.append(f"socket.{label}")
            raise NetworkBlocked(f"outbound network is blocked in the offline suite (socket.{label})")

        return blocker
