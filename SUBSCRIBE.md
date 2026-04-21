# Subscription pipeline — deploy guide

The site lets visitors subscribe to CA- or US-specific rate-change emails.
Delivery is event-driven: an email goes out **only when the commercial prime
rate for that region actually changes**, never on a schedule. Each subscriber
receives **at most one email per region per UTC day**, no matter how many
times the pipeline runs.

The subscriber list lives in your Cloudflare D1 database — it never leaves
your Cloudflare account except at send time, when Resend's transactional
batch API reads it to deliver. No Resend audiences are used, so this
pipeline doesn't consume any of your Resend plan's audience/segment slots.

## Architecture

```
  /ca/ subscribe form ──┐            (double-opt-in)
                         │  POST →  Cloudflare Worker (subscribe-proxy/)
  /us/ subscribe form ──┘              ├─ KV: pending tokens, blocks, rate-limits
                                       ├─ D1 subscribe_logs: audit every event
                                       ├─ D1 subscribers:    confirmed list
                                       ├─ D1 sends:          per-day dedup ledger
                                       └─ Resend: confirmation email

  GH Actions (daily)
    ├─ build_site.py → refreshed CSVs
    └─ send_rate_emails.py
         ├─ diffs latest prime rate against data/rate_changes.json
         └─ on change: POST /broadcast (bearer-authed) → worker
                                        ├─ per-recipient INSERT OR IGNORE sends
                                        ├─ Resend /emails/batch (chunks of 100)
                                        │  with per-recipient unsub URL in
                                        │  List-Unsubscribe + List-Unsubscribe-Post
                                        └─ returns {sent, skipped_dedup, failed}
```

## One-time setup

### 1. Cloudflare Worker

From inside `subscribe-proxy/`:

```bash
npm install
npx wrangler login

# Create the KV namespace for pending tokens, rate-limit, and block keys.
# Copy the printed id and paste it over REPLACE_WITH_KV_NAMESPACE_ID in wrangler.toml.
npx wrangler kv namespace create SUBSCRIBE_KV

# Create the D1 database (audit log + subscribers + sends ledger).
# Copy the printed database_id into wrangler.toml (REPLACE_WITH_D1_DATABASE_ID).
npx wrangler d1 create mortgage-subscribe-audit
npx wrangler d1 execute mortgage-subscribe-audit --remote --file=./schema.sql

# Generate a strong broadcast auth key and share it with the Worker.
openssl rand -hex 32   # copy the output
npx wrangler secret put BROADCAST_AUTH_KEY     # paste it

# Resend API key — the same one from news_reader works fine; this pipeline
# uses only the transactional /emails and /emails/batch endpoints.
npx wrangler secret put RESEND_API_KEY

npx wrangler deploy
```

The deploy prints a URL like
`https://mortgage-rates-subscribe-proxy.<account>.workers.dev`. Copy it.

### 2. Point the site forms at the worker

Open `build_site.py` and update the `SUBSCRIBE_PROXY_URL` constant near the
top to the URL you just copied. Commit and push. The next GH Actions deploy
publishes the updated forms.

### 3. GitHub Actions secrets + variables

In the repo's **Settings → Secrets and variables → Actions**:

| Kind     | Name                  | Value                                                            |
|----------|-----------------------|------------------------------------------------------------------|
| Variable | `SUBSCRIBE_PROXY_URL` | The deployed worker URL                                          |
| Secret   | `BROADCAST_AUTH_KEY`  | Same random hex you set with `wrangler secret put`               |

The previous `AUDIENCE_ID_CA`, `AUDIENCE_ID_US`, `RESEND_API_KEY`, and
`FROM_ADDR` are no longer read by the workflow — GH Actions never talks to
Resend directly now. You can delete them.

## How it runs

1. The daily workflow calls `python build_site.py` (refreshes CSVs), then
   `python send_rate_emails.py`.
2. The sender compares the newest row of each prime-rate CSV against
   `data/rate_changes.json`. If no new change, it exits; rate_changes.json
   is unchanged.
3. On a new change, the sender composes the email and POSTs to the worker's
   `/broadcast` endpoint with `Authorization: Bearer $BROADCAST_AUTH_KEY`.
4. The worker loads the subscriber list for that region from D1. For each
   subscriber, it runs `INSERT OR IGNORE INTO sends (email, list, date_sent)`
   with today's UTC date. The insert either creates a new row (→ recipient
   is added to the send batch) or is blocked by the `UNIQUE(email, list,
   date_sent)` constraint (→ recipient already got an email for this region
   today, skipped).
5. The accepted recipients are sent via `POST /emails/batch` in chunks of
   100, each envelope carrying:
   - A per-recipient `{{UNSUB_URL}}` substituted into the HTML and text body
   - `List-Unsubscribe` and `List-Unsubscribe-Post: List-Unsubscribe=One-Click`
     headers so Gmail/Outlook show their native "Unsubscribe" button
6. The worker returns `{sent, skipped_dedup, failed}`. The sender logs those
   counts (never individual addresses — GH Actions never sees who's on the
   list).
7. `data/rate_changes.json` is updated with the new change at the top of the
   history (trimmed to three entries) and committed with `[skip ci]`.

## Email format

**Subject**: `Rate update: CA mortgage prime rate is now 4.45%`

**Body (text)**:

```
The Canadian Commercial Prime Rate changed to 4.45% on 2025-11-05.
Previously it was 4.70%, effective 2025-09-24 (42 days).
Before that, 4.95% on 2025-03-19.

View the full chart: https://k1monfared.github.io/mortgage_rate_tracker/ca/
Unsubscribe: <per-recipient worker URL>
Support: https://k1monfared.github.io/sponsor.html
```

The HTML version is the same text in a tidy envelope.

## Privacy — who can see the subscriber list

| Party                   | What they can see                                                                         |
|-------------------------|-------------------------------------------------------------------------------------------|
| **You (Cloudflare)**    | Full list via `wrangler d1 execute mortgage-subscribe-audit --command "SELECT …"`.        |
| **Worker runtime**      | Reads addresses from D1 at send time, passes them to Resend. Never logs them to stdout.    |
| **GitHub Actions**      | Never sees addresses. Only the aggregate counts `{sent, skipped_dedup, failed}`.           |
| **Resend**              | Sees each recipient's email once per delivery (unavoidable with any email provider).       |
| **Anyone else**         | Zero access. There is no `/list`, `/subscribers`, or `/export` endpoint on the worker.     |

A `curl` of `/broadcast` without a valid `Authorization: Bearer` returns 403
with no body content. A `curl` of any non-existing path returns 404.

## Scale notes

Resend's free-plan transactional: 3,000 emails/month, 100/day. CA and US
prime rates change roughly 8 times a year each. For a subscriber list of N
people, monthly outbound is `N × changes-that-month`, which stays well
under the free-plan limits for any reasonable N. The per-day dedup
guarantees no one gets spammed even if the workflow retries several times
in the same day.

## Local dry-runs

Before you set up secrets, you can exercise the detector without sending
mail:

```bash
DRY_RUN=1 python send_rate_emails.py
```

This prints what would be sent and still updates `data/rate_changes.json`.
To exercise the detector itself, temporarily rewrite `data/rate_changes.json`
so the top entry for a region is older than what's in the CSV — the next
run will detect the "new" change and compose an email.

## Unsubscribing

Every broadcast email carries two forms of unsubscribe:

1. A **text link** in the body that points at the worker's
   `/unsubscribe?token=…` with a unique per-subscriber token.
2. A **`List-Unsubscribe` header** and `List-Unsubscribe-Post` header that
   satisfy RFC 8058, so Gmail/Apple Mail show their native "Unsubscribe"
   button directly in the inbox preview.

Both routes hit the worker's `/unsubscribe` handler. The handler deletes the
subscriber row from D1 and writes a permanent `block:<email>` KV entry so
the address can't be silently re-subscribed. Re-subscribing requires a
fresh signup + confirmation click.

## Audit / debugging

Every subscribe, confirm, block, unsubscribe, and broadcast call writes one
row to `subscribe_logs`. Recent activity:

```bash
npx wrangler d1 execute mortgage-subscribe-audit --remote \
  --command "SELECT ts, event, outcome, list, country
             FROM subscribe_logs
             ORDER BY ts DESC LIMIT 30;"
```

Current subscribers:

```bash
npx wrangler d1 execute mortgage-subscribe-audit --remote \
  --command "SELECT list, COUNT(*) FROM subscribers GROUP BY list;"
```

Today's sends (for a spot check):

```bash
npx wrangler d1 execute mortgage-subscribe-audit --remote \
  --command "SELECT list, COUNT(*) FROM sends
             WHERE date_sent = DATE('now')
             GROUP BY list;"
```
