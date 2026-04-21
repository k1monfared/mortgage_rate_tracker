#!/usr/bin/env python3
"""Dump the current mortgage-rate-tracker subscribers to CSV.

Requires `wrangler` to be authenticated (`npx wrangler login`) — the script
queries the remote D1 database via `wrangler d1 execute`, so the subscriber
list never touches the local filesystem aside from the CSV you write here.

Usage:
    python scripts/dump_subscribers.py                 # prints CSV to stdout
    python scripts/dump_subscribers.py subs.csv        # writes to file

The query joins `subscribers` against `sends` to produce one row per
(email, list) with aggregate send statistics.

Columns:
    email          the subscriber
    list           'ca' | 'us'
    confirmed_at   when they clicked the confirmation link (ISO 8601 UTC)
    total_sends    how many broadcast emails have been delivered to them
    last_sent      the most recent date we sent them a broadcast (or empty)
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_DIR = REPO_ROOT / "subscribe-proxy"
DATABASE = "mortgage-subscribe-audit"

SQL = """
SELECT
  s.email        AS email,
  s.list         AS list,
  s.confirmed_at AS confirmed_at,
  (SELECT COUNT(*) FROM sends WHERE email = s.email AND list = s.list) AS total_sends,
  (SELECT MAX(date_sent) FROM sends WHERE email = s.email AND list = s.list) AS last_sent
FROM subscribers s
ORDER BY s.list, s.confirmed_at DESC
""".strip()

FIELDS = ["email", "list", "confirmed_at", "total_sends", "last_sent"]


def run_wrangler_query() -> list[dict]:
    """Invoke wrangler and return the list of result rows."""
    cmd = [
        "npx", "wrangler", "d1", "execute", DATABASE,
        "--remote", "--json", "--command", SQL,
    ]
    proc = subprocess.run(
        cmd,
        cwd=WORKER_DIR,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(
            f"wrangler d1 execute failed (exit {proc.returncode}). "
            f"Is your CF login fresh? Try: cd {WORKER_DIR} && npx wrangler login"
        )

    # wrangler --json emits a JSON array on stdout. Recent versions shape
    # it as [{"results": [...], "success": true, "meta": {...}}].
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(proc.stdout)
        raise SystemExit("wrangler returned non-JSON output (above).")

    if isinstance(data, list):
        if not data:
            return []
        return data[0].get("results", [])
    return data.get("results", [])


def write_csv(rows: list[dict], fh) -> None:
    writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        # Normalize None → empty string for readability.
        writer.writerow({k: (row.get(k) if row.get(k) is not None else "") for k in FIELDS})


def main() -> int:
    rows = run_wrangler_query()

    if not rows:
        sys.stderr.write("No subscribers yet.\n")

    if len(sys.argv) > 1:
        out_path = Path(sys.argv[1])
        with open(out_path, "w", newline="") as f:
            write_csv(rows, f)
        sys.stderr.write(f"Wrote {len(rows)} rows to {out_path}\n")
    else:
        write_csv(rows, sys.stdout)

    return 0


if __name__ == "__main__":
    sys.exit(main())
