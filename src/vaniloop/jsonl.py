"""Tolerant JSONL reading: an interrupted write must not break a later read or resume."""

from __future__ import annotations

import json
import warnings
from pathlib import Path


def read_jsonl(path: str | Path | None) -> list[dict]:
    """Read a JSONL file. Skip blank, unparseable and non-object lines. Warn once with the skipped count."""
    if path is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    bad = 0
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    if bad:
        warnings.warn(f"{p.name}: skipped {bad} unparseable line(s)", stacklevel=2)
    return rows


def dedupe_by_call_id(rows: list[dict], last_wins: bool = True) -> list[dict]:
    """One row per call_id. Rows without a call_id are dropped. Order follows first appearance."""
    out: dict[str, dict] = {}
    for r in rows:
        cid = r.get("call_id")
        if cid is None:
            continue
        if last_wins or cid not in out:
            out[cid] = r
    return list(out.values())


def append_jsonl(path: str | Path, rows: list[dict]) -> None:
    """Append rows as JSONL. A torn last line (no trailing newline) gets a newline first, so rows never join it."""
    p = Path(path)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    with p.open("a+b") as f:
        f.seek(0, 2)
        if f.tell():
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                f.write(b"\n")
        f.write(data)
