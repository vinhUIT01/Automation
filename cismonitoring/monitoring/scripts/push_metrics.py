#!/usr/bin/env python3
"""Convert a CIS Docker audit JSON report to Prometheus exposition format
and push it to a Pushgateway.

Usage:
    push_metrics.py --json <path> --host <host> --section <name> \
                    [--pushgateway http://localhost:9091]

The JSON file is expected to be produced by the audit playbooks in
audit/auditSection*.y*ml and contain `meta` (summary counts) and
`results` (per-check entries with id/title/section/status).

Each (host, section) pair is pushed as its own grouping so concurrent
section pushes do not overwrite each other in the Pushgateway.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATUS_VALUES = {
    "PASS": 1,
    "FAIL": 0,
    "INFO": 2,
    "WARN": 3,
    "SKIP": 4,
    "ERROR": 5,
}

# Prometheus label values must escape backslash, double-quote, newline.
_ESCAPE = str.maketrans({"\\": r"\\", '"': r"\"", "\n": r"\n"})


def escape_label(value: str) -> str:
    return (value or "").translate(_ESCAPE)


def sanitize_grouping(value: str) -> str:
    """Pushgateway grouping path components cannot contain '/'. Replace
    unsafe characters with underscores."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value or "unknown")


def build_metrics(report: dict, host: str, section_label: str) -> str:
    meta = report.get("meta", {}) or {}
    results = report.get("results", []) or []
    lines: list[str] = []

    lines.append("# HELP cis_docker_check Result of a single CIS check (1=PASS, 0=FAIL, 2=INFO, 3=WARN, 4=SKIP, 5=ERROR)")
    lines.append("# TYPE cis_docker_check gauge")
    for r in results:
        check_id = escape_label(str(r.get("id", "")))
        title = escape_label(str(r.get("title", "")))
        sub_section = escape_label(str(r.get("section", "")))
        status = str(r.get("status", "")).upper()
        value = STATUS_VALUES.get(status, 5)
        lines.append(
            f'cis_docker_check{{id="{check_id}",title="{title}",'
            f'sub_section="{sub_section}",status="{status}"}} {value}'
        )

    summary_fields = ("total", "pass", "fail", "info", "warn", "skip")
    lines.append("# HELP cis_docker_summary Aggregate counts per audit run")
    lines.append("# TYPE cis_docker_summary gauge")
    for field in summary_fields:
        if field in meta:
            lines.append(
                f'cis_docker_summary{{outcome="{field}"}} {int(meta[field])}'
            )

    total = int(meta.get("total", 0) or 0)
    passed = int(meta.get("pass", 0) or 0)
    failed = int(meta.get("fail", 0) or 0)
    denom = passed + failed
    if denom > 0:
        compliance = round(100.0 * passed / denom, 2)
        lines.append("# HELP cis_docker_compliance_percent Pass / (Pass + Fail) * 100")
        lines.append("# TYPE cis_docker_compliance_percent gauge")
        lines.append(f"cis_docker_compliance_percent {compliance}")

    lines.append("# HELP cis_docker_last_audit_timestamp_seconds Unix time of the most recent audit push")
    lines.append("# TYPE cis_docker_last_audit_timestamp_seconds gauge")
    lines.append(f"cis_docker_last_audit_timestamp_seconds {int(time.time())}")

    lines.append("")
    return "\n".join(lines)


def push(pushgateway: str, host: str, section: str, body: str) -> None:
    grouping = (
        f"/metrics/job/cis_docker_audit"
        f"/instance/{urllib.parse.quote(sanitize_grouping(host), safe='')}"
        f"/section/{urllib.parse.quote(sanitize_grouping(section), safe='')}"
    )
    url = pushgateway.rstrip("/") + grouping
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "text/plain; version=0.0.4"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Pushgateway returned HTTP {resp.status}")
    print(f"[push_metrics] Pushed {host}/{section} -> {url}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", required=True, help="Path to audit JSON report")
    p.add_argument("--host", required=True, help="Host label / instance value")
    p.add_argument("--section", required=True, help="Section name, e.g. section1")
    p.add_argument(
        "--pushgateway",
        default="http://localhost:9091",
        help="Pushgateway base URL (default: http://localhost:9091)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print metrics instead of pushing",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    path = Path(args.json)
    if not path.is_file():
        print(f"[push_metrics] JSON not found: {path}", file=sys.stderr)
        return 2
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[push_metrics] Invalid JSON {path}: {exc}", file=sys.stderr)
        return 2

    body = build_metrics(report, args.host, args.section)
    if args.dry_run:
        print(body)
        return 0

    try:
        push(args.pushgateway, args.host, args.section, body)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"[push_metrics] Push failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
