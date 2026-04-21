# subscribe-proxy

Cloudflare Worker that accepts subscribe form submissions from the mortgage
rate tracker and relays them (after double-opt-in) to Resend audiences.

See **`../SUBSCRIBE.md`** in the repo root for the end-to-end deploy guide,
including Resend audience creation and GitHub Actions secrets. This README
covers the worker itself.

## Endpoints

| Route              | Method | Purpose                                                   |
|--------------------|--------|-----------------------------------------------------------|
| `/`                | POST   | Subscribe: accept `email` + `list` (ca\|us), send confirm |
| `/confirm?token=…` | GET    | Confirmation click → add to Resend audience               |
| `/block?token=…`   | GET    | "Never email me again" → permanent silent block           |

## Quick start

```bash
npm install
npx wrangler login

npx wrangler kv namespace create SUBSCRIBE_KV
# paste the printed id into wrangler.toml (REPLACE_WITH_KV_NAMESPACE_ID)

npx wrangler d1 create mortgage-subscribe-audit
# paste the printed database_id into wrangler.toml (REPLACE_WITH_D1_DATABASE_ID)
npx wrangler d1 execute mortgage-subscribe-audit --remote --file=./schema.sql

npx wrangler secret put RESEND_API_KEY
npx wrangler secret put AUDIENCE_ID_CA
npx wrangler secret put AUDIENCE_ID_US

npx wrangler deploy
```

The deploy prints the worker URL. Copy it into `SUBSCRIBE_PROXY_URL` at the
top of `../build_site.py`.

## Outcomes (audit log)

Every subscribe, confirm, or block attempt writes one row to the `subscribe_logs`
D1 table. Useful `outcome` values:

| outcome                  | meaning                                                   |
|--------------------------|-----------------------------------------------------------|
| `bad_origin`             | POST came from an origin not in `ALLOWED_ORIGINS`         |
| `invalid_email_or_list`  | Email failed regex or `list` wasn't `ca`/`us`             |
| `list_not_configured`    | Matching `AUDIENCE_ID_*` secret was missing               |
| `blocked`                | Address is on the permanent blocklist                     |
| `rate_limited`           | Another subscribe for this email in the last 24h          |
| `send_failed`            | Resend /emails call returned non-2xx                      |
| `pending`                | Confirmation email sent, awaiting click                   |
| `confirmed`              | User clicked confirm → added to the audience              |
| `resend_error`           | Adding to Resend audience failed                          |
| `bad_token` / `expired_token` | Confirm/block click with malformed or stale token    |

Query recent activity:

```bash
npx wrangler d1 execute mortgage-subscribe-audit --remote \
  --command "SELECT ts, event, outcome, list, country FROM subscribe_logs ORDER BY ts DESC LIMIT 20;"
```
