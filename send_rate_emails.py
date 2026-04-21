"""
Rate-change email sender.

Compares the latest commercial prime rate in each region's CSV against the
known-change history in data/rate_changes.json. When a new change is detected,
POSTs an email payload to the subscribe-proxy Cloudflare Worker at
/broadcast; the worker fans the email out to every confirmed subscriber for
that region, with per-day dedup. This keeps the subscriber list off this
machine and off GitHub — GitHub Actions never sees an email address.

State file schema (data/rate_changes.json):

    {
      "ca": {
        "series": "Commercial Prime Rate",
        "changes": [
          {"date": "2026-04-15", "value": 4.45},
          {"date": "2025-12-11", "value": 4.70},
          {"date": "2025-09-04", "value": 4.95}
        ]
      },
      "us": { ... }
    }

changes[0] is the most recent.

Env vars required to actually send:
  SUBSCRIBE_PROXY_URL     URL of the deployed worker, e.g.
                          https://mortgage-rates-subscribe-proxy.k1.workers.dev
  BROADCAST_AUTH_KEY      shared secret also set on the worker
  SENDER_BASE_URL         optional, defaults to
                          https://k1monfared.github.io/mortgage_rate_tracker

If any required env var is missing the script still updates the state file
and logs what *would* have been sent, so local dry-runs are safe. Set
DRY_RUN=1 to force skip sending even when env vars are present.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

DATA_DIR = Path("data")
STATE_PATH = DATA_DIR / "rate_changes.json"
HISTORY_LENGTH = 3
DEFAULT_BASE_URL = "https://k1monfared.github.io/mortgage_rate_tracker"

# Per-region config. Keep keys in sync with build_site.REGIONS slugs.
REGIONS: Dict[str, Dict[str, str]] = {
    "ca": {
        "label": "Canadian Commercial Prime Rate",
        "short_label": "CA mortgage prime rate",
        "csv": str(DATA_DIR / "commercial_prime_rate.csv"),
        "page_path": "/ca/",
    },
    "us": {
        "label": "US Bank Prime Rate",
        "short_label": "US mortgage prime rate",
        "csv": str(DATA_DIR / "us_prime_rate.csv"),
        "page_path": "/us/",
    },
}


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------
def load_state() -> Dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️  Could not parse {STATE_PATH}: {e} — treating as empty.", file=sys.stderr)
        return {}


def save_state(state: Dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"💾 Wrote {STATE_PATH}")


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------
def extract_recent_changes(csv_path: Path, limit: int = HISTORY_LENGTH) -> List[Dict]:
    """Return the last `limit` distinct-value transitions, newest first."""
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
    df = df.dropna(subset=["rate"])

    changes: List[Dict] = []
    prev_rate: Optional[float] = None
    for _, row in df.iterrows():
        rate = float(row["rate"])
        if prev_rate is None or rate != prev_rate:
            changes.append({"date": row["date"].strftime("%Y-%m-%d"), "value": rate})
        prev_rate = rate

    return list(reversed(changes))[:limit]


def merge_new_change(stored: List[Dict], latest_from_csv: List[Dict]) -> Tuple[List[Dict], bool]:
    """Given stored history and freshly-computed history, return
    (new_history, a_new_change_was_detected)."""
    if not latest_from_csv:
        return stored, False
    if stored and stored[0].get("date") == latest_from_csv[0]["date"] \
            and float(stored[0].get("value", 0)) == float(latest_from_csv[0]["value"]):
        return stored, False
    new_top = latest_from_csv[0]
    merged = [new_top] + [c for c in stored if c["date"] != new_top["date"]]
    return merged[:HISTORY_LENGTH], True


# ---------------------------------------------------------------------------
# Email composition
# ---------------------------------------------------------------------------
def days_between(a: str, b: str) -> int:
    return int(abs((pd.to_datetime(b) - pd.to_datetime(a)).days))


def compose_email(region_slug: str, history: List[Dict], base_url: str) -> Dict[str, str]:
    """Build subject + html + text for a broadcast announcing the newest change.
    `{{UNSUB_URL}}` placeholder is substituted by the worker per recipient."""
    cfg = REGIONS[region_slug]
    newest = history[0]
    rate_pct = f"{newest['value']:.2f}%"
    subject = f"Rate update: {cfg['short_label']} is now {rate_pct}"
    page_url = base_url.rstrip("/") + cfg["page_path"]

    sentences = [
        f"The <strong>{cfg['label']}</strong> changed to <strong>{rate_pct}</strong> on {newest['date']}."
    ]
    if len(history) >= 2:
        prev = history[1]
        days = days_between(prev["date"], newest["date"])
        sentences.append(
            f"Previously it was {prev['value']:.2f}%, effective {prev['date']} ({days} days)."
        )
    if len(history) >= 3:
        older = history[2]
        sentences.append(f"Before that, {older['value']:.2f}% on {older['date']}.")

    html_body = "".join(f"<p>{s}</p>" for s in sentences)
    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#222;line-height:1.55;max-width:560px;margin:0 auto;padding:24px;">
{html_body}
<p><a href="{page_url}" style="color:#0b5394;">View the full chart</a></p>
<hr style="margin:24px 0;border:none;border-top:1px solid #ddd;">
<p style="font-size:12px;color:#666;">
  You are receiving this because you subscribed to {cfg['short_label']} updates.
  <a href="{{{{UNSUB_URL}}}}" style="color:#666;">Unsubscribe from this list</a>
  &middot; <a href="{{{{PREFS_URL}}}}" style="color:#666;">Manage preferences</a>
  &middot; <a href="https://k1monfared.github.io/sponsor.html" style="color:#666;">Support</a>
</p>
</body></html>"""

    text_lines = [
        f"The {cfg['label']} changed to {rate_pct} on {newest['date']}.",
    ]
    if len(history) >= 2:
        prev = history[1]
        days = days_between(prev["date"], newest["date"])
        text_lines.append(
            f"Previously it was {prev['value']:.2f}%, effective {prev['date']} ({days} days)."
        )
    if len(history) >= 3:
        older = history[2]
        text_lines.append(f"Before that, {older['value']:.2f}% on {older['date']}.")
    text_lines.append("")
    text_lines.append(f"View the full chart: {page_url}")
    text_lines.append("Unsubscribe from this list: {{UNSUB_URL}}")
    text_lines.append("Manage preferences (both regions): {{PREFS_URL}}")
    text_lines.append("Support: https://k1monfared.github.io/sponsor.html")

    return {"subject": subject, "html": html, "text": "\n".join(text_lines)}


# ---------------------------------------------------------------------------
# Dispatch via subscribe-proxy worker
# ---------------------------------------------------------------------------
def send_via_proxy(list_slug: str, email: Dict[str, str],
                   proxy_url: str, auth_key: str) -> bool:
    url = proxy_url.rstrip("/") + "/broadcast"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {auth_key}",
                "Content-Type": "application/json",
            },
            json={
                "list":    list_slug,
                "subject": email["subject"],
                "html":    email["html"],
                "text":    email["text"],
            },
            timeout=60,
        )
    except requests.RequestException as e:
        print(f"  ❌ POST /broadcast failed: {e}", file=sys.stderr)
        return False

    if not resp.ok:
        print(f"  ❌ Worker returned {resp.status_code}: {resp.text}", file=sys.stderr)
        return False

    try:
        data = resp.json()
        print(f"  ✓ Worker accepted: sent={data.get('sent', 0)}, "
              f"skipped_dedup={data.get('skipped_dedup', 0)}, "
              f"failed={data.get('failed', 0)}")
    except Exception:
        print(f"  ✓ Worker returned {resp.status_code} (non-JSON body)")
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def process_region(slug: str, state: Dict, proxy_url: Optional[str],
                   auth_key: Optional[str], base_url: str, dry_run: bool) -> Dict:
    cfg = REGIONS[slug]
    csv_path = Path(cfg["csv"])
    if not csv_path.exists():
        print(f"⚠️  Missing CSV for {slug}: {csv_path} — skipping.")
        return state.get(slug, {})

    latest = extract_recent_changes(csv_path)
    if not latest:
        print(f"⚠️  No data in {csv_path} for {slug}.")
        return state.get(slug, {})

    region_block = state.get(slug, {})
    stored = region_block.get("changes", [])

    if not stored:
        print(f"[{slug}] Seeding initial history with last {len(latest)} changes.")
        return {"series": cfg["label"], "changes": latest}

    merged, new_change = merge_new_change(stored, latest)
    region_block = {"series": cfg["label"], "changes": merged}

    if not new_change:
        print(f"[{slug}] No new change (top: {merged[0]['date']} {merged[0]['value']}%).")
        return region_block

    newest = merged[0]
    print(f"[{slug}] New change detected: {newest['date']} -> {newest['value']}%")

    email = compose_email(slug, merged, base_url)
    print(f"  subject: {email['subject']}")
    print(f"  page:    {base_url.rstrip('/')}{cfg['page_path']}")

    if dry_run:
        print("  DRY_RUN=1 set — skipping actual send.")
    elif not proxy_url or not auth_key:
        missing = [
            name for name, val in [
                ("SUBSCRIBE_PROXY_URL", proxy_url),
                ("BROADCAST_AUTH_KEY",  auth_key),
            ] if not val
        ]
        print(f"  ⚠️  Skipping send — missing env: {', '.join(missing)}")
    else:
        send_via_proxy(slug, email, proxy_url, auth_key)

    return region_block


def main() -> int:
    proxy_url = os.environ.get("SUBSCRIBE_PROXY_URL")
    auth_key  = os.environ.get("BROADCAST_AUTH_KEY")
    base_url  = os.environ.get("SENDER_BASE_URL", DEFAULT_BASE_URL)
    dry_run   = os.environ.get("DRY_RUN") == "1"

    state = load_state()
    for slug in REGIONS:
        state[slug] = process_region(slug, state, proxy_url, auth_key, base_url, dry_run)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
