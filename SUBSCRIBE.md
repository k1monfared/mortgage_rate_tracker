# Subscription pipeline — deploy guide

The site lets visitors subscribe to CA- or US-specific rate-change emails.
Delivery is event-driven: an email goes out **only when the commercial prime
rate for that region actually changes**, never on a schedule.

## Architecture

```
  /ca/ subscribe form ──┐            (double-opt-in)
                         │  POST →  Cloudflare Worker (subscribe-proxy/)
  /us/ subscribe form ──┘              ├─ KV: pending tokens, blocks, rate-limits
                                       ├─ D1: audit log
                                       └─ Resend: confirmation email

  build_site.py (daily GH Actions)
    ├─ writes latest CSVs under data/
    └─ send_rate_emails.py
         ├─ reads data/commercial_prime_rate.csv + data/us_prime_rate.csv
         ├─ compares to data/rate_changes.json (last 3 transitions per region)
         ├─ on new change: Resend broadcast to that region's audience
         └─ commits the updated rate_changes.json
```

## One-time setup

You need a Resend account (you can **reuse the one from news_reader** — just
create new audiences, the subscriber lists are fully isolated), a Cloudflare
account with Workers + KV + D1 enabled, and GitHub Actions secrets.

### 1. Resend audiences

1. Log in to https://resend.com/audiences and click **Create audience**.
2. Create two audiences:
   - Name: `Mortgage Rate Tracker · Canada` → copy its UUID (this is `AUDIENCE_ID_CA`).
   - Name: `Mortgage Rate Tracker · United States` → copy its UUID (`AUDIENCE_ID_US`).
3. Verify that your sender domain (same one you use for news_reader) has a
   verified DNS record. No new DNS is needed — audiences share the domain.

### 2. Cloudflare Worker (subscribe-proxy)

From inside `subscribe-proxy/`:

```bash
npm install
npx wrangler login

# 2a. Create a KV namespace for pending tokens and blocklist.
#     Copy the printed id and paste it over REPLACE_WITH_KV_NAMESPACE_ID in wrangler.toml.
npx wrangler kv namespace create SUBSCRIBE_KV

# 2b. Create a D1 database for audit logging, then apply the schema.
#     Copy the printed database_id into wrangler.toml (replacing REPLACE_WITH_D1_DATABASE_ID).
npx wrangler d1 create mortgage-subscribe-audit
npx wrangler d1 execute mortgage-subscribe-audit --remote --file=./schema.sql

# 2c. Set the secrets.
npx wrangler secret put RESEND_API_KEY       # paste the same key you use for news_reader
npx wrangler secret put AUDIENCE_ID_CA       # paste the CA audience UUID
npx wrangler secret put AUDIENCE_ID_US       # paste the US audience UUID

# 2d. Deploy.
npx wrangler deploy
```

The deploy command prints a URL like
`https://mortgage-rates-subscribe-proxy.<account>.workers.dev`. Copy it.

### 3. Point the site forms at the worker

Open `build_site.py` and update the `SUBSCRIBE_PROXY_URL` constant near the
top to the URL you copied. The default placeholder is
`https://mortgage-rates-subscribe-proxy.k1monfared.workers.dev`.

Commit and push. The next GH Actions deploy publishes the updated forms.

### 4. GitHub Actions secrets + vars

In the repo's **Settings → Secrets and variables → Actions**:

| Kind     | Name              | Value                                                            |
|----------|-------------------|------------------------------------------------------------------|
| Secret   | `RESEND_API_KEY`  | Same Resend API key used by the worker                           |
| Secret   | `AUDIENCE_ID_CA`  | CA audience UUID                                                 |
| Secret   | `AUDIENCE_ID_US`  | US audience UUID                                                 |
| Variable | `FROM_ADDR`       | e.g. `Mortgage Rates <mortgage_rate_tracker@k1monfared.com>`     |

`FROM_ADDR` is a repo **variable** rather than a secret because it's not
sensitive, but it's set here rather than hardcoded so you can change the
display name without a code change.

## How it runs

1. The daily workflow calls `python build_site.py` as before, then `python
   send_rate_emails.py`.
2. `send_rate_emails.py` compares the newest row of each prime-rate CSV
   against `data/rate_changes.json`.
3. If the newest row represents a change the file has not yet seen, the script:
   - creates a Resend broadcast via `POST /broadcasts` addressed to the
     matching audience (`AUDIENCE_ID_CA` or `AUDIENCE_ID_US`);
   - sends the broadcast with `POST /broadcasts/{id}/send`;
   - writes the new change to the top of `changes[]` in `rate_changes.json`,
     trimming the list back to three entries.
4. The workflow detects the `rate_changes.json` diff and commits it with
   `[skip ci]` so every rate change is captured in the repo history.

## Email format

- **Subject**: `Rate update: CA mortgage prime rate is now 4.45%`
- **Body** (text form):

```
The Canadian Commercial Prime Rate changed to 4.45% on 2025-11-05.
Previously it was 4.70%, effective 2025-09-24 (42 days).
Before that, 4.95% on 2025-03-19.

View the full chart: https://k1monfared.github.io/mortgage_rate_tracker/ca/
Unsubscribe: <Resend's one-click link>
Support: https://k1monfared.github.io/sponsor.html
```

The HTML version is the same text in a tidy envelope. Unsubscribe and
`List-Unsubscribe` headers are handled natively by Resend broadcasts.

## Local dry-runs

Before you set up secrets, you can exercise the detector without sending mail:

```bash
DRY_RUN=1 python send_rate_emails.py
```

This prints what would be sent and still updates `data/rate_changes.json`.

To exercise the detector logic explicitly, temporarily rewrite
`data/rate_changes.json` so the top entry for a region is older than what's in
the CSV; the next run will detect the "new" change and compose an email.

## Blocklist / spam handling

The worker keeps a permanent per-address blocklist (`block:<email>` in KV, 10y
TTL). If someone clicks the "Block this address" link in the confirmation
email, their address is silently rejected on any future subscribe attempt —
even from the other region. Resend's native bounce and complaint handling
covers the rest.
