# Canadian Interest Rate Tracker

**Live chart:** [k1monfared.github.io/mortgage_rate_tracker](https://k1monfared.github.io/mortgage_rate_tracker/)

Tracks the Bank of Canada Policy Rate and Commercial Prime Rate over time using the [BoC Valet API](https://www.bankofcanada.ca/valet/docs). The live page is rebuilt automatically every day and on every push to `master`.

---

## Live Page

The GitHub Pages site shows:

- Current BoC Policy Rate and Commercial Prime Rate (with as-of date)
- Interactive dual-series chart going back decades — hover to inspect values, zoom/pan freely

## Local Setup

```bash
pip install -r requirements_boc.txt
```

No API keys required. All rate data comes from the public Bank of Canada Valet API.

## Scripts

| Script | Purpose |
|---|---|
| `build_site.py` | Fetches latest rates, generates `site/index.html` |
| `boc_monitor.py` | CLI for fetching/updating rate CSVs |
| `historical_rates.py` | `HistoricalRateFetcher` — pulls data from BoC Valet API |
| `rate_plotter.py` | `RatePlotter` — Plotly-based interactive chart builder |
| `config.py` | Paths, series codes, and constants |

### Build the site locally

```bash
python build_site.py
# opens site/index.html in a browser to preview
```

### Fetch/update rate data manually

```bash
# Full historical fetch (100 years)
python boc_monitor.py update-rates --full

# Incremental update (since last fetch)
python boc_monitor.py update-rates
```

## Data Sources

| Rate | BoC Series | Description |
|---|---|---|
| Policy Rate | `V122530` | Bank of Canada overnight rate target |
| Prime Rate | `V80691311` | Commercial prime lending rate |

Data is fetched fresh from the [BoC Valet API](https://www.bankofcanada.ca/valet/docs) at build time and is not committed to the repository.

## Automated Deployment

`.github/workflows/deploy.yml` runs on:

- Every push to `master`
- Daily at 14:00 UTC
- Manual trigger (`workflow_dispatch`)

The workflow installs dependencies, runs `build_site.py` to generate `site/index.html`, then deploys to GitHub Pages using `actions/deploy-pages`.

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

## BoC Monitor (AI Analysis)

`boc_monitor.py` uses Claude AI to analyze Bank of Canada press releases and speeches and assess the monetary policy stance (hawkish / dovish / hold). See [README_BOC_MONITOR.md](README_BOC_MONITOR.md) for full details.

## Disclaimer

For informational purposes only. Not financial advice.
