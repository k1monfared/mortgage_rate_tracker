"""Combined emails and the single /broadcast call. No network access."""

import send_rate_emails as emails

BASE = "https://example.com/tracker"


def history(slug):
    return emails.trigger_changes(slug)


def test_one_region_change_sends_only_its_message():
    msgs = emails.build_messages({"ca": history("ca")}, BASE)
    assert list(msgs) == ["ca"]


def test_both_regions_changed_adds_one_combined_message():
    msgs = emails.build_messages({"ca": history("ca"), "us": history("us")}, BASE)
    assert sorted(msgs) == ["ca", "ca+us", "us"]
    combined = msgs["ca+us"]
    assert msgs["ca"]["subject"] in combined["subject"]
    assert msgs["us"]["subject"] in combined["subject"]
    for part in ("html", "text"):
        body = combined[part].lower()
        assert "canada" in body and "united states" in body
        assert combined[part].count("{{UNSUB_URL}}") == 1
        assert "Unsubscribe from these lists" in combined[part]
    assert "Unsubscribe from this list" in msgs["ca"]["html"]


def test_pipeline_sends_both_changes_in_one_call(monkeypatch):
    calls = []
    monkeypatch.setattr(emails, "send_via_proxy", lambda m, *a: calls.append(sorted(m)) or True)
    monkeypatch.setattr(emails, "flush_deferred", lambda *a: True)
    monkeypatch.setattr(emails, "refresh_lag_for_region", lambda slug: None)
    # Stored history one change behind the data for both regions.
    state = {slug: {"series": cfg["label"], "changes": history(slug)[1:]}
             for slug, cfg in emails.REGIONS.items()}
    state = emails.run_pipeline(state, "https://proxy", "key", BASE, dry_run=False)
    assert calls == [["ca", "ca+us", "us"]]
    assert all(state[s]["changes"][0] == history(s)[0] for s in emails.REGIONS)


def test_failed_broadcast_is_retried_next_run(monkeypatch):
    monkeypatch.setattr(emails, "send_via_proxy", lambda *a: False)
    monkeypatch.setattr(emails, "flush_deferred", lambda *a: True)
    monkeypatch.setattr(emails, "refresh_lag_for_region", lambda slug: None)
    behind = {slug: {"series": cfg["label"], "changes": history(slug)[1:]}
              for slug, cfg in emails.REGIONS.items()}
    state = emails.run_pipeline(dict(behind), "https://proxy", "key", BASE, dry_run=False)
    assert state == behind


def test_pipeline_without_changes_sends_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(emails, "send_via_proxy", lambda *a: calls.append(a) or True)
    monkeypatch.setattr(emails, "flush_deferred", lambda *a: True)
    state = {slug: {"series": cfg["label"], "changes": history(slug)}
             for slug, cfg in emails.REGIONS.items()}
    emails.run_pipeline(state, "https://proxy", "key", BASE, dry_run=False)
    assert calls == []
