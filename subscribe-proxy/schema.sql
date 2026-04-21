-- subscribe-proxy audit log schema.
-- Apply with: npx wrangler d1 execute mortgage-subscribe-audit --remote --file=./schema.sql
--
-- Re-running this file is safe: all DDL uses IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS subscribe_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                     -- ISO 8601 UTC, e.g. 2026-04-20T19:07:34.123Z
  event TEXT NOT NULL,                  -- 'subscribe_attempt' | 'confirm_attempt' | 'block_attempt'
  outcome TEXT NOT NULL,                -- see outcomes list in the worker source
  email TEXT,                           -- lowercased; null for attempts rejected before parsing a valid email
  list TEXT,                            -- 'ca' | 'us'
  ip TEXT,                              -- from CF-Connecting-IP
  country TEXT,                         -- from request.cf.country
  user_agent TEXT,
  referer TEXT,
  token_prefix TEXT                     -- first 8 hex chars of the token, lets subscribe↔confirm↔block rows be correlated without storing the full token
);

CREATE INDEX IF NOT EXISTS idx_logs_ts      ON subscribe_logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_email   ON subscribe_logs(email);
CREATE INDEX IF NOT EXISTS idx_logs_ip      ON subscribe_logs(ip);
CREATE INDEX IF NOT EXISTS idx_logs_outcome ON subscribe_logs(outcome);
