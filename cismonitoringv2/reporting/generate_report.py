#!/usr/bin/env python3
"""Generate an HTML (and optionally PDF) report from the Evidence Store.

The report exposes the audit-principle fields stored by the collector:
UUID, source/record SHA-256, ingestion metadata, supersede pointers and
the custody log. Use it as the formal Reporting block of the
Compliance-as-Code pipeline.

PDF generation is optional. If ``--pdf`` is set we try, in order:
    1. ``weasyprint``           (pure Python, recommended)
    2. ``wkhtmltopdf`` binary   (system package)
If neither is available we skip the PDF step but still produce the HTML.

Usage:
    python3 generate_report.py \\
        --store ../evidence_pipeline/store/evidence.sqlite \\
        --output-dir ./output \\
        [--pdf] [--include-superseded]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

STATUS_CLASS = {
    "PASS":  "ok",
    "FAIL":  "bad",
    "INFO":  "info",
    "WARN":  "warn",
    "SKIP":  "skip",
    "ERROR": "err",
}


def _row(conn: sqlite3.Connection, run_id: int) -> dict:
    r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return dict(r) if r else {}


def load_runs(store: Path, *, include_superseded: bool) -> tuple[list[dict], list[dict]]:
    if not store.is_file():
        raise FileNotFoundError(f"evidence store not found: {store}")
    conn = sqlite3.connect(str(store))
    conn.row_factory = sqlite3.Row
    try:
        where = "" if include_superseded else "WHERE superseded_by IS NULL"
        runs_q = conn.execute(
            f"SELECT * FROM runs {where} ORDER BY host, section, generated DESC"
        ).fetchall()
        out: list[dict] = []
        for r in runs_q:
            checks = conn.execute(
                "SELECT uuid, check_id, title, section, status, details"
                " FROM checks WHERE run_id=? ORDER BY check_id",
                (r["id"],),
            ).fetchall()
            integrity = conn.execute(
                "SELECT verified_at, actor, ok, notes"
                " FROM integrity_checks WHERE run_id=?"
                " ORDER BY verified_at DESC LIMIT 1",
                (r["id"],),
            ).fetchone()
            out.append({
                "id": r["id"], "uuid": r["uuid"],
                "host": r["host"], "os": r["os"], "section": r["section"],
                "generated": r["generated"], "source": r["source"],
                "source_sha256": r["source_sha256"] or "",
                "record_sha256": r["record_sha256"] or "",
                "ingested_at":   r["ingested_at"] or "",
                "ingested_by":   r["ingested_by"] or "",
                "superseded_by": r["superseded_by"],
                "counts": {k: r[f"{k}_n"] if f"{k}_n" in r.keys() else r[k]
                           for k in ("total", "pass", "fail", "info",
                                     "warn", "skip", "error")},
                "compliance": r["compliance"],
                "checks": [dict(c) for c in checks],
                "last_integrity": dict(integrity) if integrity else None,
            })

        custody = conn.execute(
            "SELECT cl.timestamp, cl.event, cl.actor, cl.details,"
            "       r.uuid AS run_uuid, r.host, r.section"
            " FROM custody_log cl"
            " LEFT JOIN runs r ON r.id = cl.run_id"
            " ORDER BY cl.timestamp DESC LIMIT 200"
        ).fetchall()
        return out, [dict(c) for c in custody]
    finally:
        conn.close()


def _bar(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    cls = "ok" if pct >= 90 else ("warn" if pct >= 70 else "bad")
    return (f'<div class="bar"><div class="bar-fill {cls}" '
            f'style="width:{pct:.1f}%"></div><span>{pct:.1f}%</span></div>')


def _hash_chip(label: str, value: str) -> str:
    if not value:
        return ""
    return (f'<span class="chip" title="{html.escape(value)}">'
            f'{html.escape(label)}: <code>{html.escape(value[:16])}…</code></span>')


def _integrity_badge(info: dict | None) -> str:
    if not info:
        return '<span class="chip warn">Integrity: not verified</span>'
    cls = "ok" if info.get("ok") else "bad"
    label = "OK" if info.get("ok") else "FAIL"
    title = html.escape(info.get("notes") or "")
    return (f'<span class="chip {cls}" title="{title}">'
            f'Integrity {label} @ {html.escape(info.get("verified_at",""))}</span>')


def render_html(runs: list[dict], custody: list[dict]) -> str:
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    hosts = sorted({r["host"] for r in runs})
    total_runs = len(runs)
    overall = (
        sum(r["counts"]["pass"] for r in runs),
        sum(r["counts"]["fail"] for r in runs),
    )
    overall_pct = (round(100.0 * overall[0] / (overall[0] + overall[1]), 2)
                   if (overall[0] + overall[1]) else 0.0)

    summary_rows = []
    for r in runs:
        c = r["counts"]
        superseded = ('<span class="chip warn">superseded</span>'
                      if r["superseded_by"] else "")
        summary_rows.append(
            "<tr>"
            f"<td>{html.escape(r['host'])}</td>"
            f"<td>{html.escape(r['section'])}</td>"
            f"<td>{html.escape(r['generated'])}</td>"
            f"<td><code>{html.escape((r['uuid'] or '')[:8])}…</code></td>"
            f"<td>{c['total']}</td>"
            f"<td class='ok'>{c['pass']}</td>"
            f"<td class='bad'>{c['fail']}</td>"
            f"<td>{_bar(r['compliance'])}</td>"
            f"<td>{_integrity_badge(r['last_integrity'])}{superseded}</td>"
            "</tr>"
        )

    detail_blocks = []
    for r in runs:
        rows = []
        for chk in r["checks"]:
            cls = STATUS_CLASS.get(chk["status"], "err")
            rows.append(
                "<tr>"
                f"<td><code>{html.escape((chk.get('uuid') or '')[:8])}…</code></td>"
                f"<td>{html.escape(chk['check_id'])}</td>"
                f"<td>{html.escape(chk['section'] or '')}</td>"
                f"<td>{html.escape(chk['title'] or '')}</td>"
                f"<td class='{cls}'>{html.escape(chk['status'])}</td>"
                f"<td class='details'>{html.escape(chk['details'] or '')}</td>"
                "</tr>"
            )
        anchor = (r["uuid"] or f"run-{r['id']}").replace(":", "")
        detail_blocks.append(
            f'<h3 id="{html.escape(anchor)}">'
            f"{html.escape(r['host'])} – {html.escape(r['section'])} "
            f"<small>({html.escape(r['generated'])})</small></h3>"
            '<div class="meta-row">'
            f'{_hash_chip("uuid", r["uuid"] or "")}'
            f'{_hash_chip("source SHA-256", r["source_sha256"])}'
            f'{_hash_chip("record SHA-256", r["record_sha256"])}'
            f'<span class="chip">ingested {html.escape(r["ingested_at"])} '
            f'by {html.escape(r["ingested_by"])}</span>'
            f'{_integrity_badge(r["last_integrity"])}'
            '</div>'
            "<table class='detail'>"
            "<thead><tr><th>Check UUID</th><th>ID</th><th>Sub-section</th>"
            "<th>Title</th><th>Status</th><th>Details</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    custody_rows = []
    for e in custody:
        cls = ("bad" if e["event"] == "INTEGRITY_FAIL"
               else "warn" if e["event"] in ("SUPERSEDED", "DUPLICATE")
               else "ok")
        custody_rows.append(
            "<tr>"
            f"<td>{html.escape(e['timestamp'])}</td>"
            f"<td class='{cls}'>{html.escape(e['event'])}</td>"
            f"<td>{html.escape(e['actor'])}</td>"
            f"<td>{html.escape(e['host'] or '')}/{html.escape(e['section'] or '')}</td>"
            f"<td><code>{html.escape((e.get('run_uuid') or '')[:8])}…</code></td>"
            f"<td class='details'>{html.escape(e['details'] or '')}</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>CIS Docker Compliance Report</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;
       margin:32px;color:#222;background:#fafafa}}
 h1,h2,h3{{color:#1f3a93}}
 small{{color:#666;font-weight:normal}}
 code{{font-family:Menlo,Consolas,monospace;font-size:12px}}
 table{{border-collapse:collapse;width:100%;margin:12px 0;
       background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 th,td{{padding:6px 10px;border-bottom:1px solid #eee;text-align:left;
        vertical-align:top;font-size:13px}}
 th{{background:#f0f2f7;font-weight:600}}
 td.details{{font-family:Menlo,Consolas,monospace;font-size:12px;color:#444;
             max-width:520px;word-break:break-word}}
 .ok{{color:#1a7f37}} .bad{{color:#cf222e;font-weight:600}}
 .info{{color:#0969da}} .warn{{color:#bf8700}}
 .skip{{color:#6e7781}} .err{{color:#82071e;font-weight:600}}
 .bar{{position:relative;background:#e5e7eb;border-radius:4px;
       height:18px;width:140px;overflow:hidden}}
 .bar-fill{{position:absolute;top:0;left:0;bottom:0}}
 .bar-fill.ok{{background:#4ade80}}
 .bar-fill.warn{{background:#fbbf24}}
 .bar-fill.bad{{background:#f87171}}
 .bar span{{position:absolute;inset:0;display:flex;align-items:center;
            justify-content:center;font-size:11px;color:#111}}
 .meta{{display:flex;gap:20px;flex-wrap:wrap;margin:16px 0;
        padding:12px 16px;background:#fff;border-radius:6px;
        box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 .meta div{{font-size:13px}} .meta b{{display:block;color:#1f3a93}}
 .meta-row{{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 12px}}
 .chip{{display:inline-block;padding:2px 8px;border-radius:10px;
        background:#e6e8ef;color:#1f2937;font-size:12px}}
 .chip.ok{{background:#dcfce7;color:#166534}}
 .chip.bad{{background:#fee2e2;color:#991b1b}}
 .chip.warn{{background:#fef3c7;color:#854d0e}}
 @media print{{body{{margin:12mm;background:#fff}}
   table{{box-shadow:none}} .meta{{box-shadow:none}}}}
</style></head><body>
<h1>CIS Docker Benchmark – Compliance Report</h1>
<div class="meta">
  <div><b>Generated</b>{now}</div>
  <div><b>Hosts</b>{len(hosts)}</div>
  <div><b>Audit runs</b>{total_runs}</div>
  <div><b>Overall compliance</b>{overall_pct:.1f}%
       ({overall[0]} pass / {overall[1]} fail)</div>
</div>

<h2>Summary by run</h2>
<table>
  <thead><tr><th>Host</th><th>Section</th><th>Generated</th>
   <th>Run UUID</th><th>Total</th><th>Pass</th><th>Fail</th>
   <th>Compliance</th><th>Status</th></tr></thead>
  <tbody>{''.join(summary_rows) or '<tr><td colspan=9>No data</td></tr>'}</tbody>
</table>

<h2>Per-run detail (UUID · hashes · checks)</h2>
{''.join(detail_blocks) or '<p><em>No detail available.</em></p>'}

<h2>Chain of Custody (latest 200 events)</h2>
<table>
  <thead><tr><th>Timestamp</th><th>Event</th><th>Actor</th>
   <th>Host / Section</th><th>Run UUID</th><th>Details</th></tr></thead>
  <tbody>{''.join(custody_rows) or '<tr><td colspan=6>No custody events</td></tr>'}</tbody>
</table>
</body></html>
"""


def _try_pdf(html_path: Path, pdf_path: Path) -> bool:
    try:
        from weasyprint import HTML  # type: ignore

        HTML(filename=str(html_path)).write_pdf(str(pdf_path))
        return True
    except Exception:
        pass
    wk = shutil.which("wkhtmltopdf")
    if wk:
        res = subprocess.run(
            [wk, "--quiet", str(html_path), str(pdf_path)], check=False,
        )
        return res.returncode == 0
    return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--pdf", action="store_true",
                   help="Also emit a PDF if weasyprint or wkhtmltopdf exists")
    p.add_argument("--include-superseded", action="store_true",
                   help="Include historical (superseded) run versions")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    runs, custody = load_runs(args.store,
                              include_superseded=args.include_superseded)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    html_path = args.output_dir / f"cis_report_{stamp}.html"
    html_path.write_text(render_html(runs, custody), encoding="utf-8")
    print(f"[report] HTML written: {html_path}")

    latest = args.output_dir / "cis_report_latest.html"
    latest.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[report] HTML alias:   {latest}")

    if args.pdf:
        pdf_path = args.output_dir / f"cis_report_{stamp}.pdf"
        if _try_pdf(html_path, pdf_path):
            print(f"[report] PDF written:  {pdf_path}")
        else:
            print("[report] PDF skipped: install 'weasyprint' or 'wkhtmltopdf'",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
