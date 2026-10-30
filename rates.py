"""
Every rate series the tracker uses: where it comes from, where it is stored,
and how it is refreshed. build_site.py, send_rate_emails.py and boc_monitor.py
all read series from here, so a source or file name lives in exactly one place.

Sources (all free, no API key):

  ca/policy  Bank of Canada target for the overnight rate, business daily.
             StatCan vector 39079 for 1992-12 .. 2009-04-20 (the same series,
             longer history), BoC Valet V39079 from 2009-04-21 on. Valet
             publishes day D on day D+1. Note that a rate decision takes
             effect the day after the announcement.
  ca/prime   Chartered bank prime rate. BoC Valet V80691311 (weekly, as at
             Wednesday), so a change shows up to 6 days late there. Changes
             with an exact effective date from a bank's own press release
             (data/ca_prime_bank_releases.json, cited per change) override
             it. Plus RBC's posted prime from RBC's public rates API,
             which carries its own effective date and updates the same day a
             bank changes prime. The RBC point is best effort.
  us/policy  Effective federal funds rate, FRED DFF (daily).
  us/target  FOMC target rate. FRED DFEDTAR (single target, to 2008-12-15)
             then DFEDTARU (upper bound of the range, from 2008-12-16).
             Updated on the day of the FOMC decision.
  us/prime   Bank prime loan rate, FRED DPRIME (daily).

Announcements (ca/policy only): Valet publishes a new policy rate a day or
more after the Bank announces it. The Bank's press release RSS feed carries
the announcement the same morning, so every run reads it, parses the new rate
from the release title (cross-checked against the summary) and stores each
rate announcement in data/ca_announcements.json. The new rate takes effect on
the next business day. Until Valet has an observation on or after that day
the announcement is "pending": load_with_pending() adds it to the series as
its newest point, so the chart, the stat boxes and the email trigger all use
it. Once Valet covers the effective date the announcement becomes
"confirmed" and Valet's own value is used. Announcements that leave the rate
unchanged are stored as "held". A title that cannot be parsed is skipped with
a warning, which leaves the pipeline exactly as it was without the feed.

Refresh rule: every run downloads each series in full and merges it into the
CSV on disk. Fresh observations override stored ones on the same date, and
stored dates the fresh download lacks are kept. A source outage or a
truncated download therefore can never move a series backwards, which is what
let stale data trigger false emails before.

CSV schema: data/<region>_<key>.csv with columns date (YYYY-MM-DD), rate.

CLI:
  python rates.py        refresh every series and print a summary
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import partial
from io import StringIO
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "data"
TIMEOUT = 60
USER_AGENT = "Mozilla/5.0 (mortgage_rate_tracker; +https://github.com/k1monfared/mortgage_rate_tracker)"

VALET_URL = "https://www.bankofcanada.ca/valet/observations/{code}/json"
STATCAN_URL = "https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorByReferencePeriodRange"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
RBC_RATES_URL = "https://apps.royalbank.com/uaw0/app-services/public-rates/api/publicrates"
RBC_PRIME_ID = "0006470002"  # "Royal Bank Prime" in RBC's ratesIdMapping/pr.js
BOC_PRESS_FEED_URL = "https://www.bankofcanada.ca/content_type/press-releases/feed/"

# Anything outside this range is treated as a parsing or source error.
RATE_MIN, RATE_MAX = -1.0, 30.0


# ---------------------------------------------------------------------------
# Fetchers. Each returns a DataFrame with columns date (datetime64), rate
# (float), sorted by date with unique dates, and raises on any failure.
# ---------------------------------------------------------------------------
def _frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["date", "rate"])
    df["date"] = pd.to_datetime(df["date"])
    df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
    df = df.dropna().drop_duplicates("date", keep="last")
    return df.sort_values("date").reset_index(drop=True)


def _get(url: str, **params) -> requests.Response:
    resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


def fetch_valet(code: str) -> pd.DataFrame:
    obs = _get(VALET_URL.format(code=code)).json()["observations"]
    return _frame([(o["d"], o[code]["v"]) for o in obs if o.get(code, {}).get("v") not in (None, "")])


def fetch_statcan(vector: str, start: str, end: str) -> pd.DataFrame:
    payload = _get(STATCAN_URL, vectorIds=f'"{vector}"',
                   startRefPeriod=start, endReferencePeriod=end).json()
    if payload[0].get("status") != "SUCCESS":
        raise RuntimeError(f"StatCan returned {payload[0].get('status')}")
    points = payload[0]["object"]["vectorDataPoint"]
    return _frame([(p["refPer"], p["value"]) for p in points if p["value"] is not None])


def fetch_fred(series_id: str) -> pd.DataFrame:
    df = pd.read_csv(StringIO(_get(FRED_URL, id=series_id).text))
    if series_id not in df.columns:
        raise RuntimeError(f"FRED response has no {series_id} column: {list(df.columns)}")
    return _frame(df[[df.columns[0], series_id]].values.tolist())


def load_bank_releases() -> pd.DataFrame:
    """Prime changes at their exact effective dates, from banks' own press
    releases (data/ca_prime_bank_releases.json, one cited URL per change)."""
    changes = json.loads((DATA_DIR / "ca_prime_bank_releases.json").read_text())["changes"]
    return _frame([(c["effective"], c["rate"]) for c in changes])


def fetch_rbc_prime() -> pd.DataFrame:
    """RBC's posted prime as a single observation dated at its effective date."""
    rates = _get(RBC_RATES_URL, lang="en").json()
    entries = rates if isinstance(rates, list) else next(
        v for v in rates.values() if isinstance(v, list))
    entry = next(e for e in entries if e.get("Name") == RBC_PRIME_ID)
    value = float(entry["Value"])
    # Canadian prime moves in 5bp steps. Anything else means the payload
    # changed shape, so refuse it rather than plot a bad point.
    if round(value * 100) % 5 != 0:
        raise RuntimeError(f"unexpected RBC prime value {entry['Value']!r}")
    return _frame([(pd.to_datetime(entry["EffDate"], format="%Y%m%d"), value)])


# ---------------------------------------------------------------------------
# Series registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Part:
    """One source segment of a series. Later parts win on overlapping dates."""
    fetch: Callable[[], pd.DataFrame]
    source: str
    start: Optional[str] = None
    end: Optional[str] = None
    required: bool = True


@dataclass(frozen=True)
class Series:
    region: str
    key: str
    label: str
    parts: Tuple[Part, ...]
    # Transitions smaller than this are dropped when compressing for the chart
    # (DFF wiggles by a basis point most days).
    min_delta: float = 0.0
    # A warning is printed when the newest observation is older than this.
    max_age_days: int = 7

    @property
    def name(self) -> str:
        return f"{self.region}/{self.key}"

    @property
    def csv(self) -> Path:
        return DATA_DIR / f"{self.region}_{self.key}.csv"


SERIES: Dict[Tuple[str, str], Series] = {(s.region, s.key): s for s in [
    Series("ca", "policy", "BoC Policy Rate", (
        Part(partial(fetch_statcan, "39079", "1992-01-01", "2009-04-20"),
             "StatCan v39079", end="2009-04-20"),
        Part(partial(fetch_valet, "V39079"), "BoC Valet V39079", start="2009-04-21"),
    )),
    Series("ca", "prime", "Canadian Prime Rate", (
        Part(partial(fetch_valet, "V80691311"), "BoC Valet V80691311"),
        Part(load_bank_releases, "bank press releases"),
        Part(fetch_rbc_prime, "RBC posted prime", required=False),
    ), max_age_days=10),
    Series("us", "policy", "Fed Funds Rate", (
        Part(partial(fetch_fred, "DFF"), "FRED DFF"),
    ), min_delta=0.01),
    Series("us", "target", "Fed Target Rate", (
        Part(partial(fetch_fred, "DFEDTAR"), "FRED DFEDTAR", end="2008-12-15"),
        Part(partial(fetch_fred, "DFEDTARU"), "FRED DFEDTARU", start="2008-12-16"),
    )),
    Series("us", "prime", "US Bank Prime Rate", (
        Part(partial(fetch_fred, "DPRIME"), "FRED DPRIME"),
    )),
]}


def get(region: str, key: str) -> Series:
    return SERIES[(region, key)]


def region_series(region: str) -> List[Series]:
    return [s for s in SERIES.values() if s.region == region]


# ---------------------------------------------------------------------------
# Load / refresh
# ---------------------------------------------------------------------------
def load(series: Series) -> pd.DataFrame:
    """Read a series from its CSV. Raises if the file is missing or empty."""
    df = pd.read_csv(series.csv)
    df = _frame(df[["date", "rate"]].values.tolist())
    if df.empty:
        raise RuntimeError(f"{series.csv} is empty")
    return df


def _warn(msg: str) -> None:
    # The ::warning:: prefix becomes an annotation on the GitHub Actions run.
    print(f"::warning::{msg}")


def fetch(series: Series) -> pd.DataFrame:
    """Download every part of a series and splice them together."""
    frames = []
    for part in series.parts:
        try:
            df = part.fetch()
            if part.start:
                df = df[df["date"] >= pd.Timestamp(part.start)]
            if part.end:
                df = df[df["date"] <= pd.Timestamp(part.end)]
            if df.empty:
                raise RuntimeError("no observations")
        except Exception as e:
            if part.required:
                raise RuntimeError(f"{series.name}: {part.source} failed: {e}") from e
            _warn(f"{series.name}: optional source {part.source} skipped: {e}")
            continue
        print(f"  {part.source}: {len(df)} obs, "
              f"{df['date'].min():%Y-%m-%d} .. {df['date'].max():%Y-%m-%d}")
        frames.append(df)
    return _frame(pd.concat(frames)[["date", "rate"]].values.tolist())


def merge(stored: Optional[pd.DataFrame], fresh: pd.DataFrame) -> pd.DataFrame:
    """Union of stored and fresh observations, fresh winning on equal dates."""
    if stored is None or stored.empty:
        return fresh
    return _frame(pd.concat([stored, fresh])[["date", "rate"]].values.tolist())


def validate(series: Series, df: pd.DataFrame) -> None:
    if df.empty:
        raise RuntimeError(f"{series.name}: no data")
    bad = df[(df["rate"] < RATE_MIN) | (df["rate"] > RATE_MAX)]
    if not bad.empty:
        raise RuntimeError(f"{series.name}: rates out of range:\n{bad.head()}")
    age = (pd.Timestamp.now().normalize() - df["date"].max()).days
    if age > series.max_age_days:
        _warn(f"{series.name}: newest observation is {age} days old "
              f"({df['date'].max():%Y-%m-%d})")


def save(series: Series, df: pd.DataFrame) -> None:
    series.csv.parent.mkdir(parents=True, exist_ok=True)
    out = df.assign(date=df["date"].dt.strftime("%Y-%m-%d"))
    out.to_csv(series.csv, index=False)


def refresh(series: Series) -> pd.DataFrame:
    """Fetch, merge into the stored CSV, validate and save.

    If a required source fails and a stored CSV exists, the stored data is
    kept and a warning is printed, so one outage does not block the site or
    the other region's emails. With no stored CSV the error propagates.
    """
    print(f"\n[{series.name}] {series.label}")
    stored = load(series) if series.csv.exists() else None
    try:
        fresh = fetch(series)
    except Exception as e:
        if stored is None:
            raise
        _warn(f"{e}. Keeping stored data (newest {stored['date'].max():%Y-%m-%d}).")
        return stored
    df = merge(stored, fresh)
    validate(series, df)
    save(series, df)
    last = df.iloc[-1]
    print(f"  saved {series.csv.name}: {len(df)} obs, latest {last['date']:%Y-%m-%d} = {last['rate']:.2f}%")
    return df


def refresh_all() -> Dict[Tuple[str, str], pd.DataFrame]:
    result = {k: refresh(s) for k, s in SERIES.items()}
    refresh_announcements(result[ANNOUNCED_SERIES])
    return result


# ---------------------------------------------------------------------------
# Bank of Canada rate announcements (see the module docstring)
# ---------------------------------------------------------------------------
ANNOUNCED_SERIES = ("ca", "policy")
FRACTIONS = {"¼": 0.25, "½": 0.5, "¾": 0.75}
# "raises the policy rate by 25 basis points to 2½%", "maintains the policy
# rate at 2¼%", "lowers policy rate to ¼ percent".
TITLE_RATE_RE = re.compile(
    r"policy (?:interest )?rate\b.*?\b(?:to|at)\s+(\d+(?:\.\d+)?)?\s*([¼½¾])?\s*(?:%|per\s?cent)",
    re.IGNORECASE)
# Titles of rate decisions, as opposed to other releases that mention the rate
# ("publishes its 2027 schedule for policy interest rate announcements").
DECISION_RE = re.compile(
    r"\b(raises|increases|lowers|reduces|cuts|maintains|holds)\b.*\b(policy|overnight)",
    re.IGNORECASE)
# "... overnight rate by 25 basis points to 2.5%" in the summary.
SUMMARY_RATE_RE = re.compile(
    r"overnight rate\b.*?\b(?:to|at)\s+(\d+(?:\.\d+)?)\s*(?:%|per\s?cent)", re.IGNORECASE)


def announcements_path() -> Path:
    return DATA_DIR / "ca_announcements.json"


def parse_announced_rate(title: str, summary: str = "") -> Optional[float]:
    """The new policy rate in a rate announcement, or None if `title` is not
    one or the rate cannot be read reliably."""
    m = TITLE_RATE_RE.search(title)
    if not m or not (m.group(1) or m.group(2)):
        return None
    rate = float(m.group(1) or 0) + FRACTIONS.get(m.group(2) or "", 0.0)
    # Policy rates move in quarter points. Anything else is a parse error.
    if round(rate * 100) % 25 != 0 or not RATE_MIN <= rate <= RATE_MAX:
        return None
    s = SUMMARY_RATE_RE.search(summary or "")
    if s and float(s.group(1)) != rate:
        return None
    return rate


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def fetch_boc_announcements() -> List[Dict]:
    """Rate announcements in the Bank's press release feed, newest first."""
    root = ET.fromstring(_get(BOC_PRESS_FEED_URL).content)
    out = []
    for item in (e for e in root.iter() if _local(e.tag) == "item"):
        fields = {}
        for child in item:
            fields.setdefault(_local(child.tag), (child.text or "").strip())
        title = fields.get("title", "")
        if not DECISION_RE.search(title):
            continue
        rate = parse_announced_rate(title, fields.get("description", ""))
        if rate is None:
            _warn(f"could not read a rate from BoC announcement {title!r}. Skipped.")
            continue
        out.append({
            "announced": fields.get("date", "")[:10],
            "rate":      rate,
            "title":     title,
            "summary":   fields.get("description", ""),
            "url":       fields.get("link") or item.get(
                "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", ""),
        })
    return out


def _rate_on(df: pd.DataFrame, date: pd.Timestamp) -> Optional[float]:
    sub = df[df["date"] <= date]
    return None if sub.empty else float(sub.iloc[-1]["rate"])


def load_announcements() -> List[Dict]:
    path = announcements_path()
    if not path.exists():
        return []
    return json.loads(path.read_text())["announcements"]


def classify(item: Dict, policy: pd.DataFrame) -> Dict:
    """Set the effective date and status of one announcement against Valet."""
    announced = pd.Timestamp(item["announced"])
    effective = announced + pd.offsets.BDay(1)
    previous = _rate_on(policy, announced)
    if previous == item["rate"]:
        status = "held"
    elif policy["date"].max() >= effective:
        status = "confirmed"
        published = _rate_on(policy, policy[policy["date"] >= effective]["date"].min())
        if published != item["rate"]:
            _warn(f"BoC announced {item['rate']}% effective {effective:%Y-%m-%d} but "
                  f"Valet shows {published}%. Valet is used.")
    else:
        status = "pending"
    return {**item, "effective": effective.strftime("%Y-%m-%d"),
            "previous": previous, "status": status}


def refresh_announcements(policy: pd.DataFrame) -> List[Dict]:
    """Fetch the feed, merge it into the stored announcements (by URL) and
    re-classify every one against the policy series. A feed failure keeps the
    stored announcements."""
    print("\n[ca/policy] Bank of Canada rate announcements")
    stored = {a["url"]: a for a in load_announcements()}
    try:
        for a in fetch_boc_announcements():
            stored[a["url"]] = {**stored.get(a["url"], {}), **a}
    except Exception as e:
        _warn(f"BoC press release feed failed: {e}. Keeping stored announcements.")
    items = sorted((classify(a, policy) for a in stored.values()),
                   key=lambda a: a["announced"], reverse=True)
    path = announcements_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source": BOC_PRESS_FEED_URL, "announcements": items},
                               indent=2, ensure_ascii=False) + "\n")
    for a in items[:3]:
        print(f"  {a['announced']}: {a['rate']:.2f}% effective {a['effective']} ({a['status']})")
    return items


def pending(series: Series) -> Optional[Dict]:
    """The announced but not yet published change for `series`, if any."""
    if (series.region, series.key) != ANNOUNCED_SERIES:
        return None
    return next((a for a in load_announcements() if a["status"] == "pending"), None)


def announcement_for(series: Series, date: str) -> Optional[Dict]:
    """The announcement of the change that took effect on `date`, if any."""
    if (series.region, series.key) != ANNOUNCED_SERIES:
        return None
    return next((a for a in load_announcements()
                 if a["effective"] == date and a["status"] != "held"), None)


def load_with_pending(series: Series) -> pd.DataFrame:
    """load(series) plus a pending announced change as the newest point."""
    df = load(series)
    p = pending(series)
    if p is None:
        return df
    return merge(df, _frame([(p["effective"], p["rate"])]))


if __name__ == "__main__":
    refresh_all()
    sys.exit(0)
