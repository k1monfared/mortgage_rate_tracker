# subscribe-proxy

Cloudflare Worker that fronts the mortgage rate tracker's subscription
pipeline: double-opt-in signup, per-day-deduped broadcast sends, and
one-click unsubscribe. The subscriber list lives in this worker's D1
database — nothing outside Cloudflare can read it.

See **`../SUBSCRIBE.md`** for the end-to-end deploy guide.

## Endpoints

| Route              | Method    | Purpose                                                                  |
|--------------------|-----------|--------------------------------------------------------------------------|
| `/`                | POST      | Subscribe form submission (`email` + `list=ca|us`), sends confirm email  |
| `/confirm?token=…` | GET       | Confirmation click → insert row into `subscribers`                        |
| `/block?token=…`   | GET       | "Never email me" link in the confirmation email → permanent block        |
| `/unsubscribe?token=…` | GET / POST | One-click unsub from broadcast email → delete subscriber, write block |
| `/broadcast`       | POST      | GH Actions → send a broadcast to all subscribers for a region (auth-guarded) |

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
| `sends`           | `(email, list, date_sent)` with UNIQUE → per-day dedup |

The UNIQUE constraint on `sends` is what guarantees no address receives
more than one email per region per UTC day. `/broadcast` does
`INSERT OR IGNORE` and only sends to recipients where the insert created
a new row.

## Outcomes (audit log)

Every endpoint writes one row to `subscribe_logs`. Useful `outcome` values:

| event                   | outcome                 | meaning                                                |
|-------------------------|-------------------------|--------------------------------------------------------|
| `subscribe_attempt`     | `bad_origin`            | POST came from an origin not in `ALLOWED_ORIGINS`      |
| `subscribe_attempt`     | `invalid_email_or_list` | Email failed regex or `list` wasn't `ca`/`us`          |
| `subscribe_attempt`     | `blocked`               | Address is on the permanent blocklist                  |
| `subscribe_attempt`     | `rate_limited`          | Another subscribe for this email in the last 24h       |
| `subscribe_attempt`     | `send_failed`           | Resend /emails call returned non-2xx                   |
| `subscribe_attempt`     | `pending`               | Confirmation email sent                                |
| `confirm_attempt`       | `confirmed`             | User clicked confirm → row inserted into `subscribers` |
| `confirm_attempt`       | `db_error`              | D1 insert failed                                       |
| `confirm_attempt`       | `bad_token`/`expired_token` | Confirm click with malformed or stale token        |
| `block_attempt`         | `blocked`               | Address permanently blocked via confirmation-email link |
| `unsubscribe_attempt`   | `unsubscribed`          | Row removed + KV block added                           |
| `broadcast_attempt`     | `bad_auth`              | Missing / wrong `Authorization: Bearer …`              |
| `broadcast_attempt`     | `bad_body`/`bad_list`/`missing_content` | Malformed POST body               |
| `broadcast_attempt`     | `nothing_to_send`       | Zero candidates or all blocked by per-day dedup        |
| `broadcast_attempt`     | `sent_chunk`            | A batch (≤100 recipients) delivered to Resend          |
| `broadcast_attempt`     | `resend_error`/`fetch_error` | Batch call failed; sends rows for the chunk rolled back |

## Privacy

- No endpoint returns the subscriber list or any recipient address to any
  caller.
- `/broadcast` response is always shape `{sent, skipped_dedup, failed}`
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
