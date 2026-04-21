"""
Rate-change email sender.

Compares the latest commercial prime rate in each region's CSV against the
known-change history in data/rate_changes.json. When a new change is detected,
sends a short broadcast via the Resend API to that region's audience and
updates the state file to include the new change (keeping only the last 3).

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

changes[0] is the most recent. The file is committed back to the repo by the
Monday keep-alive step in .github/workflows/deploy.yml; this script only
writes it (the workflow does the git push).

Env vars required to actually send:
  RESEND_API_KEY          Resend API key with broadcast send permission
  AUDIENCE_ID_CA          Resend audience UUID for the CA list
  AUDIENCE_ID_US          Resend audience UUID for the US list
  FROM_ADDR               e.g. "Mortgage Rates <rates@yourdomain>"
  SENDER_BASE_URL         optional, defaults to https://k1monfared.github.io/mortgage_rate_tracker

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
        "audience_env": "AUDIENCE_ID_CA",
        "page_path": "/ca/",
    },
    "us": {
        "label": "US Bank Prime Rate",
        "short_label": "US mortgage prime rate",
        "csv": str(DATA_DIR / "us_prime_rate.csv"),
        "audience_env": "AUDIENCE_ID_US",
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
    """Return the last `limit` distinct-value transitions, newest first.

    A "change" is a row whose rate differs from the row immediately before it.
    The very first row of the dataset counts as a change (series onset).
    """
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

    # Newest first, trimmed.
    return list(reversed(changes))[:limit]


def changes_are_equal(a: List[Dict], b: List[Dict]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x.get("date") != y.get("date"):
            return False
        if float(x.get("value", 0)) != float(y.get("value", 0)):
            return False
    return True


def merge_new_change(stored: List[Dict], latest_from_csv: List[Dict]) -> Tuple[List[Dict], bool]:
    """Given the stored history and the freshly computed history, return
    (new_history, newest_change_detected).

    - If the top entry in the CSV history matches the top of stored, no change.
    - Otherwise the CSV top is a new change; prepend it to stored and trim.
    """
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
    da = pd.to_datetime(a)
    db = pd.to_datetime(b)
    return int(abs((db - da).days))


def compose_email(region_slug: str, history: List[Dict], base_url: str) -> Dict[str, str]:
    """Build subject + html + text for a broadcast announcing the newest change."""
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
  <a href="{{{{{{RESEND_UNSUBSCRIBE_URL}}}}}}" style="color:#666;">Unsubscribe</a>
  &middot; <a href="https://k1monfared.github.io/sponsor.html" style="color:#666;">Support</a>
</p>
</body></html>"""

    text_sentences = []
    text_sentences.append(
        f"The {cfg['label']} changed to {rate_pct} on {newest['date']}."
    )
    if len(history) >= 2:
        prev = history[1]
        days = days_between(prev["date"], newest["date"])
        text_sentences.append(
            f"Previously it was {prev['value']:.2f}%, effective {prev['date']} ({days} days)."
        )
    if len(history) >= 3:
        older = history[2]
        text_sentences.append(f"Before that, {older['value']:.2f}% on {older['date']}.")
    text_sentences.append(f"\nView the full chart: {page_url}")
    text_sentences.append("Unsubscribe: {{{RESEND_UNSUBSCRIBE_URL}}}")
    text_sentences.append("Support: https://k1monfared.github.io/sponsor.html")

    return {"subject": subject, "html": html, "text": "\n".join(text_sentences)}


# ---------------------------------------------------------------------------
# Resend broadcast send
# ---------------------------------------------------------------------------
def send_broadcast(audience_id: str, from_addr: str, email: Dict[str, str], api_key: str) -> bool:
    """Create and send a Resend broadcast to the given audience.

    Uses the two-step broadcast API: POST /broadcasts to create, POST
    /broadcasts/{id}/send to send. Returns True on success.
    """
    create_resp = requests.post(
        "https://api.resend.com/broadcasts",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "audience_id": audience_id,
            "from": from_addr,
            "subject": email["subject"],
            "html": email["html"],
            "text": email["text"],
        },
        timeout=30,
    )
    if not create_resp.ok:
        print(f"❌ Resend create failed: {create_resp.status_code} {create_resp.text}",
              file=sys.stderr)
        return False
    broadcast_id = create_resp.json().get("id")
    if not broadcast_id:
        print(f"❌ Resend create returned no id: {create_resp.text}", file=sys.stderr)
        return False

    send_resp = requests.post(
        f"https://api.resend.com/broadcasts/{broadcast_id}/send",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    if not send_resp.ok:
        print(f"❌ Resend send failed: {send_resp.status_code} {send_resp.text}",
              file=sys.stderr)
        return False
    print(f"✓ Broadcast {broadcast_id} queued")
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def process_region(slug: str, state: Dict, api_key: Optional[str], from_addr: Optional[str],
                   base_url: str, dry_run: bool) -> Dict:
    """Update state[slug] in place. Returns the region's new state block."""
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
        # First-ever run for this region: seed and send nothing.
        print(f"[{slug}] Seeding initial history with last {len(latest)} changes.")
        region_block = {"series": cfg["label"], "changes": latest}
        return region_block

    merged, new_change = merge_new_change(stored, latest)
    region_block = {"series": cfg["label"], "changes": merged}

    if not new_change:
        print(f"[{slug}] No new change (top: {merged[0]['date']} {merged[0]['value']}%).")
        return region_block

    newest = merged[0]
    print(f"[{slug}] New change detected: {newest['date']} -> {newest['value']}%")

    # Compose email from the merged (post-update) history, which has the
    # newest change as [0] and the previous one(s) as [1..].
    email = compose_email(slug, merged, base_url)
    print(f"  subject: {email['subject']}")
    print(f"  page:    {base_url.rstrip('/')}{cfg['page_path']}")

    audience_id = os.environ.get(cfg["audience_env"])

    if dry_run:
        print("  DRY_RUN=1 set — skipping actual send.")
    elif not api_key or not from_addr or not audience_id:
        missing = [
            name for name, val in [
                ("RESEND_API_KEY", api_key),
                ("FROM_ADDR", from_addr),
                (cfg["audience_env"], audience_id),
            ] if not val
        ]
        print(f"  ⚠️  Skipping send — missing env: {', '.join(missing)}")
    else:
        send_broadcast(audience_id, from_addr, email, api_key)

    return region_block


def main() -> int:
    api_key   = os.environ.get("RESEND_API_KEY")
    from_addr = os.environ.get("FROM_ADDR")
    base_url  = os.environ.get("SENDER_BASE_URL", DEFAULT_BASE_URL)
    dry_run   = os.environ.get("DRY_RUN") == "1"

    state = load_state()
    for slug in REGIONS:
        state[slug] = process_region(slug, state, api_key, from_addr, base_url, dry_run)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
