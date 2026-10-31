#!/usr/bin/env python3
"""Verify the byte-preserving copy of the owner-approved handoff package.

This repository carries the Phase A handoff package as its authoritative
specification.  The package itself stays read-only at its original location;
the copies here must be byte-identical.  ``MANIFEST.sha256`` (copied verbatim)
is the oracle.

Checks performed:
  1. every file the repository claims to carry hashes to its MANIFEST value;
  2. only an explicitly allow-listed set of MANIFEST entries is absent;
  3. no unexpected file appears under a copied spec root.

Exit code 0 only when all three hold.  No network, no third-party packages.

Usage:  python3 tools/verify_spec_copy.py [repo_root]
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

# MANIFEST entries intentionally not carried into the working repository.
# Rationale for each is recorded in SPEC_PROVENANCE.md.
EXPECTED_ABSENT = {
    "VERIFICATION.md": "Phase A package verification report; superseded by SWAP-B-REPORT.md evidence",
    "provenance/conversation-export.json": (
        "archived conversation; AGENTS.md forbids copying it into a repository without owner review"
    ),
    "tools/verify_package.py": (
        "package-level verifier; it validates the ZIP/folder pair and is run against the "
        "read-only package itself, not against this repository"
    ),
}

# Package files whose path is occupied by repository-authored content instead of a
# copy.  The copy must NOT be present (a differing hash is required), because the
# package README is the package index, not the build README.
REPLACED_BY_REPO_FILE = {
    "README.md": "repository build README; the package index lives in the read-only package",
}

# Pinned deliberately: a manifest that silently loses entries must fail, not pass quietly.
# 29 at the 2026-10-29 baseline; 33 after the owner-approved re-freeze of 2026-10-30
# (see PROVENANCE-2026-10-30.md); 34 on 2026-10-31 when the provider-category document was
# registered in release/PROVENANCE-REGISTER.md.
EXPECTED_ENTRIES = 34  # strict mode (full private repository)

SPEC_FILES = ("AGENTS.md", "RELEASE-MANIFEST.md")
SPEC_DIRS = ("docs", "contracts", "fixtures", "agents", "provenance")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_manifest(path: pathlib.Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        digest, separator, relative = line.partition(" ")
        relative = relative.lstrip("*").strip()
        if not separator or len(digest) != 64 or not relative:
            raise SystemExit(f"malformed MANIFEST.sha256 line {line_number}: {raw!r}")
        entries[relative] = digest
    return entries


def public_main() -> int:
    """Check the files this public tree ships against MANIFEST-PUBLIC.sha256."""
    import hashlib

    manifest = pathlib.Path("MANIFEST-PUBLIC.sha256")
    if not manifest.is_file():
        print("FAIL missing public manifest")
        return 2
    bad = 0
    checked = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, name = line.split("  ", 1)
        path = pathlib.Path(name)
        if not path.is_file():
            print("MISSING", name)
            bad += 1
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            print("MISMATCH", name)
            bad += 1
        checked += 1
    print(f"public manifest entries checked: {checked}, failures: {bad}")
    print("PASS public specification copies are byte-identical" if bad == 0
          else "FAIL public specification copies differ")
    return 0 if bad == 0 else 1


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--public":
        return public_main()
    root = pathlib.Path(argv[1] if len(argv) > 1 else ".").resolve()
    manifest_path = root / "MANIFEST.sha256"
    if not manifest_path.is_file():
        print(f"FAIL missing manifest: {manifest_path}")
        return 1

    manifest = parse_manifest(manifest_path)
    if len(manifest) != EXPECTED_ENTRIES:
        print(f"FAIL manifest entry count {len(manifest)} != {EXPECTED_ENTRIES}")
        return 1

    failures: list[str] = []
    verified = 0
    absent: list[str] = []
    replaced: list[str] = []

    for relative, expected in sorted(manifest.items()):
        target = root / relative
        if not target.is_file():
            if relative in EXPECTED_ABSENT:
                absent.append(relative)
            elif relative in REPLACED_BY_REPO_FILE:
                failures.append(f"missing repository replacement: {relative}")
            else:
                failures.append(f"missing and not allow-listed: {relative}")
            continue
        if relative in EXPECTED_ABSENT:
            failures.append(f"present although listed as intentionally absent: {relative}")
            continue
        actual = sha256_file(target)
        if relative in REPLACED_BY_REPO_FILE:
            if actual == expected:
                failures.append(f"package copy still present, expected repository-authored file: {relative}")
            else:
                replaced.append(relative)
            continue
        if actual != expected:
            failures.append(f"hash mismatch: {relative} expected {expected} got {actual}")
        else:
            verified += 1

    # No unexpected extra files under the spec roots.
    claimed = set(manifest)
    for name in SPEC_FILES:
        path = root / name
        if path.is_file() and name not in claimed:
            failures.append(f"unexpected spec file: {name}")
    for directory in SPEC_DIRS:
        base = root / directory
        if not base.is_dir():
            failures.append(f"missing spec directory: {directory}")
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative not in claimed:
                failures.append(f"unexpected spec file: {relative}")

    print(f"manifest entries:            {len(manifest)}")
    print(f"byte-identical copies:       {verified}")
    print(f"intentionally absent:        {len(absent)}")
    for relative in absent:
        print(f"  - {relative}: {EXPECTED_ABSENT[relative]}")
    print(f"replaced by repo-authored:   {len(replaced)}")
    for relative in replaced:
        print(f"  - {relative}: {REPLACED_BY_REPO_FILE[relative]}")

    if failures:
        print(f"FAIL failures: {len(failures)}")
        for item in failures:
            print(f"  ! {item}")
        return 1
    print("PASS spec copies are byte-identical to the approved handoff v1.0")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
