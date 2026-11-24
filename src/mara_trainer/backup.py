"""Consistent SQLite snapshots for backups (`coach backup`).

Uses SQLite's online backup API (`sqlite3.Connection.backup`), so the copy is a consistent
point-in-time image even while the Slack bot has the database open, then runs
`PRAGMA integrity_check` on the copy. The source is opened read-only and never created.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class BackupResult:
    path: Path
    bytes: int
    integrity: str

    @property
    def ok(self) -> bool:
        return self.integrity == "ok"

    def to_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "bytes": self.bytes, "integrity": self.integrity}


def target_path(out: Path, now: datetime | None = None) -> Path:
    """`out` itself, or `out/coach-<UTC stamp>.db` when `out` is an existing directory."""
    if out.is_dir():
        stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
        return out / f"coach-{stamp}.db"
    return out


def integrity_check(path: Path) -> str:
    """`ok`, or SQLite's problem list joined with `; `."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check")]
    finally:
        conn.close()
    return "; ".join(rows) if rows else "no result"


def snapshot(source: Path, out: Path) -> BackupResult:
    """Copy `source` to `out` (a file, or a directory for a timestamped name) and check it.

    The copy is written to a temporary file next to the target and renamed into place, so a
    half-written file never carries the final name.
    """
    if not source.is_file():
        raise FileNotFoundError(f"database not found: {source}")
    target = target_path(out)
    if target.resolve() == source.resolve():
        raise ValueError("backup target is the source database")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.partial")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp, target)
    return BackupResult(target, target.stat().st_size, integrity_check(target))
