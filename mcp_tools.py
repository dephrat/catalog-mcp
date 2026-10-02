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

import db

EMPTY_CATALOG_NOTICE = (
    "catalog is empty — run sync_cli.py to index your mailbox before searching"
)


def resolve_user():
    """Figure out which user this process acts on behalf of.

    CATALOG_USER (an email address) wins when set. Otherwise, a single-user
    deployment just works: the sole row in users is used. Multiple users
    with no CATALOG_USER set is refused rather than guessed at.
    """
    env_user = os.environ.get("CATALOG_USER")
    if env_user:
        found = db.get_user_by_email(env_user)
        return found["user_id"] if found else env_user

    conn = db.get_db()
    try:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
    finally:
        conn.close()

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


def _lean_row(row):
    return {
        "thread_id": row["thread_id"],
        "subject": row["subject"],
        "participants": row["participants"],
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
    count. On an empty catalog (nothing synced yet) returns a "notice"
    key instead of pretending there's simply nothing to find.
    """
    user_id = resolve_user()
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
    user_id = resolve_user()
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

    Fast: local DB only, no network.
    """
    user_id = resolve_user()
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

    return {
        "last_synced": last_synced,
        "thread_count": thread_count,
        "untagged_count": untagged_count,
        "provider": provider_for(user_id),
    }
