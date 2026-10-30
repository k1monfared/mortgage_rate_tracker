"""Bank of Canada announcements: parsing, pending/confirmed status, the email
trigger and the email link. No network access."""

import pandas as pd
import pytest

import rates
import send_rate_emails as emails

URL = "https://www.bankofcanada.ca/2026/10/fad-press-release-2026-10-28/"
TITLE = "Bank of Canada raises the policy rate by 25 basis points to 2½%"
SUMMARY = ("The Bank of Canada today increased its target for the overnight rate "
           "by 25 basis points to 2.5%, with the Bank Rate at 2.75%.")

FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <item rdf:about="{URL}">
    <title>{TITLE}</title><link>{URL}</link>
    <description>{SUMMARY}</description><dc:date>2026-10-28T09:47:20+00:00</dc:date>
  </item>
  <item rdf:about="https://www.bankofcanada.ca/2026/09/new-20/">
    <title>Bank of Canada unveils new vertical $20 bank note</title>
    <link>https://www.bankofcanada.ca/2026/09/new-20/</link>
    <description>Not a rate.</description><dc:date>2026-09-03T10:00:00+00:00</dc:date>
  </item>
</rdf:RDF>""".encode()


@pytest.mark.parametrize("title,rate", [
    (TITLE, 2.5),
    ("Bank of Canada maintains the policy rate at 2¼%", 2.25),
    ("Bank of Canada lowers policy rate to ¼ percent", 0.25),
    ("Bank of Canada reduces policy rate by 50 basis points to 3¾%", 3.75),
    ("Bank of Canada increases policy interest rate by 75 basis points, continues quantitative tightening", None),
    ("Bank of Canada raises the policy rate to 2.13%", None),
])
def test_parse_announced_rate(title, rate):
    assert rates.parse_announced_rate(title) == rate


def test_summary_must_agree_with_title():
    assert rates.parse_announced_rate(TITLE, SUMMARY) == 2.5
    assert rates.parse_announced_rate(TITLE, SUMMARY.replace("2.5%", "2.75%")) is None


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A temp data dir with the Valet series up to the announcement day."""
    monkeypatch.setattr(rates, "DATA_DIR", tmp_path)

    class Resp:
        content = FEED

    monkeypatch.setattr(rates, "_get", lambda *a, **k: Resp())
    series = rates.get("ca", "policy")
    policy = rates._frame([("2025-10-30", 2.25), ("2026-10-28", 2.25)])
    rates.save(series, policy)
    return series, policy


def test_announcement_is_pending_until_valet_covers_the_effective_date(data):
    series, policy = data
    [a] = rates.refresh_announcements(policy)
    assert (a["announced"], a["effective"], a["rate"], a["previous"], a["status"]) == \
        ("2026-10-28", "2026-10-29", 2.5, 2.25, "pending")
    assert a["url"] == URL and a["title"] == TITLE
    assert rates.load_with_pending(series)["rate"].tolist() == [2.25, 2.25, 2.5]

    published = rates.merge(policy, rates._frame([("2026-10-29", 2.5)]))
    [a] = rates.refresh_announcements(published)
    assert a["status"] == "confirmed" and rates.pending(series) is None


def test_hold_is_not_pending(data, monkeypatch):
    _, policy = data
    monkeypatch.setattr(rates, "parse_announced_rate", lambda *a: 2.25)
    [a] = rates.refresh_announcements(policy)
    assert a["status"] == "held"


def test_feed_failure_keeps_stored_announcements(data, monkeypatch):
    _, policy = data
    rates.refresh_announcements(policy)
    monkeypatch.setattr(rates, "_get", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert [a["url"] for a in rates.refresh_announcements(policy)] == [URL]


def test_pending_change_emails_once_and_links_the_announcement(data, monkeypatch):
    series, policy = data
    rates.refresh_announcements(policy)

    state = {"ca": {"series": emails.REGIONS["ca"]["label"],
                    "changes": [{"date": "2025-10-30", "value": 2.25}]}}
    block, new = emails.detect_region("ca", state)
    assert new and block["changes"][0] == {"date": "2026-10-29", "value": 2.5}

    msg = emails.compose_email("ca", block["changes"], "https://example.com")
    for part in ("html", "text", "markdown"):
        assert URL in msg[part]
    assert "Announced by the Bank of Canada on 2026-10-28" in msg["text"]

    # Valet publishes the same change: nothing new to send.
    rates.save(series, rates.merge(policy, rates._frame([("2026-10-29", 2.5)])))
    rates.refresh_announcements(rates.load(series))
    _, new = emails.detect_region("ca", {"ca": block})
    assert not new
