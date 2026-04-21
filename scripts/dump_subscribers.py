#!/usr/bin/env python3
"""Dump the current mortgage-rate-tracker subscribers to CSV.

Requires `wrangler` to be authenticated (`npx wrangler login`) — the script
queries the remote D1 database via `wrangler d1 execute`, so the subscriber
list never touches the local filesystem aside from the CSV written here.

Default behavior: writes to ~/Downloads/mortgage_subscribers_<UTC-timestamp>.csv.
Override with an explicit path or `-` for stdout.

Usage:
    python scripts/dump_subscribers.py              # → ~/Downloads/mortgage_subscribers_2026-04-21_21-50-12Z.csv
    python scripts/dump_subscribers.py some/file.csv
    python scripts/dump_subscribers.py -            # stdout
    python scripts/dump_subscribers.py --list ca    # only CA subscribers (filename reflects this)
    python scripts/dump_subscribers.py --list us

Columns (one row per (email, list), so the same email appears twice if they
subscribed to both the CA and US lists):

    email          the subscriber
    list           'ca' | 'us'   — which mailing list this row refers to
    list_name      human-readable region name
    confirmed_at   when they clicked the confirmation link (ISO 8601 UTC)
    total_sends    how many broadcast emails have been delivered to them
    last_sent      the most recent date we sent them a broadcast (or empty)
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_DIR = REPO_ROOT / "subscribe-proxy"
DATABASE = "mortgage-subscribe-audit"
DEFAULT_DIR = Path.home() / "Downloads"

LIST_NAMES = {
    "ca": "Canadian Commercial Prime Rate",
    "us": "US Bank Prime Rate",
}

SQL_ALL = """
SELECT
  s.email        AS email,
  s.list         AS list,
  s.confirmed_at AS confirmed_at,
  (SELECT COUNT(*) FROM sends WHERE email = s.email AND list = s.list) AS total_sends,
  (SELECT MAX(date_sent) FROM sends WHERE email = s.email AND list = s.list) AS last_sent
FROM subscribers s
ORDER BY s.list, s.confirmed_at DESC
""".strip()

SQL_ONE = """
SELECT
  s.email        AS email,
  s.list         AS list,
  s.confirmed_at AS confirmed_at,
  (SELECT COUNT(*) FROM sends WHERE email = s.email AND list = s.list) AS total_sends,
  (SELECT MAX(date_sent) FROM sends WHERE email = s.email AND list = s.list) AS last_sent
FROM subscribers s
WHERE s.list = ?
ORDER BY s.confirmed_at DESC
""".strip()

FIELDS = ["email", "list", "list_name", "confirmed_at", "total_sends", "last_sent"]


def run_wrangler_query(list_filter: str | None = None) -> list[dict]:
    """Invoke wrangler and return the list of result rows.

    If list_filter is 'ca' or 'us', only that list's subscribers are returned.
    """
    if list_filter:
        sql = SQL_ONE.replace("?", f"'{list_filter}'")
    else:
        sql = SQL_ALL
    cmd = [
        "npx", "wrangler", "d1", "execute", DATABASE,
        "--remote", "--json", "--command", sql,
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


def enrich(rows: list[dict]) -> list[dict]:
    """Add a human-readable list_name field alongside the 2-letter code."""
    out = []
    for row in rows:
        enriched = dict(row)
        enriched["list_name"] = LIST_NAMES.get(row.get("list", ""), row.get("list", ""))
        out.append(enriched)
    return out


def write_csv(rows: list[dict], fh) -> None:
    writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: (row.get(k) if row.get(k) is not None else "") for k in FIELDS})


def default_output_path(list_filter: str | None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%SZ")
    suffix = f"_{list_filter}" if list_filter else "_all"
    return DEFAULT_DIR / f"mortgage_subscribers{suffix}_{stamp}.csv"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump mortgage rate tracker subscribers to CSV.",
    )
    parser.add_argument(
        "output", nargs="?", default=None,
        help="Output path. Use '-' for stdout. "
             "Default: ~/Downloads/mortgage_subscribers_<scope>_<timestamp>.csv",
    )
    parser.add_argument(
        "--list", dest="list_filter", choices=["ca", "us"], default=None,
        help="Restrict to one mailing list. Default: both.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    rows = enrich(run_wrangler_query(args.list_filter))

    if args.output == "-":
        write_csv(rows, sys.stdout)
        sys.stderr.write(f"({len(rows)} rows)\n")
        return 0

    if args.output:
        out_path = Path(args.output).expanduser()
    else:
        out_path = default_output_path(args.list_filter)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        write_csv(rows, f)

    sys.stderr.write(f"Wrote {len(rows)} rows to {out_path}\n")
    if not rows:
        sys.stderr.write("(No subscribers matched.)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
