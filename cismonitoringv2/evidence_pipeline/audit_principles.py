"""Shared helpers implementing the three audit principles.

  1. Chain of Custody – every ingestion is timestamped, attributed and
     appended to both a relational ``custody_log`` and a write-once
     JSONL journal on disk.
  2. Integrity & Hashing – SHA-256 of the raw report file *and* of the
     canonical-JSON serialization of the normalized record are stored
     so any later tampering can be detected.
  3. Traceability – every run and every check carry a UUIDv4 that links
     report rows back to the source file and source hash.
"""
from __future__ import annotations

import datetime as _dt
import getpass
import hashlib
import json
import os
import platform
import socket
import uuid
from pathlib import Path
from typing import Any


def now_iso() -> str:
    """Return current UTC time as ISO-8601 with second precision."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def actor() -> str:
    """Best-effort identifier of who/what is performing the action.

    Honours ``CIS_AUDIT_ACTOR`` for CI / scheduled jobs that want to
    pin an explicit identity (e.g. ``ci@github-runner``).
    """
    override = os.environ.get("CIS_AUDIT_ACTOR")
    if override:
        return override
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    host = socket.gethostname() or platform.node() or "unknown"
    return f"{user}@{host}"


def new_uuid() -> str:
    return str(uuid.uuid4())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj: Any) -> bytes:
    """Stable byte representation used for record-level hashing.

    ``sort_keys`` + no insignificant whitespace + UTF-8 ensures the
    same logical record always produces the same digest.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Write-once journal – plain JSON-lines, one file per UTC day
# ---------------------------------------------------------------------------

def journal_path(journal_dir: Path) -> Path:
    journal_dir.mkdir(parents=True, exist_ok=True)
    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")
    return journal_dir / f"custody-{day}.jsonl"


def append_journal(journal_dir: Path, entry: dict) -> Path:
    """Append a single JSON object as a new line.

    The file is opened in append-only mode; existing content is never
    rewritten, which gives a low-tech tamper trail independent of the
    SQLite store.
    """
    path = journal_path(journal_dir)
    line = json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
    return path
