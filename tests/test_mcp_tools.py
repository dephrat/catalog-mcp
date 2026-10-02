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
        last_synced="2016-03-02T00:00:00Z"))
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
        assert set(out["threads"][0]) == {
            "thread_id", "subject", "participants", "date_first", "date_last",
            "tags", "has_attachments", "web_link"}
        assert out["total"] >= 1

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
