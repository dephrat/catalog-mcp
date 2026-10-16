"""The in-app thread reader: /threads/<id>/read.

Bodies are never stored, so the reader live-fetches through the provider.
The catalog row is the access gate: ids outside the signed-in user's
catalog must 404 before any provider call.
"""
import pytest

import app as appmod
import db
from conftest import make_thread


@pytest.fixture
def client(fresh_db, monkeypatch):
    db.upsert_user("u1", "owner@example.com", "Owner", "2024-01-01T00:00:00Z")
    db.upsert_thread("u1", make_thread(
        "conv-1", subject="Loan papers", web_link="https://mail.example/conv-1"))
    db.set_token_cache("u1", '{"refresh_token": "rt"}')
    c = appmod.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "owner@example.com"
        s["provider"] = "gmail"
    return c


class FakeProvider:
    label = "FakeMail"

    def __init__(self, messages=None, token="tok", boom=False):
        self.messages = messages or []
        self.token = token
        self.boom = boom
        self.calls = []

    def refresh_token(self, cache):
        return (self.token, cache)

    def get_thread(self, token, thread_id):
        if self.boom:
            raise RuntimeError("network down")
        self.calls.append(thread_id)
        return self.messages


def test_renders_every_message_body_in_order(client, monkeypatch):
    fake = FakeProvider(messages=[
        {"from_addr": "b@example.com", "to_addrs": ["owner@example.com"],
         "date": "2024-03-02T10:00:00Z", "body": "<p>second &amp; final</p>",
         "has_attachments": True, "web_link": "https://mail.example/m2"},
        {"from_addr": "a@example.com", "to_addrs": ["owner@example.com"],
         "date": "2024-03-01T10:00:00Z", "body": "first message",
         "has_attachments": False, "web_link": "https://mail.example/m1"},
    ])
    monkeypatch.setattr(appmod.providers, "get", lambda name=None: fake)

    resp = client.get("/threads/conv-1/read")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Loan papers" in html
    # bodies present, html stripped, chronological order
    assert "first message" in html
    assert "second &amp; final" in html and "<p>" not in html.split("Loan papers")[1]
    assert html.index("first message") < html.index("second")
    assert "a@example.com" in html and "2024-03-01 10:00" in html


def test_unknown_thread_404s_without_provider_call(client, monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(appmod.providers, "get", lambda name=None: fake)
    resp = client.get("/threads/nope/read")
    assert resp.status_code == 404
    assert fake.calls == []


def test_other_users_thread_404s(client, monkeypatch):
    db.upsert_user("u2", "other@example.com", "Other", "2024-01-01T00:00:00Z")
    db.upsert_thread("u2", make_thread("conv-2", subject="Not yours"))
    fake = FakeProvider()
    monkeypatch.setattr(appmod.providers, "get", lambda name=None: fake)
    resp = client.get("/threads/conv-2/read")
    assert resp.status_code == 404
    assert fake.calls == []


def test_expired_signin_asks_to_sign_in_again(client, monkeypatch):
    fake = FakeProvider(token=None)
    monkeypatch.setattr(appmod.providers, "get", lambda name=None: fake)
    resp = client.get("/threads/conv-1/read")
    assert resp.status_code == 401
    assert b"sign in again" in resp.data
    assert fake.calls == []


def test_provider_failure_is_a_clean_502(client, monkeypatch):
    fake = FakeProvider(boom=True)
    monkeypatch.setattr(appmod.providers, "get", lambda name=None: fake)
    resp = client.get("/threads/conv-1/read")
    assert resp.status_code == 502
    assert b"try again" in resp.data
