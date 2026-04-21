#!/usr/bin/env python3
"""Dump subscribers across every configured subscribe-proxy project.

This script is project-agnostic: it reads a single config file at
~/.config/subscribe_dump/projects.toml that lists each subscribe-proxy D1
database you own, and can pull from one, a subset, or all of them.

The config is auto-created on first run with this repo's project pre-filled.
Add more [[projects]] blocks as you build new trackers backed by the same
subscribe-proxy pattern (KV + D1 with `subscribers` and `sends` tables).

The subscriber list is never cached anywhere — each run reads D1 directly
via `npx wrangler d1 execute --remote --json`, and writes to a single CSV.

Default output: ~/Downloads/subscribers_<scope>_<utc-timestamp>.csv

Usage:
    python scripts/dump_subscribers.py
        # all projects, all lists

    python scripts/dump_subscribers.py --project mortgage_rate_tracker
        # only one project, all its lists

    python scripts/dump_subscribers.py --project mortgage_rate_tracker --list ca
        # one list within one project

    python scripts/dump_subscribers.py --stats
        # aggregate counts only, no email addresses

    python scripts/dump_subscribers.py --output -
        # CSV to stdout

Tip: symlink into your PATH so you can run it from anywhere:
    ln -s ~/public/mortgage_rate_tracker/scripts/dump_subscribers.py \
        ~/bin/subscribe-dump

CSV columns (when spanning multiple projects):
    project        the [[projects]].name from the config
    email          the subscriber (omitted under --stats)
    list           the raw list code ('ca', 'us', 'free', etc.)
    list_name      human-readable label from config, falls back to the code
    confirmed_at   ISO 8601 UTC timestamp of the confirmation click
    total_sends    how many broadcasts this subscriber has received
    last_sent      date of the most recent broadcast (empty if never)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        sys.stderr.write(
            "This script needs tomllib (Python 3.11+) or `pip install tomli`.\n"
        )
        sys.exit(2)

DEFAULT_DIR = Path.home() / "Downloads"
CONFIG_PATH = Path.home() / ".config" / "subscribe_dump" / "projects.toml"

DEFAULT_CONFIG_TEMPLATE = """\
# subscribe_dump — per-project config
#
# Each [[projects]] block describes a subscribe-proxy deployment backed by a
# Cloudflare D1 database that follows the mortgage_rate_tracker schema
# (subscribers + sends tables). List every project you own here, then run
# `dump_subscribers.py` to get a single CSV spanning them all.
#
# Fields:
#   name        a short id for the project (appears in the output CSV)
#   database    the D1 database name (the one you pass to `wrangler d1 execute`)
#   worker_dir  absolute or ~-prefixed path to the subscribe-proxy directory
#               (wrangler must be runnable from here)
#   lists       optional map of list code → human-readable label
#
# This file is safe to commit to a private location; it contains no secrets.

[[projects]]
name       = "mortgage_rate_tracker"
database   = "mortgage-subscribe-audit"
worker_dir = "~/public/mortgage_rate_tracker/subscribe-proxy"
lists      = { ca = "Canadian Commercial Prime Rate", us = "US Bank Prime Rate" }

# Example — add more as you build them:
# [[projects]]
# name       = "another_tracker"
# database   = "another-subscribe-audit"
# worker_dir = "~/public/another_project/subscribe-proxy"
# lists      = { free = "Free tier", pro = "Pro tier" }
"""

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

SQL_ONE_LIST = """
SELECT
  s.email        AS email,
  s.list         AS list,
  s.confirmed_at AS confirmed_at,
  (SELECT COUNT(*) FROM sends WHERE email = s.email AND list = s.list) AS total_sends,
  (SELECT MAX(date_sent) FROM sends WHERE email = s.email AND list = s.list) AS last_sent
FROM subscribers s
WHERE s.list = '{LIST}'
ORDER BY s.confirmed_at DESC
""".strip()

FULL_FIELDS = [
    "project", "email", "list", "list_name",
    "confirmed_at", "total_sends", "last_sent",
]
STATS_FIELDS = ["project", "list", "list_name", "subscriber_count", "total_sends"]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def ensure_config() -> list[dict]:
    """Read the projects config, creating a default on first run."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(DEFAULT_CONFIG_TEMPLATE)
        sys.stderr.write(
            f"Created default config at {CONFIG_PATH}\n"
            f"Edit it to add more projects; re-run when ready.\n"
        )
    with open(CONFIG_PATH, "rb") as f:
        data = tomllib.load(f)
    projects = data.get("projects", [])
    if not projects:
        raise SystemExit(f"No [[projects]] blocks found in {CONFIG_PATH}")

    # Expand ~ in worker_dir and validate shape.
    for p in projects:
        for req in ("name", "database", "worker_dir"):
            if req not in p:
                raise SystemExit(f"project '{p.get('name', '?')}' missing '{req}' in {CONFIG_PATH}")
        p["worker_dir"] = os.path.expanduser(p["worker_dir"])
        p.setdefault("lists", {})
    return projects


# ---------------------------------------------------------------------------
# Wrangler query
# ---------------------------------------------------------------------------
def run_wrangler_query(project: dict, list_filter: str | None) -> list[dict]:
    sql = SQL_ONE_LIST.replace("{LIST}", list_filter) if list_filter else SQL_ALL
    cmd = [
        "npx", "wrangler", "d1", "execute", project["database"],
        "--remote", "--json", "--command", sql,
    ]
    proc = subprocess.run(
        cmd, cwd=project["worker_dir"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"[{project['name']}] wrangler d1 execute failed (exit {proc.returncode}):\n"
        )
        sys.stderr.write(proc.stderr)
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(f"[{project['name']}] non-JSON output from wrangler:\n")
        sys.stderr.write(proc.stdout)
        return []
    if isinstance(data, list):
        return data[0].get("results", []) if data else []
    return data.get("results", [])


def enrich(rows: list[dict], project: dict) -> list[dict]:
    """Add project + list_name columns."""
    labels = project.get("lists", {})
    out = []
    for row in rows:
        enriched = dict(row)
        enriched["project"] = project["name"]
        list_code = row.get("list", "")
        enriched["list_name"] = labels.get(list_code, list_code)
        out.append(enriched)
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_full_csv(rows: list[dict], fh) -> None:
    writer = csv.DictWriter(fh, fieldnames=FULL_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in FULL_FIELDS})


def write_stats_csv(rows: list[dict], fh) -> None:
    """Collapse per-subscriber rows into per-(project, list) stats."""
    from collections import defaultdict
    buckets: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {"subscriber_count": 0, "total_sends": 0}
    )
    for row in rows:
        key = (row["project"], row["list"], row.get("list_name", row["list"]))
        buckets[key]["subscriber_count"] += 1
        buckets[key]["total_sends"] += int(row.get("total_sends") or 0)

    writer = csv.DictWriter(fh, fieldnames=STATS_FIELDS)
    writer.writeheader()
    for (project, list_code, list_name), agg in sorted(buckets.items()):
        writer.writerow({
            "project": project,
            "list": list_code,
            "list_name": list_name,
            "subscriber_count": agg["subscriber_count"],
            "total_sends": agg["total_sends"],
        })


def default_output_path(scope_tag: str, mode: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%SZ")
    kind = "stats" if mode == "stats" else "subscribers"
    return DEFAULT_DIR / f"{kind}_{scope_tag}_{stamp}.csv"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump subscribers across configured subscribe-proxy projects.",
    )
    parser.add_argument(
        "--project", default=None,
        help="Restrict to one project's name (as set in projects.toml). Default: all.",
    )
    parser.add_argument(
        "--list", dest="list_filter", default=None,
        help="Restrict to one list code (e.g. ca, us, en). Requires --project.",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Aggregate counts per (project, list) instead of per-subscriber rows. "
             "Email addresses are NOT written in this mode.",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help=f"Output path. Use '-' for stdout. "
             f"Default: {DEFAULT_DIR}/subscribers_<scope>_<utc-timestamp>.csv",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.list_filter and not args.project:
        sys.stderr.write("--list requires --project (lists are per-project)\n")
        return 2

    projects = ensure_config()

    if args.project:
        projects = [p for p in projects if p["name"] == args.project]
        if not projects:
            sys.stderr.write(
                f"No project named '{args.project}' in {CONFIG_PATH}.\n"
                f"Available: {', '.join(p['name'] for p in ensure_config())}\n"
            )
            return 2

    all_rows: list[dict] = []
    for proj in projects:
        raw = run_wrangler_query(proj, args.list_filter)
        all_rows.extend(enrich(raw, proj))

    if args.output == "-":
        fh = sys.stdout
        if args.stats:
            write_stats_csv(all_rows, fh)
        else:
            write_full_csv(all_rows, fh)
        sys.stderr.write(f"({len(all_rows)} subscriber rows)\n")
        return 0

    scope_tag = (
        args.project + (f"_{args.list_filter}" if args.list_filter else "")
        if args.project else
        "all"
    )
    mode = "stats" if args.stats else "full"
    out_path = Path(args.output).expanduser() if args.output else default_output_path(scope_tag, mode)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", newline="") as f:
        if args.stats:
            write_stats_csv(all_rows, f)
        else:
            write_full_csv(all_rows, f)

    sys.stderr.write(f"Wrote {len(all_rows)} subscriber rows to {out_path}\n")
    if not all_rows:
        sys.stderr.write("(No subscribers matched.)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
