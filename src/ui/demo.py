"""Demo reset and status helpers for the offline build.

``python -m ui.demo reset --data-dir data`` returns the store to a clean seed:
SQLite rows are deleted, placeholder media and uploaded scenario files are
removed, and the simulator journal is discarded.  It refuses any path that is not
a child of the current working directory, so a mistyped argument cannot delete a
user directory.  No network, no credentials, no provider units.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

from ui.service import ReturnService


def _guard(data_dir: pathlib.Path) -> pathlib.Path:
    resolved = data_dir.expanduser().resolve()
    cwd = pathlib.Path.cwd().resolve()
    if resolved == cwd or cwd not in resolved.parents:
        raise SystemExit(f"refusing to reset {resolved}: outside the working directory")
    if resolved == pathlib.Path(resolved.anchor):
        raise SystemExit("refusing to reset a filesystem root")
    return resolved


def reset(data_dir: pathlib.Path) -> dict[str, int]:
    target = _guard(data_dir)
    removed = {"media": 0, "uploads": 0, "journal": 0, "storeRows": 0}
    if (target / "app.db").exists():
        service = ReturnService(target)
        service.store.reset()
        removed["storeRows"] = 1
        service.close()
    for folder, key in ((target / "media", "media"), (target / "uploads", "uploads")):
        if folder.exists():
            for child in folder.iterdir():
                if child.is_file():
                    child.unlink()
                    removed[key] += 1
    journal = target / "commerce.json"
    if journal.exists():
        journal.unlink()
        removed["journal"] = 1
    return removed


def status(data_dir: pathlib.Path) -> dict[str, object]:
    target = _guard(data_dir)
    if not (target / "app.db").exists():
        return {"dataDir": str(target), "exists": False}
    service = ReturnService(target)
    try:
        return {
            "dataDir": str(target),
            "exists": True,
            "journalMode": service.store.journal_mode(),
            "ledger": service.ledger_totals(),
            "deletionRows": len(service.store.deletion_history()),
            "mediaRows": len(service.store.list_media_all()),
        }
    finally:
        service.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SwapSure offline demo utilities")
    parser.add_argument("action", choices=("reset", "status"))
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args(argv)
    target = pathlib.Path(args.data_dir)
    if args.action == "reset":
        removed = reset(target)
        print(f"reset {target}: {removed}")
        return 0
    import json

    print(json.dumps(status(target), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
