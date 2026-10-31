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



# The complete shipped file list. A file deleted together with its manifest entry still fails:
# the verifier knows what must be here, so the manifest cannot shrink itself.
RELEASE_INVENTORY = (
    ".dockerignore",
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "Dockerfile",
    "LICENSE",
    "MANIFEST-PUBLIC.sha256",
    "README.md",
    "RELEASE-MANIFEST.md",
    "THIRD-PARTY-NOTICES.md",
    "assets/catalog/PROVENANCE.json",
    "assets/catalog/references/M01-ASYMMETRIC-KNIT-POLO.png",
    "assets/catalog/references/M01-ASYMMETRIC-KNIT-POLO.rights.json",
    "assets/catalog/references/M02-COLORBLOCK-OVERSHIRT.png",
    "assets/catalog/references/M02-COLORBLOCK-OVERSHIRT.rights.json",
    "assets/catalog/references/M03-TECHNICAL-JACKET.png",
    "assets/catalog/references/M03-TECHNICAL-JACKET.rights.json",
    "assets/catalog/references/M04-TEXTURED-CAMP-SHIRT.png",
    "assets/catalog/references/M04-TEXTURED-CAMP-SHIRT.rights.json",
    "assets/catalog/references/M05-TAILORED-UTILITY-TROUSERS.png",
    "assets/catalog/references/M05-TAILORED-UTILITY-TROUSERS.rights.json",
    "assets/catalog/references/M06-OLIVE-FIELD-JACKET.png",
    "assets/catalog/references/M06-OLIVE-FIELD-JACKET.rights.json",
    "assets/catalog/references/M07-BLUE-DENIM-OVERSHIRT.png",
    "assets/catalog/references/M07-BLUE-DENIM-OVERSHIRT.rights.json",
    "assets/catalog/references/M08-TAN-TRENCH-COAT.png",
    "assets/catalog/references/M08-TAN-TRENCH-COAT.rights.json",
    "assets/catalog/references/M09-RUST-CHECK-FLANNEL.png",
    "assets/catalog/references/M09-RUST-CHECK-FLANNEL.rights.json",
    "assets/catalog/references/M10-BLACK-BOMBER-JACKET.png",
    "assets/catalog/references/M10-BLACK-BOMBER-JACKET.rights.json",
    "assets/catalog/references/W01-TEAL-WRAP-DRESS.png",
    "assets/catalog/references/W01-TEAL-WRAP-DRESS.rights.json",
    "assets/catalog/references/W02-IVORY-LACE-BLOUSE.png",
    "assets/catalog/references/W02-IVORY-LACE-BLOUSE.rights.json",
    "assets/catalog/references/W03-BLACK-PINSTRIPE-BLAZER.png",
    "assets/catalog/references/W03-BLACK-PINSTRIPE-BLAZER.rights.json",
    "assets/catalog/references/W04-RUST-WIDELEG-TROUSERS.png",
    "assets/catalog/references/W04-RUST-WIDELEG-TROUSERS.rights.json",
    "assets/catalog/references/W05-LAVENDER-PLEATED-SKIRT.png",
    "assets/catalog/references/W05-LAVENDER-PLEATED-SKIRT.rights.json",
    "assets/catalog/references/W06-RED-FLORAL-MAXIDRESS.png",
    "assets/catalog/references/W06-RED-FLORAL-MAXIDRESS.rights.json",
    "assets/catalog/references/W07-MUSTARD-CABLE-CARDIGAN.png",
    "assets/catalog/references/W07-MUSTARD-CABLE-CARDIGAN.rights.json",
    "assets/catalog/references/W08-BLUE-GEOMETRIC-SHIRT.png",
    "assets/catalog/references/W08-BLUE-GEOMETRIC-SHIRT.rights.json",
    "assets/catalog/references/W09-OLIVE-UTILITY-JACKET.png",
    "assets/catalog/references/W09-OLIVE-UTILITY-JACKET.rights.json",
    "assets/catalog/references/W10-CORAL-RUFFLED-TOP.png",
    "assets/catalog/references/W10-CORAL-RUFFLED-TOP.rights.json",
    "assets/catalog/thumbs/M01-ASYMMETRIC-KNIT-POLO.png",
    "assets/catalog/thumbs/M02-COLORBLOCK-OVERSHIRT.png",
    "assets/catalog/thumbs/M03-TECHNICAL-JACKET.png",
    "assets/catalog/thumbs/M04-TEXTURED-CAMP-SHIRT.png",
    "assets/catalog/thumbs/M05-TAILORED-UTILITY-TROUSERS.png",
    "assets/catalog/thumbs/M06-OLIVE-FIELD-JACKET.png",
    "assets/catalog/thumbs/M07-BLUE-DENIM-OVERSHIRT.png",
    "assets/catalog/thumbs/M08-TAN-TRENCH-COAT.png",
    "assets/catalog/thumbs/M09-RUST-CHECK-FLANNEL.png",
    "assets/catalog/thumbs/M10-BLACK-BOMBER-JACKET.png",
    "assets/catalog/thumbs/W01-TEAL-WRAP-DRESS.png",
    "assets/catalog/thumbs/W02-IVORY-LACE-BLOUSE.png",
    "assets/catalog/thumbs/W03-BLACK-PINSTRIPE-BLAZER.png",
    "assets/catalog/thumbs/W04-RUST-WIDELEG-TROUSERS.png",
    "assets/catalog/thumbs/W05-LAVENDER-PLEATED-SKIRT.png",
    "assets/catalog/thumbs/W06-RED-FLORAL-MAXIDRESS.png",
    "assets/catalog/thumbs/W07-MUSTARD-CABLE-CARDIGAN.png",
    "assets/catalog/thumbs/W08-BLUE-GEOMETRIC-SHIRT.png",
    "assets/catalog/thumbs/W09-OLIVE-UTILITY-JACKET.png",
    "assets/catalog/thumbs/W10-CORAL-RUFFLED-TOP.png",
    "contracts/authorization-template.json",
    "contracts/eligibility.schema.json",
    "contracts/policy.json",
    "contracts/state-machine.json",
    "deploy/Caddyfile",
    "deploy/start.sh",
    "docs/01-competition-brief.md",
    "docs/02-product-brief.md",
    "docs/03-architecture.md",
    "docs/04-state-machine.md",
    "docs/05-decision-contracts.md",
    "docs/06-youcam-boundary.md",
    "docs/07-privacy-safety.md",
    "docs/08-oss-custom-boundary.md",
    "docs/09-evaluation-plan.md",
    "docs/10-adversarial-matrix.md",
    "docs/11-demo-journey.md",
    "docs/12-engineering-task-tree.md",
    "docs/13-acceptance-criteria.md",
    "docs/14-owner-checklist.md",
    "docs/15-offline-demo-guide.md",
    "docs/CATALOG-PROVIDER-CATEGORIES.md",
    "docs/DEPLOY-GOOGLE-CLOUD.md",
    "docs/DEVPOST-SUBMISSION.md",
    "docs/HOSTED-JUDGING-PLAN.md",
    "docs/RELEASE-NOTES-1.7.12.md",
    "docs/YOUTUBE-METADATA.md",
    "fixtures/catalog.json",
    "fixtures/eligibility-cases.json",
    "fixtures/web-scenario.example.json",
    "pyproject.toml",
    "src/adapters/__init__.py",
    "src/adapters/mock/__init__.py",
    "src/adapters/mock/fixtures.py",
    "src/adapters/youcam_live.py",
    "src/adapters/youcam_stub.py",
    "src/commerce/__init__.py",
    "src/commerce/codec.py",
    "src/commerce/confirmation.py",
    "src/commerce/inventory.py",
    "src/commerce/ledger.py",
    "src/commerce/order.py",
    "src/commerce/quote.py",
    "src/commerce/simulator.py",
    "src/domain/__init__.py",
    "src/domain/catalog.py",
    "src/domain/eligibility.py",
    "src/domain/preview.py",
    "src/domain/ranking.py",
    "src/domain/samples.py",
    "src/domain/state_machine.py",
    "src/specs.py",
    "src/ui/README.md",
    "src/ui/__init__.py",
    "src/ui/demo.py",
    "src/ui/media.py",
    "src/ui/photo.py",
    "src/ui/previews.py",
    "src/ui/references.py",
    "src/ui/security.py",
    "src/ui/server.py",
    "src/ui/service.py",
    "src/ui/store.py",
    "src/ui/uploads.py",
    "src/ui/views.py",
    "src/ui/webapp.py",
    "tests/conftest.py",
    "tests/contracts/test_adapter.py",
    "tests/contracts/test_commit.py",
    "tests/contracts/test_ledger.py",
    "tests/contracts/test_no_network.py",
    "tests/contracts/test_quote.py",
    "tests/contracts/test_race.py",
    "tests/contracts/test_recovery.py",
    "tests/domain/test_catalog.py",
    "tests/domain/test_catalog_samples.py",
    "tests/domain/test_eligibility_fixtures.py",
    "tests/domain/test_eligibility_rules.py",
    "tests/domain/test_ranking.py",
    "tests/domain/test_state_machine.py",
    "tests/e2e/test_journeys.py",
    "tests/harness/__init__.py",
    "tests/harness/clock.py",
    "tests/harness/commerce.py",
    "tests/harness/netblock.py",
    "tests/harness/scenarios.py",
    "tests/harness/schema.py",
    "tests/harness/webclient.py",
    "tests/provider/__init__.py",
    "tests/provider/durable_support.py",
    "tests/provider/images.py",
    "tests/provider/test_category_binding.py",
    "tests/provider/test_consent_clock_awareness.py",
    "tests/provider/test_durable_recovery.py",
    "tests/provider/test_final_status_and_binding.py",
    "tests/provider/test_garment_binding.py",
    "tests/provider/test_jpeg_results.py",
    "tests/provider/test_live_app_journey.py",
    "tests/provider/test_no_image_reasons.py",
    "tests/provider/test_r6_closures.py",
    "tests/provider/test_r7_runtime.py",
    "tests/provider/test_reference_refusal.py",
    "tests/provider/test_result_delivery.py",
    "tests/provider/test_youcam_contract.py",
    "tests/provider/test_youcam_live.py",
    "tests/tools/test_public_manifest.py",
    "tests/web/__init__.py",
    "tests/web/conftest.py",
    "tests/web/helpers.py",
    "tests/web/test_accessibility.py",
    "tests/web/test_catalog_flow.py",
    "tests/web/test_delete_media_message.py",
    "tests/web/test_expired_result_page.py",
    "tests/web/test_hosted_mode.py",
    "tests/web/test_judging_notice.py",
    "tests/web/test_money_guards.py",
    "tests/web/test_previews_and_races.py",
    "tests/web/test_public_live_journey.py",
    "tests/web/test_repeat_and_photo.py",
    "tests/web/test_trust_boundary.py",
    "tests/web/test_trusted_scheme_and_manifest.py",
    "tests/web/test_uploads_and_persistence.py",
    "tests/web/test_web_journeys.py",
    "tools/demo_journey.py",
    "tools/live_fixture_server.py",
    "tools/verify_spec_copy.py",
)

# The required set is defined here, independently of the manifest. A manifest cannot make itself
# complete: it is checked against this list, so an empty or truncated manifest fails.
REQUIRED_PUBLIC = (
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "THIRD-PARTY-NOTICES.md",
    "RELEASE-MANIFEST.md",
    "pyproject.toml",
    "contracts/state-machine.json",
    "fixtures/catalog.json",
    "docs/CATALOG-PROVIDER-CATEGORIES.md",
    "docs/HOSTED-JUDGING-PLAN.md",
    "src/ui/server.py",
    "src/ui/service.py",
    "src/ui/views.py",
    "src/ui/webapp.py",
    "tests/web/test_judging_notice.py",
)

def public_main() -> int:
    """Check the files this public tree ships against MANIFEST-PUBLIC.sha256."""
    import hashlib

    manifest = pathlib.Path("MANIFEST-PUBLIC.sha256")
    if not manifest.is_file():
        print("FAIL missing public manifest")
        return 2
    lines = [line for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        print("FAIL empty public manifest: it covers nothing, so it proves nothing")
        return 1
    covered = {}
    seen_lines = set()
    for line in lines:
        if "  " not in line:
            print("FAIL malformed manifest line:", repr(line[:60]))
            return 1
        digest, name = line.split("  ", 1)
        if line in seen_lines:
            print("FAIL duplicate manifest line:", repr(name[:60]))
            return 1
        seen_lines.add(line)
        if name in covered:
            print("FAIL duplicate manifest path:", name)
            return 1
        if (not name or name.startswith(("/", "\\")) or ".." in name.split("/")
                or "\\" in name or name.startswith("./")):
            print("FAIL unsafe manifest path:", repr(name))
            return 1
        covered[name] = digest
    absent_inventory = [name for name in RELEASE_INVENTORY if name not in covered]
    if absent_inventory:
        print("FAIL manifest does not cover the complete release inventory; missing:",
              ", ".join(absent_inventory[:6]), f"({len(absent_inventory)} file(s))")
        return 1
    absent = [name for name in REQUIRED_PUBLIC if name not in covered]
    if absent:
        print("FAIL manifest is truncated: it omits required files ->", ", ".join(sorted(absent)))
        return 1
    bad = 0
    checked = 0
    for line in lines:
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
