#!/usr/bin/env python3
"""Evidence Collector – second stage of the data pipeline.

The collector enforces the three audit principles:

  * Chain of Custody  – every ingestion writes a row to ``custody_log``
    and appends a JSON-lines entry to ``store/journal/custody-YYYYMMDD.jsonl``.
    Previously stored versions of the same (host, section, generated)
    tuple are **not** overwritten – they are kept and the column
    ``superseded_by`` is set to the rowid of the new version.
  * Integrity         – the SHA-256 of the raw report file and of the
    canonical-JSON serialisation of the normalised record are persisted.
  * Traceability      – every run and every check carry a UUIDv4 that
    is exposed to downstream consumers (HTML/PDF report, dashboards).

Usage:
    python3 evidence_collector.py \\
        --reports-dir ../../audit/docker_benchmark_reports \\
        --store ./store/evidence.sqlite
"""
from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
from pathlib import Path

# A consumer of our stdout (e.g. `| head`) may close the pipe early.
# Restore the default SIGPIPE handler so Python exits silently instead
# of raising BrokenPipeError mid-transaction and rolling everything back.
try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):
    pass  # not available on every platform (e.g. Windows)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_principles import (
    actor,
    append_journal,
    canonical_json,
    new_uuid,
    now_iso,
    sha256_bytes,
    sha256_file,
)
from normalizer import normalize  # noqa: E402


SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid            TEXT    NOT NULL UNIQUE,
    host            TEXT    NOT NULL,
    os              TEXT,
    section         TEXT    NOT NULL,
    generated       TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    source_sha256   TEXT    NOT NULL,
    record_sha256   TEXT    NOT NULL,
    ingested_at     TEXT    NOT NULL,
    ingested_by     TEXT    NOT NULL,
    superseded_by   INTEGER REFERENCES runs(id),
    total           INTEGER NOT NULL,
    pass_n          INTEGER NOT NULL,
    fail_n          INTEGER NOT NULL,
    info_n          INTEGER NOT NULL,
    warn_n          INTEGER NOT NULL,
    skip_n          INTEGER NOT NULL,
    error_n         INTEGER NOT NULL,
    compliance      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid       TEXT    NOT NULL UNIQUE,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    check_id   TEXT    NOT NULL,
    title      TEXT,
    section    TEXT,
    status     TEXT    NOT NULL,
    details    TEXT
);

CREATE TABLE IF NOT EXISTS custody_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    event      TEXT    NOT NULL,         -- INGESTED / SUPERSEDED / VERIFIED / DUPLICATE
    timestamp  TEXT    NOT NULL,
    actor      TEXT    NOT NULL,
    details    TEXT
);

CREATE TABLE IF NOT EXISTS integrity_checks (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    verified_at             TEXT    NOT NULL,
    actor                   TEXT    NOT NULL,
    source_sha256_at_check  TEXT,
    record_sha256_at_check  TEXT,
    ok                      INTEGER NOT NULL,   -- 1 = match, 0 = tamper / missing
    notes                   TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_host           ON runs(host);
CREATE INDEX IF NOT EXISTS idx_runs_section        ON runs(section);
CREATE INDEX IF NOT EXISTS idx_runs_superseded     ON runs(superseded_by);
CREATE INDEX IF NOT EXISTS idx_runs_uuid           ON runs(uuid);
CREATE INDEX IF NOT EXISTS idx_checks_run          ON checks(run_id);
CREATE INDEX IF NOT EXISTS idx_checks_status       ON checks(status);
CREATE INDEX IF NOT EXISTS idx_custody_run         ON custody_log(run_id);
CREATE INDEX IF NOT EXISTS idx_integrity_run       ON integrity_checks(run_id);
"""


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def open_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Best-effort upgrade of stores created by an earlier collector."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    additions = [
        ("uuid",           "TEXT"),
        ("source_sha256",  "TEXT"),
        ("record_sha256",  "TEXT"),
        ("ingested_at",    "TEXT"),
        ("ingested_by",    "TEXT"),
        ("superseded_by",  "INTEGER REFERENCES runs(id)"),
    ]
    for name, decl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")
    cols_c = {row[1] for row in conn.execute("PRAGMA table_info(checks)")}
    if "uuid" not in cols_c:
        conn.execute("ALTER TABLE checks ADD COLUMN uuid TEXT")
    # Backfill UUID/hashes for legacy rows so downstream readers don't break.
    for row in conn.execute("SELECT id FROM runs WHERE uuid IS NULL").fetchall():
        conn.execute("UPDATE runs SET uuid=? WHERE id=?", (new_uuid(), row[0]))
    for row in conn.execute("SELECT id FROM checks WHERE uuid IS NULL").fetchall():
        conn.execute("UPDATE checks SET uuid=? WHERE id=?", (new_uuid(), row[0]))


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def _existing_run(conn, host, section, generated, source_sha256):
    return conn.execute(
        "SELECT id, source_sha256 FROM runs"
        " WHERE host=? AND section=? AND generated=? AND superseded_by IS NULL",
        (host, section, generated),
    ).fetchone()


def insert_run(conn: sqlite3.Connection, record: dict,
               *, source_sha: str, record_sha: str, who: str, when: str) -> int:
    c = record["counts"]
    run_uuid = new_uuid()
    cur = conn.execute(
        """
        INSERT INTO runs (uuid, host, os, section, generated, source,
                          source_sha256, record_sha256, ingested_at, ingested_by,
                          total, pass_n, fail_n, info_n, warn_n,
                          skip_n, error_n, compliance)
        VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?,?,?,?,?,?)
        """,
        (
            run_uuid, record["host"], record["os"], record["section"],
            record["generated"], record["source"],
            source_sha, record_sha, when, who,
            c["total"], c["pass"], c["fail"], c["info"],
            c["warn"], c["skip"], c["error"], record["compliance"],
        ),
    )
    run_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO checks (uuid, run_id, check_id, title, section, status, details)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            (new_uuid(), run_id, r["id"], r["title"], r["section"],
             r["status"], r["details"])
            for r in record["results"]
        ],
    )
    return run_id


def log_custody(conn: sqlite3.Connection, *, run_id: int | None,
                event: str, who: str, when: str, details: str) -> None:
    conn.execute(
        "INSERT INTO custody_log (run_id, event, timestamp, actor, details)"
        " VALUES (?,?,?,?,?)",
        (run_id, event, when, who, details),
    )


def collect(reports_dir: Path, store_path: Path) -> list[dict]:
    files = sorted(reports_dir.glob("*_section*.json"))
    if not files:
        print(f"[collector] no JSON reports under {reports_dir}")
        return []

    conn = open_store(store_path)
    journal_dir = store_path.parent / "journal"
    who = actor()
    summary: list[dict] = []

    try:
        # Commit per file so a crash mid-batch keeps prior progress
        # (and a broken stdout pipe doesn't lose hours of ingestion).
        for path in files:
            when = now_iso()
            try:
                raw_bytes = path.read_bytes()
                raw = json.loads(raw_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                print(f"[collector] skip {path.name}: {exc}", file=sys.stderr)
                continue

            try:
                # DB writes + journal appends happen atomically inside
                # the `with` block; we only print after the commit so a
                # SIGPIPE during stdout can never drop data.
                with conn:
                    result, log_line = _ingest_one(
                        conn, path, raw_bytes, raw,
                        journal_dir, who, when,
                    )
            except Exception as exc:
                print(f"[collector] error on {path.name}: {exc}",
                      file=sys.stderr)
                continue

            summary.append(result)
            print(log_line)
    finally:
        conn.close()

    print(f"[collector] journal: {journal_dir}/")
    return summary


def _ingest_one(conn, path, raw_bytes, raw, journal_dir, who, when):
    """All DB + journal writes for one report file.

    Returns ``(summary_dict, stdout_line)`` – stdout printing is left to
    the caller so it happens *after* the SQLite commit, preventing
    SIGPIPE-induced inconsistencies between the DB and the journal.
    """
    source_sha = sha256_bytes(raw_bytes)
    record = normalize(raw, source_path=path)
    record_sha = sha256_bytes(canonical_json(record))

    prev = _existing_run(
        conn, record["host"], record["section"],
        record["generated"], source_sha,
    )

    if prev and prev[1] == source_sha:
        log_custody(conn, run_id=prev[0], event="DUPLICATE",
                    who=who, when=when,
                    details=f"re-ingest of {path.name} (sha256={source_sha})")
        append_journal(journal_dir, {
            "event": "DUPLICATE", "timestamp": when, "actor": who,
            "run_id": prev[0], "file": path.name,
            "source_sha256": source_sha,
        })
        return (
            {"file": path.name, "status": "duplicate", "run_id": prev[0]},
            f"[collector] duplicate (skip insert) {path.name}",
        )

    run_id = insert_run(conn, record,
                        source_sha=source_sha, record_sha=record_sha,
                        who=who, when=when)
    run_uuid = conn.execute(
        "SELECT uuid FROM runs WHERE id=?", (run_id,)
    ).fetchone()[0]

    if prev:
        conn.execute("UPDATE runs SET superseded_by=? WHERE id=?",
                     (run_id, prev[0]))
        log_custody(conn, run_id=prev[0], event="SUPERSEDED",
                    who=who, when=when,
                    details=f"replaced by run_id={run_id}")
        # SUPERSEDED must also land in the on-disk journal so the chain
        # of custody is reconstructable without the SQLite file.
        append_journal(journal_dir, {
            "event": "SUPERSEDED", "timestamp": when, "actor": who,
            "run_id": prev[0], "replaced_by_run_id": run_id,
            "replaced_by_run_uuid": run_uuid,
            "host": record["host"], "section": record["section"],
        })

    log_custody(conn, run_id=run_id, event="INGESTED",
                who=who, when=when,
                details=(f"file={path.name} "
                         f"source_sha256={source_sha} "
                         f"record_sha256={record_sha}"))
    append_journal(journal_dir, {
        "event":          "INGESTED",
        "timestamp":      when,
        "actor":          who,
        "run_id":         run_id,
        "run_uuid":       run_uuid,
        "host":           record["host"],
        "section":        record["section"],
        "generated":      record["generated"],
        "file":           path.name,
        "source_sha256":  source_sha,
        "record_sha256":  record_sha,
        "superseded":     prev[0] if prev else None,
    })

    return (
        {"file": path.name, "status": "ingested", "run_id": run_id,
         "source_sha256": source_sha, "record_sha256": record_sha},
        (f"[collector] ingested {record['host']}/{record['section']} "
         f"({record['counts']['pass']}P/{record['counts']['fail']}F "
         f"-> {record['compliance']}%) "
         f"src={source_sha[:12]}…"),
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reports-dir", required=True, type=Path,
                   help="Directory containing *_section*.json reports")
    p.add_argument("--store", required=True, type=Path,
                   help="Path to the SQLite evidence store")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.reports_dir.is_dir():
        print(f"[collector] reports dir not found: {args.reports_dir}",
              file=sys.stderr)
        return 2
    rows = collect(args.reports_dir, args.store)
    ingested = sum(1 for r in rows if r["status"] == "ingested")
    duplicates = sum(1 for r in rows if r["status"] == "duplicate")
    print(f"[collector] {ingested} new, {duplicates} duplicate, "
          f"store={args.store}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
