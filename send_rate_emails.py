"""
Rate-change email sender.

Change-detection strategy, per region:

  ca -> data/boc_policy_rate.csv            (BoC policy rate)
  us -> data/us_fed_target_upper.csv        (FOMC target rate upper bound,
                                             FRED series DFEDTARU)

The mortgage prime rate for each region is shown inside the email as context
(same-date value + a typical-lag note), but is NOT what triggers a send.

When a new change is detected, an email payload is POSTed to the subscribe
proxy's /broadcast endpoint, which fans out to every confirmed subscriber for
that region. Per-day dedup is enforced server-side.

State file schema (data/rate_changes.json):

    {
      "ca": {
        "series": "Bank of Canada policy rate",
        "changes": [
          {"date": "2025-11-01", "value": 2.50},
          {"date": "2025-09-01", "value": 2.75},
          {"date": "2025-03-01", "value": 3.00},
          {"date": "2025-02-01", "value": 3.25}
        ]
      },
      "us": { ... }
    }

changes[0] is the most recent. Four entries are kept so the history table in
the email can show four changes.

Env vars:
  SUBSCRIBE_PROXY_URL     worker URL, e.g.
                          https://mortgage-rates-subscribe-proxy.k1.workers.dev
  BROADCAST_AUTH_KEY      shared secret
  SENDER_BASE_URL         optional, defaults to
                          https://k1monfared.github.io/mortgage_rate_tracker
  DRY_RUN=1               compose + log, don't POST

CLI:
  python send_rate_emails.py                    # normal pipeline
  python send_rate_emails.py --draft ca         # write drafts/ca.md (or the
                                                #   path passed via --out)
                                                # for the current most-recent
                                                #   change, without sending
  python send_rate_emails.py --draft ca --out email_draft.md
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

DATA_DIR = Path("data")
STATE_PATH = DATA_DIR / "rate_changes.json"
LAG_CACHE_PATH = DATA_DIR / "prime_lag.json"
HISTORY_LENGTH = 4
LAG_WINDOW_YEARS = 2
DEFAULT_BASE_URL = "https://k1monfared.github.io/mortgage_rate_tracker"

# Per-region config. Keep slug in sync with build_site.REGIONS and with the
# subscribe-proxy worker's VALID_LISTS.
REGIONS: Dict[str, Dict[str, str]] = {
    "ca": {
        "label":            "Bank of Canada policy rate",
        "central_bank":     "Bank of Canada",
        "trigger_csv":      str(DATA_DIR / "boc_policy_rate.csv"),
        "prime_csv":        str(DATA_DIR / "commercial_prime_rate.csv"),
        "prime_label":      "Canadian mortgage prime rate",
        "page_path":        "/ca/",
        "subscriber_label": "Canadian rate updates",
        "subject_prefix":   "BoC policy rate",
    },
    "us": {
        "label":            "Fed target rate (upper bound)",
        "central_bank":     "Federal Reserve",
        "trigger_csv":      str(DATA_DIR / "us_fed_target_upper.csv"),
        "prime_csv":        str(DATA_DIR / "us_prime_rate.csv"),
        "prime_label":      "US mortgage prime rate",
        "page_path":        "/us/",
        "subscriber_label": "US rate updates",
        "subject_prefix":   "Fed target rate",
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
        print(f"Could not parse {STATE_PATH}: {e} — treating as empty.", file=sys.stderr)
        return {}


def save_state(state: Dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"Wrote {STATE_PATH}")


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
def _load_rate_csv(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"])
    df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
    df = df.dropna(subset=["rate"]).sort_values("date").reset_index(drop=True)
    return df


def extract_recent_changes(csv_path: Path, limit: int = HISTORY_LENGTH) -> List[Dict]:
    """Return the last `limit` distinct-value transitions, newest first."""
    df = _load_rate_csv(csv_path)
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


def prime_on_date(prime_csv: Path, date_str: str) -> Optional[float]:
    """Return the prime rate 'as of' date_str (last-obs-carried-forward)."""
    df = _load_rate_csv(prime_csv)
    target = pd.to_datetime(date_str)
    sub = df[df["date"] <= target]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["rate"])


def current_prime(prime_csv: Path) -> Tuple[Optional[float], Optional[str]]:
    df = _load_rate_csv(prime_csv)
    if df.empty:
        return None, None
    row = df.iloc[-1]
    return float(row["rate"]), row["date"].strftime("%Y-%m-%d")


def _rate_transitions(df: pd.DataFrame) -> List[pd.Timestamp]:
    out, prev = [], None
    for _, row in df.iterrows():
        r = float(row["rate"])
        if prev is None or r != prev:
            out.append(row["date"])
        prev = r
    return out


def compute_prime_lag_days(policy_csv: Path, prime_csv: Path,
                           years: int = LAG_WINDOW_YEARS,
                           max_gap: int = 90) -> Optional[Dict]:
    """Compute the median days from policy→next-prime change over the last
    `years` years. Expensive (scans two CSVs), so we only call this when a
    new change is detected — the result lives in the lag cache.

    Returns a dict {median_days, samples, window_start} or None if no samples.
    """
    policy_df = _load_rate_csv(policy_csv)
    prime_df  = _load_rate_csv(prime_csv)
    if policy_df.empty or prime_df.empty:
        return None

    latest = max(policy_df["date"].max(), prime_df["date"].max())
    window_start = latest - pd.DateOffset(years=years)

    policy_dates = [d for d in _rate_transitions(policy_df) if d >= window_start]
    prime_dates  = _rate_transitions(prime_df)

    lags: List[int] = []
    for pd_ in policy_dates:
        next_prime = [d for d in prime_dates if d > pd_]
        if not next_prime:
            continue
        lag = (next_prime[0] - pd_).days
        if 0 < lag <= max_gap:
            lags.append(lag)
    if not lags:
        return None
    return {
        "median_days":  int(round(statistics.median(lags))),
        "samples":      len(lags),
        "window_start": window_start.strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------------------
# Lag cache — recomputed only when a new change is detected, then reused on
# subsequent runs. Keeps the per-run work minimal.
# ---------------------------------------------------------------------------
def load_lag_cache() -> Dict:
    if not LAG_CACHE_PATH.exists():
        return {}
    try:
        with open(LAG_CACHE_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"Could not parse {LAG_CACHE_PATH}: {e} — treating as empty.", file=sys.stderr)
        return {}


def save_lag_cache(cache: Dict) -> None:
    LAG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LAG_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"Wrote {LAG_CACHE_PATH}")


def refresh_lag_for_region(slug: str) -> Optional[int]:
    """Recompute the lag for `slug` over the last LAG_WINDOW_YEARS years and
    persist it. Returns the new median, or the currently-cached value if the
    recompute produced nothing usable.
    """
    cfg = REGIONS[slug]
    result = compute_prime_lag_days(Path(cfg["trigger_csv"]), Path(cfg["prime_csv"]))
    cache = load_lag_cache()
    if result is not None:
        cache[slug] = {
            **result,
            "refreshed_at": pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        save_lag_cache(cache)
        print(f"[{slug}] Refreshed prime-lag cache: median {result['median_days']} days "
              f"({result['samples']} samples since {result['window_start']}).")
        return result["median_days"]
    return cache.get(slug, {}).get("median_days")


def cached_lag_days(slug: str) -> Optional[int]:
    return load_lag_cache().get(slug, {}).get("median_days")


# ---------------------------------------------------------------------------
# Email composition
# ---------------------------------------------------------------------------
def direction_word(new_val: float, prev_val: float) -> str:
    return "decreased" if new_val < prev_val else "increased"


def _enriched_history(history: List[Dict], trigger_csv: Path,
                      prime_csv: Path) -> List[Dict]:
    """Annotate each history row with same-date prime, days-since-last, and
    signed policy delta vs the immediately-preceding change. For the oldest
    row, the prior change is pulled from the CSV so the delta is still
    accurate even though it isn't displayed.
    """
    # Pull enough extra entries from the CSV to cover one prior to the
    # oldest displayed row (if it's outside `history`).
    extended = extract_recent_changes(trigger_csv, limit=HISTORY_LENGTH + 5)
    out: List[Dict] = []
    for i, h in enumerate(history):
        date = h["date"]
        val  = float(h["value"])

        # Prefer the next-older entry already in `history`; fall back to CSV.
        prior = history[i + 1] if i + 1 < len(history) else None
        if prior is None:
            prior = next((c for c in extended if c["date"] < date), None)

        if prior is not None:
            days  = (pd.to_datetime(date) - pd.to_datetime(prior["date"])).days
            delta = val - float(prior["value"])
        else:
            days  = None
            delta = None

        out.append({
            "date":            date,
            "value":           val,
            "prime":           prime_on_date(prime_csv, date),
            "days_since_last": days,
            "delta":           delta,
        })
    return out


def _fmt_signed_pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    sign = "+" if x > 0 else ("-" if x < 0 else "")
    return f"{sign}{abs(x):.2f}%"


def _fmt_days(d: Optional[int]) -> str:
    return "—" if d is None else str(d)


def compose_email(region_slug: str, history: List[Dict], base_url: str) -> Dict[str, str]:
    """Build subject, markdown, html, and text for a broadcast.

    `{{UNSUB_URL}}` / `{{PREFS_URL}}` placeholders are substituted by the
    worker per recipient.
    """
    cfg = REGIONS[region_slug]
    prime_csv = Path(cfg["prime_csv"])

    newest = history[0]
    newest_val = float(newest["value"])
    rate_pct = f"{newest_val:.2f}%"
    page_url = base_url.rstrip("/") + cfg["page_path"]

    # Direction + delta compared to the previous stored entry (if any).
    if len(history) >= 2:
        prev_val = float(history[1]["value"])
        direction = direction_word(newest_val, prev_val)
        delta = abs(newest_val - prev_val)
        delta_str = f"{delta:.2f}%"
    else:
        direction = "changed to"
        prev_val = None
        delta_str = None

    # Prime context at the change date.
    prime_at_change = prime_on_date(prime_csv, newest["date"])
    prime_now, _ = current_prime(prime_csv)
    # Value to display in the lede: prime rate as of the change date. Falls
    # back to the most recent prime observation if we have nothing on/before
    # the change date.
    prime_lede = prime_at_change if prime_at_change is not None else prime_now
    # Read from the cached lag (refreshed whenever a change is detected by
    # process_region or refresh_lag_for_region). Never recompute here.
    lag_days = cached_lag_days(region_slug)

    rows = _enriched_history(history, Path(cfg["trigger_csv"]), prime_csv)

    subject = f"{cfg['subject_prefix']} {direction} to {rate_pct}"

    # --- Markdown (the canonical human-readable copy) ----------------------
    md_lines: List[str] = []
    md_lines.append(f"# {subject}")
    md_lines.append("")
    if delta_str:
        md_lines.append(
            f"The **{cfg['label']}** {direction} to **{rate_pct}** "
            f"on {newest['date']} by {delta_str}."
        )
    else:
        md_lines.append(
            f"The **{cfg['label']}** is now **{rate_pct}** as of {newest['date']}."
        )
    md_lines.append("")
    if prime_lede is not None:
        lag_note = (
            f" Prime rate typically follows within about {lag_days} days."
            if lag_days else ""
        )
        md_lines.append(
            f"The **{cfg['prime_label']}** is **{prime_lede:.2f}%**.{lag_note}"
        )
        md_lines.append("")
    md_lines.append("**Recent history**")
    md_lines.append("")
    md_lines.append(
        f"| Date       | {cfg['label']} | {cfg['prime_label']} (same date) | "
        f"Days since last | Change |"
    )
    md_lines.append("| ---------- | ---: | ---: | ---: | ---: |")
    for r in rows:
        prime_cell = f"{r['prime']:.2f}%" if r["prime"] is not None else "—"
        md_lines.append(
            f"| {r['date']} | {r['value']:.2f}% | {prime_cell} | "
            f"{_fmt_days(r['days_since_last'])} | {_fmt_signed_pct(r['delta'])} |"
        )
    md_lines.append("")
    md_lines.append(f"[View the full chart]({page_url})")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("")
    md_lines.append(
        f"You are receiving this because you subscribed to {cfg['subscriber_label']}."
    )
    md_lines.append(
        "[Unsubscribe from this list]({{UNSUB_URL}}) · "
        "[Manage preferences]({{PREFS_URL}}) · "
        "[Support](https://k1monfared.github.io/sponsor.html)"
    )
    markdown = "\n".join(md_lines) + "\n"

    # --- HTML --------------------------------------------------------------
    if delta_str:
        lede_html = (
            f"<p>The <strong>{cfg['label']}</strong> {direction} to "
            f"<strong>{rate_pct}</strong> on {newest['date']} by {delta_str}.</p>"
        )
    else:
        lede_html = (
            f"<p>The <strong>{cfg['label']}</strong> is now "
            f"<strong>{rate_pct}</strong> as of {newest['date']}.</p>"
        )
    if prime_lede is not None:
        lag_note_html = (
            f" Prime rate typically follows within about {lag_days} days."
            if lag_days else ""
        )
        prime_html = (
            f"<p>The <strong>{cfg['prime_label']}</strong> is "
            f"<strong>{prime_lede:.2f}%</strong>.{lag_note_html}</p>"
        )
    else:
        prime_html = ""

    def _html_row(r: Dict) -> str:
        prime_cell = f"{r['prime']:.2f}%" if r["prime"] is not None else "—"
        return (
            f"<tr><td>{r['date']}</td>"
            f"<td style=\"text-align:right;\">{r['value']:.2f}%</td>"
            f"<td style=\"text-align:right;\">{prime_cell}</td>"
            f"<td style=\"text-align:right;\">{_fmt_days(r['days_since_last'])}</td>"
            f"<td style=\"text-align:right;\">{_fmt_signed_pct(r['delta'])}</td>"
            f"</tr>"
        )
    table_rows_html = "".join(_html_row(r) for r in rows)
    table_html = (
        "<p><strong>Recent history</strong></p>"
        "<table role=\"presentation\" cellpadding=\"6\" cellspacing=\"0\" "
        "style=\"border-collapse:collapse;margin:4px 0 12px;font-size:14px;\">"
        "<thead><tr>"
        f"<th style=\"text-align:left;border-bottom:1px solid #ddd;\">Date</th>"
        f"<th style=\"text-align:right;border-bottom:1px solid #ddd;\">{cfg['label']}</th>"
        f"<th style=\"text-align:right;border-bottom:1px solid #ddd;\">{cfg['prime_label']}<br>(same date)</th>"
        f"<th style=\"text-align:right;border-bottom:1px solid #ddd;\">Days since last</th>"
        f"<th style=\"text-align:right;border-bottom:1px solid #ddd;\">Change</th>"
        "</tr></thead>"
        f"<tbody>{table_rows_html}</tbody>"
        "</table>"
    )

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#222;line-height:1.55;max-width:560px;margin:0 auto;padding:24px;">
{lede_html}
{prime_html}
{table_html}
<p><a href="{page_url}" style="color:#0b5394;">View the full chart</a></p>
<hr style="margin:24px 0;border:none;border-top:1px solid #ddd;">
<p style="font-size:12px;color:#666;">
  You are receiving this because you subscribed to {cfg['subscriber_label']}.
  <a href="{{{{UNSUB_URL}}}}" style="color:#666;">Unsubscribe from this list</a>
  &middot; <a href="{{{{PREFS_URL}}}}" style="color:#666;">Manage preferences</a>
  &middot; <a href="https://k1monfared.github.io/sponsor.html" style="color:#666;">Support</a>
</p>
</body></html>"""

    # --- Plain text --------------------------------------------------------
    text_lines: List[str] = []
    if delta_str:
        text_lines.append(
            f"The {cfg['label']} {direction} to {rate_pct} "
            f"on {newest['date']} by {delta_str}."
        )
    else:
        text_lines.append(f"The {cfg['label']} is now {rate_pct} as of {newest['date']}.")
    if prime_lede is not None:
        lag_note_txt = (
            f" Prime rate typically follows within about {lag_days} days."
            if lag_days else ""
        )
        text_lines.append(f"The {cfg['prime_label']} is {prime_lede:.2f}%.{lag_note_txt}")
    text_lines.append("")
    text_lines.append("Recent history:")
    text_lines.append("  date        policy   prime   days  change")
    for r in rows:
        prime_cell = f"{r['prime']:.2f}%" if r["prime"] is not None else "   — "
        days_cell  = _fmt_days(r['days_since_last']).rjust(4)
        text_lines.append(
            f"  {r['date']}  {r['value']:5.2f}%  {prime_cell:>6}  {days_cell}  "
            f"{_fmt_signed_pct(r['delta'])}"
        )
    text_lines.append("")
    text_lines.append(f"View the full chart: {page_url}")
    text_lines.append("Unsubscribe from this list: {{UNSUB_URL}}")
    text_lines.append("Manage preferences (both regions): {{PREFS_URL}}")
    text_lines.append("Support: https://k1monfared.github.io/sponsor.html")

    return {
        "subject":  subject,
        "html":     html,
        "text":     "\n".join(text_lines),
        "markdown": markdown,
    }


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
                "Content-Type":  "application/json",
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
        print(f"  POST /broadcast failed: {e}", file=sys.stderr)
        return False

    if not resp.ok:
        print(f"  Worker returned {resp.status_code}: {resp.text}", file=sys.stderr)
        return False

    try:
        data = resp.json()
        print(f"  Worker accepted: sent={data.get('sent', 0)}, "
              f"skipped_dedup={data.get('skipped_dedup', 0)}, "
              f"failed={data.get('failed', 0)}")
    except Exception:
        print(f"  Worker returned {resp.status_code} (non-JSON body)")
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def process_region(slug: str, state: Dict, proxy_url: Optional[str],
                   auth_key: Optional[str], base_url: str, dry_run: bool) -> Dict:
    cfg = REGIONS[slug]
    trigger_csv = Path(cfg["trigger_csv"])
    if not trigger_csv.exists():
        print(f"Missing trigger CSV for {slug}: {trigger_csv} — skipping.")
        return state.get(slug, {})

    latest = extract_recent_changes(trigger_csv)
    if not latest:
        print(f"No data in {trigger_csv} for {slug}.")
        return state.get(slug, {})

    region_block = state.get(slug, {})
    stored = region_block.get("changes", [])
    stored_series = region_block.get("series")

    # Reseed on first run OR when the stored series doesn't match the current
    # trigger (e.g. leftover entries from a different series before a refactor).
    # Without this guard, merge_new_change would prepend the new change onto
    # unrelated history, producing a mixed-series table.
    if not stored or stored_series != cfg["label"]:
        if stored and stored_series != cfg["label"]:
            print(f"[{slug}] Stored series '{stored_series}' != configured "
                  f"'{cfg['label']}' — reseeding history from {trigger_csv}.")
        else:
            print(f"[{slug}] Seeding initial history with last {len(latest)} changes.")
        return {"series": cfg["label"], "changes": latest}

    merged, new_change = merge_new_change(stored, latest)
    region_block = {"series": cfg["label"], "changes": merged}

    if not new_change:
        print(f"[{slug}] No new change (top: {merged[0]['date']} {merged[0]['value']}%).")
        return region_block

    newest = merged[0]
    print(f"[{slug}] New change detected: {newest['date']} -> {newest['value']}%")

    # A new change landed, so the prime-lag sample set has grown. Refresh the
    # cached 2-year median before composing so the email reflects current data.
    refresh_lag_for_region(slug)

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
        print(f"  Skipping send — missing env: {', '.join(missing)}")
    else:
        send_via_proxy(slug, email, proxy_url, auth_key)

    return region_block


def write_draft(slug: str, base_url: str, out_path: Path) -> None:
    """Write the markdown draft for a region's most-recent trigger change.

    Always reads directly from the region's trigger CSV (policy/target) —
    the send-state file is for dedup, not for content, and can lag or hold
    pre-refactor values.
    """
    cfg = REGIONS[slug]
    changes = extract_recent_changes(Path(cfg["trigger_csv"]))
    if not changes:
        raise RuntimeError(f"No history available for {slug}.")

    email = compose_email(slug, changes, base_url)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(email["markdown"], encoding="utf-8")
    print(f"Wrote draft for {slug} -> {out_path}")
    print(f"  subject: {email['subject']}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--draft", metavar="REGION",
        help="Write the markdown draft for REGION (e.g. 'ca' or 'us') and exit. "
             "Does not send.",
    )
    parser.add_argument(
        "--out", metavar="PATH", default=None,
        help="Path for the draft file. Defaults to drafts/<region>.md, or "
             "email_draft.md when REGION is ca (so the existing review file "
             "stays in place).",
    )
    parser.add_argument(
        "--refresh-lag", metavar="REGION", nargs="?", const="all",
        help="Recompute the cached prime-lag (last 2 years) for REGION (or "
             "'all'). Normally only needed for first-run seeding; the change-"
             "detection pipeline refreshes this automatically when a change "
             "lands.",
    )
    args = parser.parse_args(argv)

    base_url  = os.environ.get("SENDER_BASE_URL", DEFAULT_BASE_URL)

    if args.refresh_lag:
        slugs = list(REGIONS) if args.refresh_lag == "all" else [args.refresh_lag.lower()]
        for slug in slugs:
            if slug not in REGIONS:
                parser.error(f"Unknown region '{slug}'. Known: {', '.join(REGIONS)}.")
            refresh_lag_for_region(slug)
        return 0

    if args.draft:
        slug = args.draft.lower()
        if slug not in REGIONS:
            parser.error(f"Unknown region '{slug}'. Known: {', '.join(REGIONS)}.")
        if cached_lag_days(slug) is None:
            # First-run seed so the draft has a lag note. Subsequent draft
            # calls reuse the cache until a change triggers a refresh.
            print(f"[{slug}] Lag cache empty — seeding from the last {LAG_WINDOW_YEARS} years.")
            refresh_lag_for_region(slug)
        if args.out:
            out_path = Path(args.out)
        elif slug == "ca":
            out_path = Path("email_draft.md")
        else:
            out_path = Path("drafts") / f"{slug}.md"
        write_draft(slug, base_url, out_path)
        return 0

    proxy_url = os.environ.get("SUBSCRIBE_PROXY_URL")
    auth_key  = os.environ.get("BROADCAST_AUTH_KEY")
    dry_run   = os.environ.get("DRY_RUN") == "1"

    state = load_state()
    for slug in REGIONS:
        state[slug] = process_region(slug, state, proxy_url, auth_key, base_url, dry_run)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
