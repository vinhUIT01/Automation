#!/usr/bin/env python3
"""Integrity verifier – enforces the "Integrity & Hashing" principle.

For every active run in the Evidence Store the verifier re-hashes:

  * the original source file on disk (``source_sha256``)
  * the canonical-JSON representation rebuilt from the SQLite rows
    (``record_sha256``)

Both values are compared against the hashes captured at ingestion.
Each verification produces a row in ``integrity_checks`` and a custody
log entry. Exit code is non-zero if any tamper / missing-file event is
detected.

Usage:
    python3 verify_evidence.py --store ./store/evidence.sqlite
"""
from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
from pathlib import Path

# See evidence_collector for rationale – SIGPIPE during stdout writes
# must terminate without rolling back partially completed work.
try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_principles import (
    actor,
    append_journal,
    canonical_json,
    now_iso,
    sha256_bytes,
    sha256_file,
)


def _record_from_db(conn: sqlite3.Connection, run_id: int) -> dict:
    """Rebuild the canonical normalized record for hashing."""
    r = conn.execute(
        "SELECT host, os, section, generated, source,"
        "       total, pass_n, fail_n, info_n, warn_n,"
        "       skip_n, error_n, compliance"
        " FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    checks = conn.execute(
        "SELECT check_id, title, section, status, details"
        " FROM checks WHERE run_id=? ORDER BY id",
        (run_id,),
    ).fetchall()
    return {
        "host":       r[0],
        "os":         r[1] or "",
        "section":    r[2],
        "generated":  r[3],
        "source":     r[4],
        "counts": {
            "total": r[5], "pass": r[6], "fail": r[7], "info": r[8],
            "warn": r[9], "skip": r[10], "error": r[11],
        },
        "compliance": r[12],
        "results": [
            {"id": c[0], "title": c[1] or "", "section": c[2] or "",
             "status": c[3], "details": c[4] or ""}
            for c in checks
        ],
    }


def verify(store_path: Path) -> int:
    if not store_path.is_file():
        print(f"[verify] store not found: {store_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(store_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    journal_dir = store_path.parent / "journal"
    who = actor()

    runs = conn.execute(
        "SELECT id, uuid, host, section, generated, source,"
        "       source_sha256, record_sha256"
        " FROM runs WHERE superseded_by IS NULL"
        " ORDER BY host, section"
    ).fetchall()

    if not runs:
        print("[verify] no active runs to verify")
        conn.close()
        return 0

    total = ok = mismatch = missing = 0
    try:
        for r in runs:
            total += 1
            when = now_iso()
            src_path = Path(r["source"])
            notes: list[str] = []
            src_hash_now: str | None = None

            if src_path.is_file():
                src_hash_now = sha256_file(src_path)
                if src_hash_now != r["source_sha256"]:
                    notes.append(
                        f"source mismatch (stored={r['source_sha256'][:12]}, "
                        f"now={src_hash_now[:12]})"
                    )
            else:
                notes.append(f"source file missing on disk: {src_path}")

            rec_hash_now = sha256_bytes(canonical_json(_record_from_db(conn, r["id"])))
            if rec_hash_now != r["record_sha256"]:
                notes.append(
                    f"record mismatch (stored={r['record_sha256'][:12]}, "
                    f"now={rec_hash_now[:12]})"
                )

            is_ok = not notes
            if is_ok:
                ok += 1
            elif any("missing" in n for n in notes):
                missing += 1
            else:
                mismatch += 1

            event = "VERIFIED" if is_ok else "INTEGRITY_FAIL"
            # Commit per-run so a SIGPIPE during stdout never leaves the
            # DB and the on-disk journal out of sync.
            with conn:
                conn.execute(
                    "INSERT INTO integrity_checks "
                    "(run_id, verified_at, actor,"
                    " source_sha256_at_check, record_sha256_at_check, ok, notes)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (r["id"], when, who, src_hash_now, rec_hash_now,
                     1 if is_ok else 0, "; ".join(notes) or None),
                )
                conn.execute(
                    "INSERT INTO custody_log "
                    "(run_id, event, timestamp, actor, details)"
                    " VALUES (?,?,?,?,?)",
                    (r["id"], event, when, who,
                     "; ".join(notes) if notes else "all hashes match"),
                )
                append_journal(journal_dir, {
                    "event":      event,
                    "timestamp":  when,
                    "actor":      who,
                    "run_id":     r["id"],
                    "run_uuid":   r["uuid"],
                    "host":       r["host"],
                    "section":    r["section"],
                    "source_sha256_at_check": src_hash_now,
                    "record_sha256_at_check": rec_hash_now,
                    "ok":         is_ok,
                    "notes":      notes,
                })

            tag = "OK" if is_ok else "FAIL"
            print(f"[verify] {tag} {r['host']}/{r['section']} "
                  f"uuid={r['uuid']}  "
                  + ("; ".join(notes) if notes else ""))
    finally:
        conn.close()

    print(f"[verify] total={total} ok={ok} mismatch={mismatch} missing={missing}")
    return 0 if (mismatch == 0 and missing == 0) else 1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", required=True, type=Path)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    return verify(args.store)


if __name__ == "__main__":
    sys.exit(main())
