"""Offline tests for rates.py and the email trigger. No network access."""

import json

import pandas as pd
import pytest

import rates
import send_rate_emails as emails


def frame(*rows):
    return rates._frame(list(rows))


def make_series(tmp_path, monkeypatch, *parts):
    monkeypatch.setattr(rates, "DATA_DIR", tmp_path)
    return rates.Series("xx", "policy", "Test", tuple(parts))


def fails():
    raise RuntimeError("boom")


def test_registry_paths_are_unique_and_derived_from_keys():
    paths = [s.csv.name for s in rates.SERIES.values()]
    assert len(paths) == len(set(paths))
    assert all(p == f"{s.region}_{s.key}.csv" for p, s in zip(paths, rates.SERIES.values()))


def test_fetch_splices_parts_and_clips_ranges(tmp_path, monkeypatch):
    old = rates.Part(lambda: frame(("2000-01-01", 5), ("2000-01-03", 6)), "old", end="2000-01-02")
    new = rates.Part(lambda: frame(("2000-01-02", 1), ("2000-01-03", 2)), "new", start="2000-01-03")
    df = rates.fetch(make_series(tmp_path, monkeypatch, old, new))
    assert df.values.tolist() == frame(("2000-01-01", 5), ("2000-01-03", 2)).values.tolist()


def test_optional_part_failure_is_skipped(tmp_path, monkeypatch):
    ok = rates.Part(lambda: frame(("2000-01-01", 5)), "ok")
    df = rates.fetch(make_series(tmp_path, monkeypatch, ok, rates.Part(fails, "bad", required=False)))
    assert len(df) == 1


def test_merge_keeps_stored_dates_and_prefers_fresh():
    stored = frame(("2000-01-01", 5), ("2000-01-02", 5), ("2000-01-05", 4))
    fresh = frame(("2000-01-01", 5), ("2000-01-02", 6))
    merged = rates.merge(stored, fresh)
    assert merged["rate"].tolist() == [5, 6, 4]


def test_refresh_never_moves_backwards_on_truncated_download(tmp_path, monkeypatch):
    rows = [("2000-01-01", 5), ("2000-01-02", 6)]
    series = make_series(tmp_path, monkeypatch, rates.Part(lambda: frame(*rows), "src"))
    rates.refresh(series)
    rows.pop()  # source now returns less data
    assert rates.load(series)["rate"].tolist() == [5, 6]
    assert rates.refresh(series)["rate"].tolist() == [5, 6]


def test_required_failure_keeps_stored_data(tmp_path, monkeypatch):
    series = make_series(tmp_path, monkeypatch, rates.Part(lambda: frame(("2000-01-01", 5)), "src"))
    rates.refresh(series)
    broken = rates.Series("xx", "policy", "Test", (rates.Part(fails, "src"),))
    assert rates.refresh(broken)["rate"].tolist() == [5]


def test_required_failure_without_stored_data_raises(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError):
        rates.refresh(make_series(tmp_path, monkeypatch, rates.Part(fails, "src")))


def test_out_of_range_rates_are_rejected(tmp_path, monkeypatch):
    series = make_series(tmp_path, monkeypatch, rates.Part(lambda: frame(("2000-01-01", 250)), "src"))
    with pytest.raises(RuntimeError):
        rates.refresh(series)


def test_rbc_prime_parses_effective_date(monkeypatch):
    payload = [{"Name": "0000000001", "EffDate": "20200101", "Value": "1.00"},
               {"Name": rates.RBC_PRIME_ID, "EffDate": "20261029", "Value": "4.700"}]

    class Resp:
        def json(self):
            return payload

    monkeypatch.setattr(rates, "_get", lambda *a, **k: Resp())
    df = rates.fetch_rbc_prime()
    assert df.values.tolist() == [[pd.Timestamp("2026-10-29"), 4.7]]

    payload[1]["Value"] = "4.712"
    with pytest.raises(RuntimeError):
        rates.fetch_rbc_prime()


def test_prime_bank_releases_match_policy_effective_dates():
    # Each cited prime change takes effect on the same day as a policy change
    # in the same direction, which is what the banks' releases state.
    policy = emails.trigger_changes("ca", limit=50)
    moves = {c["date"]: c["value"] - p["value"] for c, p in zip(policy, policy[1:])}
    for _, row in rates.load_bank_releases().iterrows():
        assert row["date"].strftime("%Y-%m-%d") in moves
        assert emails.prime_on_date(rates.get("ca", "prime").csv,
                                    row["date"].strftime("%Y-%m-%d")) == row["rate"]


def test_trigger_detects_only_new_changes(tmp_path):
    csv = tmp_path / "t.csv"
    csv.write_text("date,rate\n2025-10-29,2.5\n2025-10-30,2.25\n2026-10-28,2.25\n2026-10-29,2.5\n")
    latest = emails.extract_recent_changes(csv)
    assert latest[0] == {"date": "2026-10-29", "value": 2.5}
    stored = [{"date": "2025-10-30", "value": 2.25}]
    merged, new = emails.merge_new_change(stored, latest)
    assert new and merged[0]["date"] == "2026-10-29"
    assert emails.merge_new_change(merged, latest) == (merged, False)


def test_prime_lag_counts_same_day_moves(tmp_path):
    policy, prime = tmp_path / "p.csv", tmp_path / "q.csv"
    policy.write_text("date,rate\n2026-01-01,2.0\n2026-03-01,2.25\n")
    prime.write_text("date,rate\n2026-01-01,4.2\n2026-03-01,4.45\n")
    assert emails.compute_prime_lag_days(policy, prime)["median_days"] == 0


def test_committed_email_state_matches_trigger_csvs():
    state = json.loads(emails.STATE_PATH.read_text())
    for slug, cfg in emails.REGIONS.items():
        assert state[slug]["series"] == cfg["label"]
        # Every stored change must exist in the data with the same value, so
        # a source switch can never leave history from a different series.
        in_data = {c["date"]: c["value"] for c in emails.trigger_changes(slug, limit=50)}
        stored = {c["date"]: c["value"] for c in state[slug]["changes"]}
        assert stored and all(in_data.get(d) == v for d, v in stored.items()), slug
