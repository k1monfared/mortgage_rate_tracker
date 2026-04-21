-- subscribe-proxy D1 schema.
-- Apply with: npx wrangler d1 execute mortgage-subscribe-audit --remote --file=./schema.sql
--
-- Re-running is safe: all DDL uses IF NOT EXISTS.

-- 1. Audit log: every subscribe / confirm / block / unsubscribe / broadcast
--    attempt produces one row. Used for debugging and abuse investigation.
CREATE TABLE IF NOT EXISTS subscribe_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                     -- ISO 8601 UTC
  event TEXT NOT NULL,                  -- 'subscribe_attempt' | 'confirm_attempt'
                                        -- | 'block_attempt' | 'unsubscribe_attempt'
                                        -- | 'broadcast_attempt'
  outcome TEXT NOT NULL,                -- see outcomes list in the worker source
  email TEXT,                           -- lowercased; null when unavailable
  list TEXT,                            -- 'ca' | 'us'
  ip TEXT,                              -- from CF-Connecting-IP
  country TEXT,                         -- from request.cf.country
  user_agent TEXT,
  referer TEXT,
  token_prefix TEXT                     -- first 8 hex chars of the token, for correlation
);

CREATE INDEX IF NOT EXISTS idx_logs_ts      ON subscribe_logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_email   ON subscribe_logs(email);
CREATE INDEX IF NOT EXISTS idx_logs_ip      ON subscribe_logs(ip);
CREATE INDEX IF NOT EXISTS idx_logs_outcome ON subscribe_logs(outcome);

-- 2. Confirmed subscribers. Private: only readable via `wrangler d1 execute`
--    (i.e. authenticated Cloudflare access). No worker endpoint exposes this
--    list. Each row carries a unique unsub_token used in every email's
--    List-Unsubscribe header.
CREATE TABLE IF NOT EXISTS subscribers (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  email        TEXT NOT NULL,
  list         TEXT NOT NULL,          -- 'ca' | 'us'
  confirmed_at TEXT NOT NULL,          -- ISO 8601 UTC
  unsub_token  TEXT NOT NULL,          -- 64-char hex, unique per row
  UNIQUE(email, list)
);
CREATE INDEX IF NOT EXISTS idx_subs_list  ON subscribers(list);
CREATE INDEX IF NOT EXISTS idx_subs_token ON subscribers(unsub_token);

-- 3. Sends: one row per (recipient, region, UTC day). Drives the "no more
--    than one email per day per region" guarantee. /broadcast does an
--    INSERT OR IGNORE here first and only emails the recipient if the insert
--    actually created a row (i.e. SQLite changes() == 1).
CREATE TABLE IF NOT EXISTS sends (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email      TEXT NOT NULL,
  list       TEXT NOT NULL,            -- 'ca' | 'us'
  date_sent  TEXT NOT NULL,            -- 'YYYY-MM-DD' UTC
  UNIQUE(email, list, date_sent)
);
CREATE INDEX IF NOT EXISTS idx_sends_date ON sends(date_sent);
