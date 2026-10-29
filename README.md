# Interest Rate Tracker (Canada and US)

**Live chart:** [k1monfared.github.io/mortgage_rate_tracker](https://k1monfared.github.io/mortgage_rate_tracker/)

Tracks central bank policy rates and prime rates for Canada and the US, and emails subscribers when the policy rate changes. The data is refreshed and the site rebuilt every hour and on every push to `master`.

---

## Live Page

Each region page shows:

- The current policy rate and prime rate, each with its as-of date
- An interactive step chart of the full history. Hover to read values, zoom and pan freely.

## Local Setup

```bash
pip install -r requirements.txt        # site + email pipeline
pip install -r requirements_boc.txt    # also the BoC Monitor extras
```

No API keys are needed. Every source is free and public.

## Scripts

| Script | Purpose |
|---|---|
| `rates.py` | Every rate series: its sources, CSV path and label, plus the refresh logic. The only place a source is defined. |
| `build_site.py` | Refreshes all series, then generates `site/` |
| `send_rate_emails.py` | Detects policy rate changes and sends subscriber emails |
| `boc_monitor.py` | BoC Monitor CLI (AI analysis, Canadian rate refresh and plots) |
| `rate_plotter.py` | Plotly chart builder used by `boc_monitor.py plot` |
| `config.py` | BoC Monitor settings |

```bash
python rates.py                       # refresh every series into data/
python build_site.py                  # refresh, then build site/
python build_site.py --no-refresh     # build from the CSVs on disk
python -m pytest -q tests             # offline tests
```

## Data Sources

| Series | File | Source | Frequency and timing |
|---|---|---|---|
| BoC policy rate (target for the overnight rate) | `data/ca_policy.csv` | [BoC Valet](https://www.bankofcanada.ca/valet/docs) `V39079` from 2009-04-21, [StatCan](https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1010013901) vector 39079 before that (same series, from 1992-12) | Business daily. A decision takes effect the day after the announcement, and Valet publishes day D on day D+1. |
| Canadian prime rate | `data/ca_prime.csv` | BoC Valet `V80691311`, plus RBC's posted prime from RBC's public rates API | Official series is weekly (as at Wednesday). The RBC point carries its own effective date and updates the same day. It is best effort: if it fails, the run continues on the official series. |
| Fed funds effective rate | `data/us_policy.csv` | [FRED](https://fred.stlouisfed.org/) `DFF` | Daily, published the next business day |
| Fed target rate | `data/us_target.csv` | FRED `DFEDTAR` to 2008-12-15, then `DFEDTARU` (upper bound) | Daily, updated on the day of the FOMC decision |
| US bank prime rate | `data/us_prime.csv` | FRED `DPRIME` | Daily, published within a day or two |

How a refresh works: each run downloads every series in full and merges it into the CSV on disk. Fresh values replace stored values on the same date, and stored dates missing from the download are kept. A failed or truncated download therefore can never move a series backwards. If a required source fails, the stored CSV is kept and the run prints a warning annotation. Rates outside the range from $-1\%$ to $30\%$ abort the refresh.

Before October 2026 the Canadian "policy rate" came from `V122530`, which is the monthly Bank Rate. That is $0.25$ points above the policy rate and lags it by up to a month. It is no longer used.

## Automated Deployment

`.github/workflows/deploy.yml` runs hourly, on every push to `master`, and on manual trigger. Each run:

1. runs the tests,
2. refreshes all series and builds `site/`,
3. sends rate-change emails,
4. commits everything under `data/` (CSVs, email state, lag cache) in one commit,
5. deploys to GitHub Pages.

New observations arrive most days, so the data commits also keep the scheduled workflow from being disabled for inactivity.

## Rate-Change Emails

The trigger is the policy rate for Canada (`ca/policy`) and the target rate for the US (`us/target`). `data/rate_changes.json` stores the last four changes per region. A run sends an email when the newest change in the trigger CSV differs from the stored one.

No address ever receives more than one email per UTC day, across both lists and confirmation emails. The subscribe worker enforces this for every send (see [`subscribe-proxy/README.md`](subscribe-proxy/README.md)). Each run checks both regions first and sends everything in one call. When both rates changed, people on both lists get one combined email. A rate email that would be someone's second of the day (for example, the second change was detected in a later run) is held back and sent the next day, so nobody misses a change.

After switching a data source, run `python send_rate_emails.py --reseed` so the corrected history is stored without being mistaken for a new change. `--draft ca` writes a preview of the current email.

## Email Subscribers

The subscriber list lives in the `mortgage-subscribe-audit` Cloudflare D1 database, which backs the [`subscribe-proxy`](subscribe-proxy/) worker. You can query or update it from the CLI or from the Cloudflare dashboard.

### From the command line

```bash
python scripts/dump_subscribers.py
```

Writes a timestamped CSV to `~/Downloads/` with columns: `project, email, list, list_name, confirmed_at, total_sends, last_sent`. D1 is read live on every run, nothing is cached on disk.

Common variants:

```bash
python scripts/dump_subscribers.py --project mortgage_rate_tracker --list ca   # filter to one list
python scripts/dump_subscribers.py --stats                                     # counts only, no addresses
python scripts/dump_subscribers.py --output -                                  # CSV to stdout
```

First run auto-creates `~/.config/subscribe_dump/projects.toml` with this repo pre-filled. To run it from anywhere, symlink it onto your `PATH`:

```bash
ln -s ~/public/mortgage_rate_tracker/scripts/dump_subscribers.py ~/bin/subscribe-dump
```

### From the Cloudflare dashboard

1. Open [dash.cloudflare.com](https://dash.cloudflare.com) and select your account.
2. Go to **Workers & Pages**, then **D1**.
3. Click the `mortgage-subscribe-audit` database.
4. Use the **Console** tab to run SQL, or page through the `subscribers`, `sends`, and `subscribe_logs` tables directly.

### Sample queries

These work in the dashboard console or via `npx wrangler d1 execute mortgage-subscribe-audit --remote --command "..."`:

```sql
SELECT list, COUNT(*) FROM subscribers GROUP BY list;
SELECT email, list, confirmed_at FROM subscribers ORDER BY confirmed_at DESC LIMIT 20;
SELECT list, COUNT(*) FROM sends WHERE date_sent = DATE('now') GROUP BY list;
DELETE FROM subscribers WHERE email = 'someone@example.com' AND list = 'ca';
```

See [`subscribe-proxy/README.md`](subscribe-proxy/README.md) for the full schema and more helper queries.

## BoC Monitor (AI Analysis, future plan)

`boc_monitor.py` is a CLI-only tool that uses Claude AI to analyze Bank of Canada press releases, the Monetary Policy Report, and speeches, then synthesizes a stance (hawkish, dovish, hold, or mixed) with confidence, rate-change probability, direction, inflation concern, growth outlook, and key signals per source. When run, it writes `output/boc_analysis_YYYYMMDD_HHMMSS.txt` and `.json` locally. See [README_BOC_MONITOR.md](README_BOC_MONITOR.md) for full details.

**Current status:** not wired into the automated pipeline. `boc_monitor.py` is not referenced in `.github/workflows/deploy.yml`, its output does not land in `site/`, and the subscriber emails do not include it. The recurring LLM cost is not justified at the current audience size.

**To enable later**, three pieces are needed:

1. A scheduled step in `deploy.yml` that runs `boc_monitor.py` with an Anthropic API key (via `secrets.ANTHROPIC_API_KEY`), at a cadence matched to BoC announcement days rather than every build.
2. An output format that writes to `site/ca/analysis.json` (stance, confidence, probability, direction, timestamp, source list) so the static site can read it.
3. A card or section on `site/ca/index.html` that fetches `analysis.json` at page load and renders the synthesis, plus an optional inclusion in the rate-change email body.

A companion pipeline for US (FOMC statements, minutes, SEP, speeches) could follow the same shape once the Canadian side is validated.

## Disclaimer

For informational purposes only. Not financial advice.
