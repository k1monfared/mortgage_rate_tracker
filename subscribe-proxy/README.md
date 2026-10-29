# subscribe-proxy

Cloudflare Worker that fronts the mortgage rate tracker's subscription
pipeline: double-opt-in signup, broadcast sends capped at one email per
address per day, and
one-click unsubscribe. The subscriber list lives in this worker's D1
database — nothing outside Cloudflare can read it.

See **`../SUBSCRIBE.md`** for the end-to-end deploy guide.

## Endpoints

| Route              | Method    | Purpose                                                                  |
|--------------------|-----------|--------------------------------------------------------------------------|
| `/`                | POST      | Subscribe form submission (`email` + `list=ca|us`), sends confirm email  |
| `/confirm?token=…` | GET       | Confirmation click → insert row into `subscribers`                        |
| `/block?token=…`   | GET       | "Never email me" link in the confirmation email → permanent block        |
| `/unsubscribe?token=…` | GET / POST | One-click unsub from a broadcast email. Several `token` params (combined email) leave several lists |
| `/broadcast`       | POST      | GH Actions → send rate emails (auth-guarded). Body `{"messages": {"ca": …, "us": …, "ca+us": …}}`, one `{subject, html, text}` per key. Each subscriber gets the one message matching the changed lists they are on. The old `{list, subject, html, text}` shape still works |
| `/flush`           | POST      | GH Actions, every run → send emails held back by the daily limit on earlier days (auth-guarded) |

## Quick start

```bash
npm install
npx wrangler login

npx wrangler kv namespace create SUBSCRIBE_KV
# paste the printed id into wrangler.toml (REPLACE_WITH_KV_NAMESPACE_ID)

npx wrangler d1 create mortgage-subscribe-audit
# paste the printed database_id into wrangler.toml (REPLACE_WITH_D1_DATABASE_ID)
npx wrangler d1 execute mortgage-subscribe-audit --remote --file=./schema.sql

# Generate a shared secret for the broadcast endpoint and paste it at both
# `wrangler secret put BROADCAST_AUTH_KEY` and into GitHub Actions secrets.
openssl rand -hex 32

npx wrangler secret put BROADCAST_AUTH_KEY
npx wrangler secret put RESEND_API_KEY

npx wrangler deploy
```

The deploy prints the worker URL. Copy it into:
- `SUBSCRIBE_PROXY_URL` at the top of `../build_site.py`
- GitHub Actions repo **variable** `SUBSCRIBE_PROXY_URL`

## D1 tables (schema.sql)

| Table             | Purpose                                              |
|-------------------|------------------------------------------------------|
| `subscribe_logs`  | Audit row for every subscribe / confirm / broadcast  |
| `subscribers`     | Confirmed email ↔ list ↔ unsub_token (source of truth)|
| `sends`           | Ledger of every email sent: `(email, list, date_sent)`, where `list` is `ca`, `us`, `ca+us` or `confirm` |
| `deferred`        | Rate emails held back by the daily limit, waiting for `/flush` |

**One email per address per UTC day, from any path.** Before sending, both
`/broadcast` and the subscribe confirmation claim the day in `sends` (see
`claimDailySlot` in `src/worker.ts`). The claim fails if the address already
has a row for today on any list, so:

- when both rates change in the same run, someone on both lists gets one
  combined email (the `ca+us` message) instead of two,
- a rate email that would be someone's second email of the day is held in
  the `deferred` table and sent by `/flush` on a later day (oldest first,
  one per day). A newer message for the same lists replaces a held one, and
  a held email is dropped if the address has since left its lists,
- a confirmation email is not sent to an address that already got an email
  today. The form redirects to `DAILY_LIMIT_URL` asking them to try tomorrow.

The unique index `idx_sends_one_per_day` on `(email, date_sent)` makes the
claim atomic. It is partial (from 2026-10-30) so older per-list history
cannot break the migration. After pulling this change, re-apply the schema
before deploying the worker:

```bash
npx wrangler d1 execute mortgage-subscribe-audit --remote --file=./schema.sql
npx wrangler deploy
```

A failed send releases the claim, so a retry later the same day can still go
out.

## Outcomes (audit log)

Every endpoint writes one row to `subscribe_logs`. Useful `outcome` values:

| event                   | outcome                 | meaning                                                |
|-------------------------|-------------------------|--------------------------------------------------------|
| `subscribe_attempt`     | `bad_origin`            | POST came from an origin not in `ALLOWED_ORIGINS`      |
| `subscribe_attempt`     | `invalid_email_or_list` | Email failed regex or `list` wasn't `ca`/`us`          |
| `subscribe_attempt`     | `blocked`               | Address is on the permanent blocklist                  |
| `subscribe_attempt`     | `rate_limited`          | Another subscribe for this email in the last 24h       |
| `subscribe_attempt`     | `daily_limit`           | Address already got an email today, no confirmation sent |
| `subscribe_attempt`     | `send_failed`           | Resend /emails call returned non-2xx                   |
| `subscribe_attempt`     | `pending`               | Confirmation email sent                                |
| `confirm_attempt`       | `confirmed`             | User clicked confirm → row inserted into `subscribers` |
| `confirm_attempt`       | `db_error`              | D1 insert failed                                       |
| `confirm_attempt`       | `bad_token`/`expired_token` | Confirm click with malformed or stale token        |
| `block_attempt`         | `blocked`               | Address permanently blocked via confirmation-email link |
| `unsubscribe_attempt`   | `unsubscribed`          | Row removed + KV block added                           |
| `broadcast_attempt`     | `bad_auth`              | Missing / wrong `Authorization: Bearer …`              |
| `broadcast_attempt`     | `bad_body`/`bad_list`/`missing_content` | Malformed POST body               |
| `broadcast_attempt`     | `nothing_to_send`       | Zero candidates, or every candidate already emailed today (held back) |
| `flush_attempt`         | `sent_chunk`/`nothing_to_send`/`resend_error`/`fetch_error`/`bad_auth` | Same meanings, for held-back emails |
| `broadcast_attempt`     | `sent_chunk`            | A batch (≤100 recipients) delivered to Resend          |
| `broadcast_attempt`     | `resend_error`/`fetch_error` | Batch call failed; sends rows for the chunk rolled back |

## Privacy

- No endpoint returns the subscriber list or any recipient address to any
  caller.
- `/broadcast` response is always shape `{sent, deferred, failed}`
  — aggregate counts only.
- `wrangler tail` output for `/broadcast` only shows the aggregate counts.
- Read access to the tables requires authenticated Cloudflare CLI
  (`wrangler d1 execute`).

## Query helpers

Recent audit rows:
```bash
npx wrangler d1 execute mortgage-subscribe-audit --remote \
  --command "SELECT ts, event, outcome, list FROM subscribe_logs ORDER BY ts DESC LIMIT 20;"
```

Subscriber counts:
```bash
npx wrangler d1 execute mortgage-subscribe-audit --remote \
  --command "SELECT list, COUNT(*) FROM subscribers GROUP BY list;"
```

Today's sends (dedup diagnostic):
```bash
npx wrangler d1 execute mortgage-subscribe-audit --remote \
  --command "SELECT list, COUNT(*) FROM sends WHERE date_sent = DATE('now') GROUP BY list;"
```
