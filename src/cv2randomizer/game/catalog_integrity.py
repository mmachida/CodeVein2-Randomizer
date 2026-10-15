"""Helpers for validating text-based catalog files."""

from __future__ import annotations

import hashlib


# Hash text with normalized newlines
def canonical_text_sha256(payload: bytes) -> str:
    """Return a stable SHA-256 for UTF-8 catalog bytes."""
    normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()
