"""Adapters: fixture-backed mock catalog and the YouCam provider boundary.

Phase B ships a MOCK adapter with zero provider units and **no live transport at
all** — the live path is not disabled by a flag, it does not exist (see
``youcam_stub``).  This package deliberately re-exports nothing, so that importing
``adapters.mock.fixtures`` does not pull in the provider stub.
"""

__all__: tuple[str, ...] = ()
