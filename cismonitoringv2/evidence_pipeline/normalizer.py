"""Canonical schema + normalization for CIS Docker audit JSON reports.

The legacy audit playbooks emit a JSON document with two top-level keys:

    {
      "meta":    { host, os, generated, total, pass, fail, info, warn?, skip? },
      "results": [ { id, title, section, status, details? }, ... ]
    }

Different sections occasionally use slightly different keys (e.g. some
omit ``warn`` and ``skip`` from ``meta``). This module produces a stable,
fully-populated record shape that downstream consumers (Evidence Store,
HTML report, PDF report) can rely on.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any

CANONICAL_STATUSES = ("PASS", "FAIL", "INFO", "WARN", "SKIP", "ERROR")

_SECTION_PATTERN = re.compile(r"_section([0-9_a-zA-Z]+)\.json$")


def section_from_filename(path: str | Path) -> str:
    name = Path(path).name
    match = _SECTION_PATTERN.search(name)
    return f"section{match.group(1)}" if match else "unknown"


def host_from_filename(path: str | Path) -> str:
    name = Path(path).name
    return _SECTION_PATTERN.sub("", name) or "unknown"


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(raw: dict, *, source_path: str | Path) -> dict:
    """Return a canonical record built from a raw audit report dict."""
    meta = dict(raw.get("meta") or {})
    results_in = list(raw.get("results") or [])

    host = (
        meta.get("host")
        or meta.get("inventory_hostname")
        or host_from_filename(source_path)
    )
    # Prefer the canonical filename-derived identifier ("section1",
    # "section2_3", …). meta.section is often a descriptive label and
    # is kept on the per-check records.
    section = section_from_filename(source_path) or meta.get("section", "unknown")
    generated = _iso(meta.get("generated"))

    counts = {
        "total": _coerce_int(meta.get("total"), len(results_in)),
        "pass":  _coerce_int(meta.get("pass")),
        "fail":  _coerce_int(meta.get("fail")),
        "info":  _coerce_int(meta.get("info")),
        "warn":  _coerce_int(meta.get("warn")),
        "skip":  _coerce_int(meta.get("skip")),
        "error": _coerce_int(meta.get("error")),
    }

    # If the playbook did not provide pre-aggregated counters, derive them
    # from the per-check results so the Evidence Store always carries them.
    derived = {s: 0 for s in CANONICAL_STATUSES}
    for r in results_in:
        derived[str(r.get("status", "ERROR")).upper() if str(r.get("status", "ERROR")).upper() in CANONICAL_STATUSES else "ERROR"] += 1
    for key in ("pass", "fail", "info", "warn", "skip", "error"):
        if counts[key] == 0:
            counts[key] = derived[key.upper()]
    if counts["total"] == 0:
        counts["total"] = sum(derived.values())

    denom = counts["pass"] + counts["fail"]
    compliance = round(100.0 * counts["pass"] / denom, 2) if denom else 0.0

    results: list[dict] = []
    for r in results_in:
        status = str(r.get("status", "ERROR")).upper()
        if status not in CANONICAL_STATUSES:
            status = "ERROR"
        results.append({
            "id":      str(r.get("id", "")).strip(),
            "title":   str(r.get("title", "")).strip(),
            "section": str(r.get("section", "")).strip(),
            "status":  status,
            "details": str(r.get("details", "")).strip(),
        })

    return {
        "host":       str(host),
        "os":         str(meta.get("os", "")),
        "section":    str(section),
        "generated":  generated,
        "source":     str(source_path),
        "counts":     counts,
        "compliance": compliance,
        "results":    results,
    }
