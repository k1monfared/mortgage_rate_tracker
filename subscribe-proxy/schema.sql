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

-- 3. Sends: ledger of every email the worker sends, one row per
--    (recipient, UTC day). Drives the "no address receives more than one
--    email per day" guarantee, across both lists AND confirmation emails.
--    Every send path claims the day's row first with INSERT OR IGNORE and
--    only emails the recipient if the insert created a row.
CREATE TABLE IF NOT EXISTS sends (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email      TEXT NOT NULL,
  list       TEXT NOT NULL,            -- 'ca' | 'us' for broadcasts,
                                       -- 'confirm' for confirmation emails
  date_sent  TEXT NOT NULL,            -- 'YYYY-MM-DD' UTC
  UNIQUE(email, list, date_sent)
);
CREATE INDEX IF NOT EXISTS idx_sends_date ON sends(date_sent);

-- One email per address per day, regardless of list. Partial so that history
-- from before the cutover (when the limit was per list) cannot make this
-- migration fail.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sends_one_per_day
  ON sends(email, date_sent) WHERE date_sent >= '2026-10-30';

-- 4. Deferred: rate emails held back because the address already got an
--    email that day. /flush sends them on a later day, one per address per
--    day, oldest first. `lists` is the message key: 'ca', 'us' or 'ca+us'.
--    A newer message for the same lists replaces an older held one, since
--    its history table already includes the older change.
CREATE TABLE IF NOT EXISTS deferred (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  email        TEXT NOT NULL,
  lists        TEXT NOT NULL,
  subject      TEXT NOT NULL,
  html         TEXT NOT NULL,             -- still has {{UNSUB_URL}} placeholders
  text         TEXT NOT NULL,
  created_day  TEXT NOT NULL,             -- 'YYYY-MM-DD' UTC
  UNIQUE(email, lists)
);
CREATE INDEX IF NOT EXISTS idx_deferred_day ON deferred(created_day);
