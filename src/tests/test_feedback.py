"""The feedback button's endpoint: public, emailed (never stored), limited."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi.testclient import TestClient

from main import app
from routes import feedback
from services import email


@pytest.fixture
def client(monkeypatch):
    feedback._hits.clear()
    monkeypatch.setenv("FEEDBACK_TO", "team@example.org, second@example.org")
    yield TestClient(app)
    feedback._hits.clear()


@pytest.fixture
def mailed(monkeypatch):
    sent = []

    def fake(to, subject, body, reply_to=None):
        sent.append({"to": to, "subject": subject, "body": body, "reply_to": reply_to})
        return True

    monkeypatch.setattr(email, "send_email", fake)
    return sent


def test_anyone_can_send_without_signing_in_and_every_recipient_gets_it(client, mailed):
    r = client.post("/feedback", json={"type": "bug", "message": "The map does not load", "page": "/dashboard/map"})
    assert r.status_code == 200 and r.json() == {"status": "received"}
    assert [m["to"] for m in mailed] == ["team@example.org", "second@example.org"]
    first = mailed[0]
    assert "Error: The map does not load" in first["subject"]
    assert "Page: /dashboard/map" in first["body"] and "Type: Error" in first["body"]
    assert first["reply_to"] is None


def test_the_contact_email_becomes_the_reply_to_and_is_in_the_body(client, mailed):
    r = client.post("/feedback", json={"message": "Please add a filter", "contact_email": " me@example.org "})
    assert r.status_code == 200
    assert mailed[0]["reply_to"] == "me@example.org"
    assert "Reply to: me@example.org" in mailed[0]["body"] and "not signed in" in mailed[0]["body"]


def test_who_is_writing_is_not_recorded_without_a_contact_email(client, mailed):
    client.post("/feedback", json={"message": "No reply needed here"})
    assert "Account:" not in mailed[0]["body"] and "Reply to:" not in mailed[0]["body"]


@pytest.mark.parametrize(
    "body",
    [
        {"message": "   hi "},
        {"message": "x" * (feedback.MAX_MESSAGE + 1)},
        {"message": "A real message", "contact_email": "not an email"},
        {"message": "A real message", "contact_email": "a@b.org\nBcc: victim@example.org"},
        {"message": "A real message", "type": "complaint"},
    ],
)
def test_bad_input_is_refused_and_nothing_is_sent(client, mailed, body):
    assert client.post("/feedback", json=body).status_code == 422
    assert mailed == []


def test_page_and_language_are_kept_to_one_line(client, mailed):
    client.post("/feedback", json={"message": "A real message", "page": "/a\nBcc: x@example.org", "language": "es"})
    body = mailed[0]["body"]
    assert "Page: /a Bcc: x@example.org" in body and "\nBcc:" not in body


def test_the_honeypot_looks_like_a_success_but_sends_nothing(client, mailed):
    r = client.post("/feedback", json={"message": "Buy cheap watches", "website": "http://spam.example"})
    assert r.status_code == 200 and mailed == []


def test_a_caller_is_limited_per_hour(client, mailed):
    limit = feedback.PER_CALLER[0]
    codes = [client.post("/feedback", json={"message": f"message number {i}"}).status_code for i in range(limit + 1)]
    assert codes == [200] * limit + [429]
    assert len(mailed) == limit * 2


def test_anonymous_callers_share_a_global_budget(client, mailed):
    total = feedback.ANONYMOUS_TOTAL[0]
    codes = [
        client.post("/feedback", json={"message": f"message number {i}"}, headers={"X-Forwarded-For": f"10.0.{i}.1"}).status_code
        for i in range(total + 1)
    ]
    assert codes == [200] * total + [429]


def test_without_recipients_it_says_so_instead_of_pretending(client, mailed, monkeypatch):
    monkeypatch.delenv("FEEDBACK_TO")
    assert client.post("/feedback", json={"message": "A real message"}).status_code == 503
    assert mailed == []


def test_a_failed_send_is_an_error_when_smtp_is_configured(client, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setattr(email, "send_email", lambda *a, **k: False)
    assert client.post("/feedback", json={"message": "A real message"}).status_code == 502


def test_without_smtp_it_is_accepted_and_kept_in_the_log(client, monkeypatch, caplog):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with caplog.at_level("WARNING", logger="mfl.feedback"):
        r = client.post("/feedback", json={"message": "Kept in the log"})
    assert r.status_code == 200 and "Kept in the log" in caplog.text
