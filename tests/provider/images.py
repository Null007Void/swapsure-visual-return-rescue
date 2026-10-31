"""Genuine image fixtures for the provider tests.

`png()` emits a complete, decodable PNG (real IHDR/IDAT/IEND with correct CRCs and a zlib stream).
`jpeg()` emits a structurally complete JPEG (SOI, SOF0, SOS, entropy data, EOI) — header-only and
truncated variants are built separately so the negative tests are explicit.
"""
from __future__ import annotations

import struct
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) % (2 ** 32))


def png(width: int = 640, height: int = 480, rgb: tuple[int, int, int] = (40, 120, 200)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))


def png_noise(width: int = 900, height: int = 700) -> bytes:
    """Poorly compressible pixels, for the 'bigger than the JSON ceiling' case."""
    rows = []
    seed = 12345
    for _ in range(height):
        row = bytearray(b"\x00")
        for _ in range(width):
            seed = (1103515245 * seed + 12345) % (2 ** 31)
            row += bytes(((seed >> 16) % 256, (seed >> 8) % 256, seed % 256))
        rows.append(bytes(row))
    raw = b"".join(rows)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 0))
            + _chunk(b"IEND", b""))


def png_header_only(width: int = 640, height: int = 480) -> bytes:
    """Signature + IHDR only: the shape the old validator wrongly accepted."""
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))


def png_truncated(width: int = 640, height: int = 480) -> bytes:
    """A real PNG with its IDAT payload cut in half."""
    full = png(width, height)
    return full[: len(full) - 40]


def jpeg(width: int = 640, height: int = 480) -> bytes:
    """Structurally complete JPEG: SOI, SOF0, SOS, entropy bytes, EOI."""
    sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width)
           + b"\x03" + b"\x01\x11\x00" + b"\x02\x11\x01" + b"\x03\x11\x01")
    sos = (b"\xff\xda" + struct.pack(">H", 12) + b"\x03" + b"\x01\x00" + b"\x02\x11"
           + b"\x03\x11" + b"\x00\x3f\x00")
    return b"\xff\xd8" + sof + sos + b"\x00" * 64 + b"\xff\xd9"


def jpeg_header_only(width: int = 640, height: int = 480) -> bytes:
    sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width)
           + b"\x03" + b"\x01\x11\x00" + b"\x02\x11\x01" + b"\x03\x11\x01")
    return b"\xff\xd8" + sof
