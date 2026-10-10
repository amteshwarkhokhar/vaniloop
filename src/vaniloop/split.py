"""The fix/hold-out split of calls (#106): a patch is written from the fix half and proven on the hold-out half."""

from __future__ import annotations

import hashlib
from typing import Literal


def split_half(call_id: str) -> Literal["fix", "holdout"]:
    """Deterministic half of a call: the parity of the first byte of sha256(call_id). Even is "fix", odd "holdout"."""
    return "holdout" if hashlib.sha256(call_id.encode()).digest()[0] % 2 else "fix"
