"""DB-backed MCP tool logic: search_catalog, list_tags, sync_status.

mcp_tools.py is Flask-free, read-only against db.py, and resolves the acting
user itself (CATALOG_USER env var, else the sole row in users) rather than
taking a user_id argument — that's the shape the MCP server wrapper needs.
"""
import json

import pytest

import db
import mcp_tools
from conftest import make_thread


@pytest.fixture
def seeded(user):
    """A fresh db with one registered user and two tagged threads."""
    db.upsert_user(user, "owner@example.com", "Owner", "2024-01-01T00:00:00Z")
    db.upsert_thread(user, make_thread(
        "car", subject="Honda loan", ai_tags=["honda", "car", "loan"],
        user_tags=["2016"],
        date_first="2016-03-01T00:00:00Z", date_last="2016-03-01T00:00:00Z",
        has_attachments=1, web_link="https://example.test/car",
        last_synced="2016-03-02T00:00:00Z",
        attachments=[{"name": "loan-statement.pdf"}, {"name": "title.png"}]))
    db.upsert_thread(user, make_thread(
        "dentist", subject="Cleaning", ai_tags=["dentist", "teeth", "health"],
        user_tags=[],
        date_first="2020-06-01T00:00:00Z", date_last="2020-06-02T00:00:00Z",
        has_attachments=0, web_link="https://example.test/dentist",
        last_synced="2020-06-03T00:00:00Z"))
    return user


@pytest.fixture
def empty_db(user):
    """A fresh db with a registered user but no threads yet."""
    db.upsert_user(user, "owner@example.com", "Owner", "2024-01-01T00:00:00Z")
    return user


class FakeProvider:
    """Stand-in for a providers.MailProvider — no network, just scripted replies."""

    label = "FakeMail"

    def __init__(self, access_token="live-token", messages=None):
        self.access_token = access_token
        self.messages = messages if messages is not None else []
        self.get_thread_calls = []

    def refresh_token(self, token_cache):
        if not self.access_token:
            return (None, None)
        return (self.access_token, "new-cache")

    def get_thread(self, access_token, thread_id):
        self.get_thread_calls.append((access_token, thread_id))
        return self.messages


@pytest.fixture
def fake_provider(monkeypatch):
    """Patches mcp_tools.providers.get to always return this fake, whatever
    provider name provider_for() guessed."""
    fake = FakeProvider(messages=[
        {
            "id": "m1", "thread_id": "car", "subject": "Honda loan",
            "from_addr": "lender@example.com", "to_addrs": ["owner@example.com"],
            "date": "2016-03-01T00:00:00Z", "has_attachments": True,
            "body": "<p>Hello <b>there</b></p>", "web_link": "https://provider.test/m1",
            "container_id": "folder-A",
        },
    ])
    monkeypatch.setattr(mcp_tools.providers, "get", lambda name: fake)
    return fake


@pytest.fixture
def dead_provider(monkeypatch):
    """Patches mcp_tools.providers.get to return a provider whose sign-in
    has expired: refresh_token() -> (None, None)."""
    fake = FakeProvider(access_token=None)
    monkeypatch.setattr(mcp_tools.providers, "get", lambda name: fake)
    return fake


class TestResolveUser:
    def test_resolve_user_prefers_env_then_sole_user(self, monkeypatch, fresh_db):
        db.upsert_user("u1", "a@example.com", "A", "2024-01-01T00:00:00Z")

        monkeypatch.delenv("CATALOG_USER", raising=False)
        assert mcp_tools.resolve_user() == "u1"

        monkeypatch.setenv("CATALOG_USER", "a@example.com")
        assert mcp_tools.resolve_user() == "u1"

    def test_resolve_user_raises_when_ambiguous(self, monkeypatch, fresh_db):
        monkeypatch.delenv("CATALOG_USER", raising=False)
        db.upsert_user("u1", "a@example.com", "A", "2024-01-01T00:00:00Z")
        db.upsert_user("u2", "b@example.com", "B", "2024-01-01T00:00:00Z")

        with pytest.raises(ValueError):
            mcp_tools.resolve_user()

    def test_resolve_user_rejects_catalog_user_matching_no_user(
            self, monkeypatch, fresh_db):
        """A CATALOG_USER that doesn't match any users.user_id row must be a
        loud misconfiguration (ValueError), not a silent empty catalog."""
        db.upsert_user("u1", "a@example.com", "A", "2024-01-01T00:00:00Z")
        monkeypatch.setenv("CATALOG_USER", "nobody@example.com")

        with pytest.raises(ValueError, match="no such user: nobody@example.com"):
            mcp_tools.resolve_user()

    def test_resolve_user_on_nonexistent_db_raises_no_catalog_error(
            self, monkeypatch, tmp_path):
        """DB_PATH pointing at a path nothing has ever written to (no file,
        no schema) must read as "no catalog yet", not a raw
        sqlite3.OperationalError('no such table: users')."""
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "never-created.db"))
        monkeypatch.delenv("CATALOG_USER", raising=False)

        with pytest.raises(mcp_tools.NoCatalogError):
            mcp_tools.resolve_user()

    def test_resolve_user_on_schema_initialised_but_userless_db_raises_no_catalog_error(
            self, monkeypatch, fresh_db):
        """init_db() ran (schema exists) but nobody has signed in yet: still
        "no catalog", not "multiple users; set CATALOG_USER"."""
        monkeypatch.delenv("CATALOG_USER", raising=False)

        with pytest.raises(mcp_tools.NoCatalogError):
            mcp_tools.resolve_user()


class TestProviderFor:
    def test_gmail_cache_detected_from_refresh_token_key(self, user, fresh_db):
        db.upsert_user(user, "a@example.com", "A", "2024-01-01T00:00:00Z")
        db.set_token_cache(user, json.dumps({"refresh_token": "rt", "other": 1}))
        assert mcp_tools.provider_for(user) == "gmail"

    def test_non_gmail_cache_detected_as_microsoft(self, user, fresh_db):
        db.upsert_user(user, "a@example.com", "A", "2024-01-01T00:00:00Z")
        db.set_token_cache(user, "some-opaque-msal-cache-blob")
        assert mcp_tools.provider_for(user) == "microsoft"

    def test_no_cache_is_empty_string(self, user, fresh_db):
        db.upsert_user(user, "a@example.com", "A", "2024-01-01T00:00:00Z")
        assert mcp_tools.provider_for(user) == ""


class TestSearchCatalog:
    def test_search_returns_lean_rows_and_total(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        out = mcp_tools.search_catalog(query="dentist")
        row = out["threads"][0]
        assert set(row) == {
            "thread_id", "subject", "participants", "date_first", "date_last",
            "tags", "has_attachments", "web_link"}
        # Field shapes/types, not just key names: a prior bug left
        # participants as a JSON-encoded string instead of a parsed list.
        assert row["thread_id"] == "dentist"
        assert row["subject"] == "Cleaning"
        assert isinstance(row["participants"], list)
        assert isinstance(row["date_first"], str)
        assert isinstance(row["tags"], list)
        assert isinstance(row["has_attachments"], bool)
        assert isinstance(row["web_link"], str)
        assert out["total"] >= 1

    def test_participants_parsed_into_list_not_json_string(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        out = mcp_tools.search_catalog(query="honda")
        assert out["threads"][0]["participants"] == [
            "owner@example.com", "sender@example.com"]

    def test_search_on_nonexistent_db_says_empty_catalog(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "never-created.db"))
        monkeypatch.delenv("CATALOG_USER", raising=False)

        out = mcp_tools.search_catalog(query="x")

        assert out == {"threads": [], "total": 0, "notice": mcp_tools.EMPTY_CATALOG_NOTICE}

    def test_tags_merge_ai_and_user_tags_deduped(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        out = mcp_tools.search_catalog(query="honda")
        row = out["threads"][0]
        assert set(row["tags"]) == {"honda", "car", "loan", "2016"}

    def test_search_empty_catalog_says_so(self, empty_db, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        assert "run sync_cli.py" in mcp_tools.search_catalog(query="x")["notice"]

    def test_search_survives_fts_metacharacters(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        # Must not raise sqlite3.OperationalError.
        mcp_tools.search_catalog(query='"unclosed -NEAR( *')

    def test_match_any_uses_or_search_mode(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        out = mcp_tools.search_catalog(query="honda dentist", match_any=True)
        assert out["total"] == 2

    def test_has_attachments_filter_passed_through(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        out = mcp_tools.search_catalog(has_attachments=True)
        assert out["total"] == 1
        assert out["threads"][0]["thread_id"] == "car"


class TestListTags:
    def test_list_tags_returns_counts_matching_prefix(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        out = mcp_tools.list_tags(prefix="de")
        assert out["tags"] == [{"tag": "dentist", "count": 1}]

    def test_list_tags_counts_across_all_threads_without_prefix(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        out = mcp_tools.list_tags()
        by_tag = {t["tag"]: t["count"] for t in out["tags"]}
        assert by_tag["honda"] == 1
        assert by_tag["dentist"] == 1

    def test_list_tags_empty_catalog_says_so(self, empty_db, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        assert "run sync_cli.py" in mcp_tools.list_tags()["notice"]

    def test_list_tags_on_nonexistent_db_says_empty_catalog(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "never-created.db"))
        monkeypatch.delenv("CATALOG_USER", raising=False)

        assert mcp_tools.list_tags() == {
            "tags": [], "notice": mcp_tools.EMPTY_CATALOG_NOTICE}

    def test_list_tags_dedupes_tag_shared_by_both_columns_on_one_thread(
            self, seeded, monkeypatch):
        """A tag present in both ai_tags and user_tags on the same thread
        must count that thread once, not twice — the UNION (not UNION ALL)
        between the ai_tags pass and the user_tags pass is what guarantees
        this."""
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        db.upsert_thread(seeded, make_thread(
            "both-cols", subject="Overlap", ai_tags=["shared"],
            user_tags=["shared"]))

        out = mcp_tools.list_tags(prefix="shared")

        assert out["tags"] == [{"tag": "shared", "count": 1}]


class TestTagNames:
    def test_tag_names_returns_names_only_matching_prefix(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        assert mcp_tools.tag_names(prefix="de") == ["dentist"]

    def test_tag_names_without_prefix_returns_all_names(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        assert set(mcp_tools.tag_names()) == {
            "honda", "car", "loan", "2016", "dentist", "teeth", "health"}

    def test_tag_names_no_match_returns_empty_list(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        assert mcp_tools.tag_names(prefix="zzz") == []

    def test_tag_names_on_empty_catalog_returns_empty_list(self, empty_db, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        assert mcp_tools.tag_names() == []

    def test_tag_names_respects_limit(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        assert len(mcp_tools.tag_names(limit=2)) == 2


class TestSyncStatus:
    def test_sync_status_reports_counts_and_last_synced(self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        out = mcp_tools.sync_status()
        assert out["thread_count"] == 2
        assert out["untagged_count"] == 0
        assert out["last_synced"] == "2020-06-03T00:00:00Z"
        assert out["provider"] == ""

    def test_sync_status_empty_catalog(self, empty_db, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        out = mcp_tools.sync_status()
        assert out["thread_count"] == 0
        assert out["untagged_count"] == 0
        assert out["last_synced"] is None
        # A registered user with zero threads still gets the same empty-
        # catalog notice the other tools use, not just zeroed counts.
        assert out["notice"] == mcp_tools.EMPTY_CATALOG_NOTICE

    def test_sync_status_on_nonexistent_db_says_empty_catalog(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "never-created.db"))
        monkeypatch.delenv("CATALOG_USER", raising=False)

        out = mcp_tools.sync_status()

        assert out == {
            "last_synced": None,
            "thread_count": 0,
            "untagged_count": 0,
            "provider": "",
            "notice": mcp_tools.EMPTY_CATALOG_NOTICE,
        }


class TestGetThread:
    def test_get_thread_fetches_live_and_strips_html(
            self, seeded, fake_provider, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")

        out = mcp_tools.get_thread("car")

        assert out["subject"] == "Honda loan"
        assert out["web_link"] == "https://example.test/car"
        assert out["attachments"] == ["loan-statement.pdf", "title.png"]
        assert out["messages"] == [{
            "from": "lender@example.com",
            "to": ["owner@example.com"],
            "date": "2016-03-01T00:00:00Z",
            "body_text": "Hello there",
        }]
        assert "truncated" not in out
        assert fake_provider.get_thread_calls == [("live-token", "car")]

    def test_get_thread_unknown_id_errors_without_provider_call(
            self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")

        def boom(name):
            raise AssertionError("providers.get must not be called for an unknown thread")

        monkeypatch.setattr(mcp_tools.providers, "get", boom)

        out = mcp_tools.get_thread("no-such-thread")

        assert "error" in out
        assert "no-such-thread" in out["error"]

    def test_get_thread_other_users_thread_errors_without_provider_call(
            self, seeded, monkeypatch):
        """A thread_id that exists, but for a different user, must read as
        not-found rather than leaking cross-user existence."""
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")

        def boom(name):
            raise AssertionError("providers.get must not be called for another user's thread")

        monkeypatch.setattr(mcp_tools.providers, "get", boom)

        db.upsert_user("other-user", "other@example.com", "Other", "2024-01-01T00:00:00Z")
        db.upsert_thread("other-user", make_thread("only-others", subject="Secret"))

        out = mcp_tools.get_thread("only-others")

        assert "error" in out

    def test_get_thread_expired_signin_gives_reauth_message(
            self, seeded, dead_provider, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")

        out = mcp_tools.get_thread("car")

        assert "error" in out
        assert "sign" in out["error"].lower()
        assert "Catalog web app" in out["error"]
        assert not dead_provider.get_thread_calls

    def test_get_thread_on_nonexistent_db_says_empty_catalog(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "never-created.db"))
        monkeypatch.delenv("CATALOG_USER", raising=False)

        out = mcp_tools.get_thread("whatever")

        assert out == {"error": mcp_tools.EMPTY_CATALOG_NOTICE}

    def test_get_thread_provider_network_failure_is_caught(
            self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")

        class BoomProvider(FakeProvider):
            def get_thread(self, access_token, thread_id):
                raise ConnectionError("provider unreachable")

        monkeypatch.setattr(mcp_tools.providers, "get", lambda name: BoomProvider())

        out = mcp_tools.get_thread("car")

        assert out == {"error": "provider fetch failed: provider unreachable"}

    def test_get_thread_attachment_without_name_key_does_not_raise(
            self, seeded, fake_provider, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        db.upsert_thread(seeded, make_thread(
            "no-name-attachment", subject="Odd attachment",
            attachments=[{"size": 123}]))

        out = mcp_tools.get_thread("no-name-attachment")

        assert out["attachments"] == [""]

    def test_get_thread_truncates_at_50k_and_flags_it(
            self, seeded, monkeypatch):
        monkeypatch.setenv("CATALOG_USER", "owner@example.com")
        huge = FakeProvider(messages=[
            {
                "id": "m1", "thread_id": "car", "subject": "Honda loan",
                "from_addr": "lender@example.com", "to_addrs": ["owner@example.com"],
                "date": "2016-03-01T00:00:00Z", "has_attachments": False,
                "body": "a" * 60_000, "web_link": "https://provider.test/m1",
                "container_id": "folder-A",
            },
        ])
        monkeypatch.setattr(mcp_tools.providers, "get", lambda name: huge)

        out = mcp_tools.get_thread("car")

        assert out["truncated"] is True
        assert len(out["messages"][0]["body_text"]) == 50_000
