"""Rights-cleared garment references, one per candidate SKU.

A reference is usable only when its rights record says so: `<sku>.png` (or `.jpg`) plus
`<sku>.rights.json` carrying {"cleared": true, "rights_ref": "..."}. Nothing is synthesised — a
missing or uncleared reference refuses the live path instead of sending a substitute image.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from adapters.youcam_live import sniff_image
from adapters.youcam_stub import AdapterError, FailureCode

_SUFFIXES = ((".png", "image/png"),)


class ReferenceCatalog:
    """Looks for `<sku>.png` + `<sku>.rights.json` in the session's own directory first, then in
    the bundled catalog shipped with the product. The binding stays server-side: the browser can
    only name a SKU, never a path."""

    def __init__(self, root: pathlib.Path, *extra_roots: pathlib.Path) -> None:
        self.roots = [pathlib.Path(root), *(pathlib.Path(r) for r in extra_roots)]
        self.root = self.roots[0]

    def thumb(self, sku: str) -> dict[str, object]:
        """The catalogue card image: the downscaled copy when bundled, else the full reference."""
        for root in self.roots:
            candidate = root.parent / "thumbs" / f"{sku}.png"
            if candidate.is_file():
                return {"bytes": candidate.read_bytes(), "content_type": "image/png",
                        "rights_ref": f"thumb:{sku}"}
        return self.load(sku)

    def load(self, sku: str) -> dict[str, Any]:
        safe = sku.replace("/", "_")
        root = next((r for r in self.roots if (r / f"{safe}.png").exists()
                     or (r / f"{safe}.jpg").exists()), None)
        if root is None:
            raise AdapterError(FailureCode.INVALID_INPUT,
                               f"no rights-cleared reference image for candidate {sku}")
        for suffix, content_type in _SUFFIXES:
            image = root / f"{safe}{suffix}"
            if image.exists():
                break
        else:
            raise AdapterError(FailureCode.INVALID_INPUT,
                               f"no rights-cleared reference image for candidate {sku}")
        rights_path = root / f"{safe}.rights.json"
        if not rights_path.exists():
            raise AdapterError(FailureCode.INVALID_INPUT, f"no rights record for candidate {sku}")
        rights = json.loads(rights_path.read_text(encoding="utf-8"))
        if not rights.get("cleared") or not rights.get("rights_ref"):
            raise AdapterError(FailureCode.INVALID_INPUT, f"reference for {sku} is not cleared for use")
        payload = image.read_bytes()
        sniff_image(payload, content_type)
        return {"bytes": payload, "content_type": content_type, "rights_ref": str(rights["rights_ref"])}

    def available(self, sku: str) -> bool:
        try:
            self.load(sku)
            return True
        except (AdapterError, OSError, ValueError):
            return False
