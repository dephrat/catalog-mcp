"""DB-backed logic for the Catalog MCP server.

Flask-free and read-only: these functions never write to the database or
trigger a sync — that split is deliberate so the MCP server (an agent-facing
surface) cannot be tricked into mutating a mailbox index or kicking off a
sync that double-runs alongside the web app's own. Each function resolves
the acting user itself via resolve_user(), so callers (the FastMCP tool
wrappers in mcp_server.py) never pass a user_id.
"""
import json
import os
import re
import sqlite3

import db
import providers

EMPTY_CATALOG_NOTICE = (
    "catalog is empty — run sync_cli.py to index your mailbox before searching"
)

BODY_CHAR_CAP = 50_000


class NoCatalogError(Exception):
    """The database has no schema yet (DB_PATH points at a nonexistent or
    just-created file, or migrations never ran). resolve_user() raises this
    instead of letting sqlite3's "no such table" escape, so every tool can
    report the same empty-catalog notice a never-synced-but-initialised
    database gets, rather than a raw OperationalError."""


def resolve_user():
    """Figure out which user this process acts on behalf of.

    CATALOG_USER (an email address) wins when set, but only if it names a
    real user — otherwise a typo'd or stale CATALOG_USER would silently read
    as an empty catalog instead of the misconfiguration it is. With no
    CATALOG_USER, a single-user deployment just works: the sole row in users
    is used. Multiple users with no CATALOG_USER set is refused rather than
    guessed at. A database with no schema (or no users) yet raises
    NoCatalogError rather than either of the above.
    """
    env_user = os.environ.get("CATALOG_USER")
    try:
        if env_user:
            found = db.get_user_by_email(env_user)
            if found:
                return found["user_id"]
            raise ValueError(f"no such user: {env_user}")

        conn = db.get_db()
        try:
            rows = conn.execute("SELECT user_id FROM users").fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        raise NoCatalogError()

    if not rows:
        raise NoCatalogError()
    if len(rows) == 1:
        return rows[0]["user_id"]
    raise ValueError("multiple users; set CATALOG_USER")


def provider_for(user_id):
    """Guess the mail provider from the stored token cache.

    The users table has no provider column — the web app only ever keeps
    that in the Flask session, which this server doesn't have. Gmail's
    cache is a JSON blob with a top-level "refresh_token" key; Microsoft's
    MSAL cache is some other non-empty blob; no cache at all means the user
    has never signed in (or signed out).
    """
    cache = db.get_token_cache(user_id)
    if not cache:
        return ""
    try:
        parsed = json.loads(cache)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict) and "refresh_token" in parsed:
        return "gmail"
    return "microsoft"


def _merged_tags(row):
    """ai_tags and user_tags are separate JSON lists; tools see one set."""
    try:
        ai_tags = json.loads(row["ai_tags"] or "[]")
    except (ValueError, TypeError):
        ai_tags = []
    try:
        user_tags = json.loads(row["user_tags"] or "[]")
    except (ValueError, TypeError):
        user_tags = []
    seen = set()
    merged = []
    for tag in ai_tags + user_tags:
        if tag not in seen:
            seen.add(tag)
            merged.append(tag)
    return merged


def _parse_json_list(value):
    try:
        parsed = json.loads(value or "[]")
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _lean_row(row):
    return {
        "thread_id": row["thread_id"],
        "subject": row["subject"],
        "participants": _parse_json_list(row["participants"]),
        "date_first": row["date_first"],
        "date_last": row["date_last"],
        "tags": _merged_tags(row),
        "has_attachments": bool(row["has_attachments"]),
        "web_link": row["web_link"],
    }


def search_catalog(query="", from_addr="", date_from="", date_to="",
                    has_attachments=None, match_any=False, limit=20):
    """Search the local tag index. Fast: local DB only, no network.

    Returns a lean projection of matching threads plus the total match
    count. On an empty catalog (nothing synced yet, or no schema at all)
    returns a "notice" key instead of pretending there's simply nothing to
    find.
    """
    try:
        user_id = resolve_user()
    except NoCatalogError:
        return {"threads": [], "total": 0, "notice": EMPTY_CATALOG_NOTICE}
    if db.count_threads(user_id) == 0:
        return {"threads": [], "total": 0, "notice": EMPTY_CATALOG_NOTICE}

    combined_query = " ".join(part for part in (query, from_addr) if part)
    rows, total = db.search_threads(
        user_id,
        query=combined_query or None,
        has_attachments=has_attachments,
        date_from=date_from or None,
        date_to=date_to or None,
        search_mode="or" if match_any else "and",
        limit=limit,
        with_total=True,
    )
    return {"threads": [_lean_row(r) for r in rows], "total": total}


def list_tags(prefix="", limit=50):
    """List tags in use, with counts, optionally filtered by prefix.

    Fast: one SQL pass over the user's threads (json_each over the merged
    ai_tags/user_tags columns), no network.
    """
    try:
        user_id = resolve_user()
    except NoCatalogError:
        return {"tags": [], "notice": EMPTY_CATALOG_NOTICE}
    if db.count_threads(user_id) == 0:
        return {"tags": [], "notice": EMPTY_CATALOG_NOTICE}

    conn = db.get_db()
    try:
        rows = conn.execute(
            """
            SELECT tag, COUNT(*) c FROM (
                SELECT DISTINCT thread_id, json_each.value AS tag
                  FROM threads, json_each(ai_tags)
                 WHERE user_id = ?
                UNION
                SELECT DISTINCT thread_id, json_each.value AS tag
                  FROM threads, json_each(user_tags)
                 WHERE user_id = ?
            )
            WHERE tag LIKE ? ESCAPE '\\'
            GROUP BY tag
            ORDER BY c DESC, tag ASC
            LIMIT ?
            """,
            (user_id, user_id,
             (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
             if prefix else "%",
             limit),
        ).fetchall()
    finally:
        conn.close()

    return {"tags": [{"tag": r["tag"], "count": r["c"]} for r in rows]}


def sync_status():
    """Report catalog health: thread/untagged counts, last sync, provider.

    Fast: local DB only, no network. On an empty catalog (nothing synced
    yet, or no schema at all) also carries the same "notice" key the other
    tools use, alongside the (zero) counts.
    """
    try:
        user_id = resolve_user()
    except NoCatalogError:
        return {
            "last_synced": None,
            "thread_count": 0,
            "untagged_count": 0,
            "provider": "",
            "notice": EMPTY_CATALOG_NOTICE,
        }

    thread_count = db.count_threads(user_id)
    untagged_count = db.count_untagged(user_id)

    last_synced = None
    if thread_count:
        conn = db.get_db()
        try:
            row = conn.execute(
                "SELECT MAX(last_synced) m FROM threads WHERE user_id=?",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        last_synced = row["m"] if row else None

    result = {
        "last_synced": last_synced,
        "thread_count": thread_count,
        "untagged_count": untagged_count,
        "provider": provider_for(user_id),
    }
    if thread_count == 0:
        result["notice"] = EMPTY_CATALOG_NOTICE
    return result


def _strip_html(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def get_thread(thread_id):
    """Fetch a thread's full content live from the mail provider.

    subject/web_link/attachment names come from the local catalog row —
    they're cheap, already synced, and don't need a provider round trip.
    Message bodies are never persisted (only a char count is), so those are
    always fetched live via provider.get_thread.

    The thread_id is checked against the catalog *first*: an id that
    doesn't exist, or belongs to another user, is rejected before any
    token refresh or provider call — no network, no leaking which ids
    exist for other accounts. A refresh_token() that comes back empty
    (expired/revoked sign-in) is reported as a clear, actionable error
    instead of surfacing whatever opaque failure the provider API itself
    would raise downstream.
    """
    try:
        user_id = resolve_user()
    except NoCatalogError:
        return {"error": EMPTY_CATALOG_NOTICE}

    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT subject, web_link, attachments FROM threads "
            "WHERE user_id=? AND thread_id=?",
            (user_id, thread_id),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return {"error": f"no such thread: {thread_id!r}"}

    try:
        attachments = [a.get("name", "") for a in json.loads(row["attachments"] or "[]")]
    except (ValueError, TypeError):
        attachments = []

    provider = providers.get(provider_for(user_id))
    token_cache = db.get_token_cache(user_id)
    access_token, _ = provider.refresh_token(token_cache)
    if not access_token:
        return {
            "error": f"{provider.label} sign-in expired — please sign in "
                     "again via the Catalog web app."
        }

    try:
        messages = provider.get_thread(access_token, thread_id)
    except Exception as e:
        return {"error": f"provider fetch failed: {e}"}

    out_messages = []
    remaining = BODY_CHAR_CAP
    truncated = False
    for msg in messages:
        body_text = _strip_html(msg.get("body", "") or "")
        if len(body_text) > remaining:
            body_text = body_text[:remaining]
            truncated = True
            remaining = 0
        else:
            remaining -= len(body_text)
        out_messages.append({
            "from": msg.get("from_addr", ""),
            "to": msg.get("to_addrs", []),
            "date": msg.get("date", ""),
            "body_text": body_text,
        })

    result = {
        "subject": row["subject"],
        "web_link": row["web_link"],
        "messages": out_messages,
        "attachments": attachments,
    }
    if truncated:
        result["truncated"] = True
    return result
