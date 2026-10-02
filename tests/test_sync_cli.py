"""sync_cli.py: run a mailbox sync from the command line.

No network and no real sync here — app.run_sync and the provider's token
refresh are monkeypatched. These tests only verify the CLI's own plumbing:
which user it resolves, whether it guards against a sync already running,
how it reacts to a dead refresh token, and what it hands to run_sync.
"""
import time

import pytest

import app
import db
import providers
import sync_cli


class FakeProvider:
    """Stand-in for a providers.MailProvider — scripted refresh, no network."""

    name = "fake"
    label = "FakeMail"

    def __init__(self, access_token="fresh-token", new_cache="new-cache"):
        self.access_token = access_token
        self.new_cache = new_cache
        self.refresh_calls = []

    def refresh_token(self, token_cache):
        self.refresh_calls.append(token_cache)
        if not self.access_token:
            return (None, None)
        return (self.access_token, self.new_cache)


@pytest.fixture
def seeded(user, monkeypatch):
    """A registered user with a stored (non-JSON) token cache, email known."""
    email = "owner@example.com"
    db.upsert_user(user, email, "Owner", "2024-01-01T00:00:00Z")
    db.set_token_cache(user, "msal-cache-blob")
    monkeypatch.delenv("CATALOG_USER", raising=False)
    return user, email


@pytest.fixture(autouse=True)
def clean_sync_state():
    """app.sync_running is process-global; never leak a claim between tests."""
    yield
    app.sync_running.clear()
    app.detective_running.clear()


def _patch_run_sync(monkeypatch):
    calls = []

    def fake_run_sync(user_id, provider, token, token_cache=None):
        calls.append((user_id, provider, token, token_cache))

    monkeypatch.setattr(app, "run_sync", fake_run_sync)
    return calls


def test_happy_path_calls_run_sync_with_refreshed_token(seeded, monkeypatch):
    user_id, email = seeded
    fake_provider = FakeProvider(access_token="fresh-token", new_cache="new-cache")
    monkeypatch.setattr(providers, "get", lambda name: fake_provider)
    calls = _patch_run_sync(monkeypatch)

    rc = sync_cli.main(["--user", email])

    assert rc == 0
    assert calls == [(user_id, fake_provider, "fresh-token", "msal-cache-blob")]
    assert db.get_token_cache(user_id) == "new-cache"


def test_dead_refresh_exits_2_with_sign_in_message(seeded, monkeypatch, capsys):
    user_id, email = seeded
    fake_provider = FakeProvider(access_token=None)
    monkeypatch.setattr(providers, "get", lambda name: fake_provider)
    calls = _patch_run_sync(monkeypatch)

    rc = sync_cli.main(["--user", email])

    assert rc == 2
    assert calls == []
    err = capsys.readouterr().err.lower()
    assert "sign" in err and "web app" in err


def test_already_running_exits_3_without_calling_run_sync(seeded, monkeypatch):
    """Exercises the in-process simulation only: app.sync_running is a
    per-process dict, so this proves the guard works within one process, not
    that it catches a sync the web app started in a different process."""
    user_id, email = seeded
    fake_provider = FakeProvider()
    monkeypatch.setattr(providers, "get", lambda name: fake_provider)
    calls = _patch_run_sync(monkeypatch)
    app.set_running(app.sync_running, user_id, True)

    rc = sync_cli.main(["--user", email])

    assert rc == 3
    assert calls == []
    assert fake_provider.refresh_calls == []


def test_no_stored_credentials_exits_2(user, monkeypatch, capsys):
    """Never signed in via the web app: no token cache at all."""
    email = "nocache@example.com"
    db.upsert_user(user, email, "Owner", "2024-01-01T00:00:00Z")
    monkeypatch.delenv("CATALOG_USER", raising=False)
    calls = _patch_run_sync(monkeypatch)

    rc = sync_cli.main(["--user", email])

    assert rc == 2
    assert calls == []
    err = capsys.readouterr().err.lower()
    assert "sign" in err and "web app" in err
