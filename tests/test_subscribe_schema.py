"""The D1 schema must allow at most one email per address per UTC day.

Runs subscribe-proxy/schema.sql against SQLite (D1 is SQLite) and replays the
worker's claim statement (claimDailySlot in src/worker.ts).
"""

import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).parent.parent / "subscribe-proxy" / "schema.sql"
CLAIM = "INSERT OR IGNORE INTO sends (email, list, date_sent) VALUES (?, ?, ?)"
CUTOVER = "2026-10-30"


def claim(db, email, list_, day):
    return db.execute(CLAIM, (email, list_, day)).rowcount == 1


def test_one_email_per_address_per_day_across_lists_and_confirmations():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA.read_text())
    assert claim(db, "a@x.com", "ca", CUTOVER)
    assert not claim(db, "a@x.com", "us", CUTOVER)
    assert not claim(db, "a@x.com", "confirm", CUTOVER)
    assert claim(db, "b@x.com", "us", CUTOVER)
    assert claim(db, "a@x.com", "us", "2026-10-31")


def test_migration_applies_over_old_per_list_history():
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE sends (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL,
                  list TEXT NOT NULL, date_sent TEXT NOT NULL, UNIQUE(email, list, date_sent))""")
    db.executemany(CLAIM, [("a@x.com", "ca", "2026-09-17"), ("a@x.com", "us", "2026-09-17")])
    db.executescript(SCHEMA.read_text())  # must not fail on the old duplicate day
    assert claim(db, "a@x.com", "ca", CUTOVER)
    assert not claim(db, "a@x.com", "us", CUTOVER)
