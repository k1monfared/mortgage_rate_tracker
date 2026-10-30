"""The email's prime sentence for every state of the prime data."""

import send_rate_emails as emails


def status(tmp_path, rows, day="2026-10-29"):
    csv = tmp_path / "prime.csv"
    csv.write_text("date,rate\n" + "".join(f"{d},{r}\n" for d, r in rows))
    return emails.prime_status(csv, day)


def test_no_prime_data(tmp_path):
    assert status(tmp_path, []) is None


def test_prime_not_published_for_the_change_yet(tmp_path):
    s = status(tmp_path, [("2026-10-21", 4.45), ("2026-10-28", 4.45)])
    assert s == ("The prime rate was **4.45%** as of 2026-10-28. The banks' prime rate "
                 "for this change has not been published yet.")


def test_prime_moved_the_same_day(tmp_path):
    s = status(tmp_path, [("2026-10-28", 4.45), ("2026-10-29", 4.70)])
    assert s == "The prime rate increased from 4.45% to **4.70%** on the same day."


def test_prime_moved_later(tmp_path):
    s = status(tmp_path, [("2026-10-28", 4.45), ("2026-11-04", 4.70)])
    assert s == "The prime rate increased from 4.45% to **4.70%** on 2026-11-04."


def test_prime_unchanged_since_the_change(tmp_path):
    s = status(tmp_path, [("2026-10-28", 4.45), ("2026-11-04", 4.45)])
    assert s == "The prime rate is unchanged at **4.45%** as of 2026-11-04."


def test_prime_series_starts_after_the_change(tmp_path):
    s = status(tmp_path, [("2026-11-04", 4.70)])
    assert s == "The prime rate is **4.70%** as of 2026-11-04."
