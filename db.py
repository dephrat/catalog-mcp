import sqlite3
import json
import os
import time

DB_PATH = os.environ.get("DB_PATH", "catalog.db")

# Rows that predate per-user scoping are parked here until claimed.
# See claim_legacy.py.
LEGACY_USER_ID = "__legacy__"


def get_db():
    # timeout: wait out a concurrent writer instead of raising 'database is locked'.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _set_journal_mode(conn):
    """Write-ahead logging, set once and stored in the database file.

    A sync writes threads from several worker threads at once. Under the
    default rollback journal a writer takes an exclusive lock, and a second
    writer can get SQLITE_BUSY immediately rather than waiting out the
    timeout — which surfaced as 'database is locked' the moment maintaining
    the full-text index made each write longer. WAL lets readers proceed
    during a write and makes writers queue properly.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error as e:      # a read-only or networked filesystem
        print(f"Could not enable WAL ({e}); continuing with the default journal.")


def _columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def init_db():
    conn = get_db()
    _set_journal_mode(conn)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            email TEXT,
            display_name TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS threads (
            user_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            message_ids TEXT,
            subject TEXT,
            participants TEXT,
            date_first TEXT,
            date_last TEXT,
            has_attachments INTEGER DEFAULT 0,
            attachments TEXT,
            attachments_scanned INTEGER DEFAULT 0,
            web_link TEXT,
            ai_tags TEXT,
            user_tags TEXT,
            manually_reviewed INTEGER DEFAULT 0,
            last_synced TEXT,
            body_char_count INTEGER,
            body_scan_status TEXT DEFAULT 'ok',
            tags_truncated INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, thread_id)
        )
    """)
    # Extracted attachment text is deliberately NOT persisted: measured churn
    # was ~9 threads/quarter, which does not justify storing document contents
    # at rest. Drop the table if an earlier build created it.
    conn.execute("DROP TABLE IF EXISTS attachment_text")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS access_requests (
            user_id TEXT PRIMARY KEY,
            email TEXT,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT,
            decided_at TEXT,
            denied_until TEXT,
            notified_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            user_id TEXT NOT NULL,
            day TEXT NOT NULL,
            batched INTEGER NOT NULL DEFAULT 0,
            requests INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day, batched)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (user_id, key)
        )
    """)
    conn.commit()

    _migrate_to_multi_user(conn)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_threads_user ON threads(user_id, last_synced)")
    conn.commit()

    _init_fts(conn)
    conn.close()


# ── Full-text index ───────────────────────────────────────────────────────────
#
# LIKE '%term%' matched substrings, which on a real corpus is not a nuance:
# searching "car" returned 2,688 threads of which 42 actually carried the tag,
# and "man" returned 1,427 of which none did. Short queries were noise.
#
# FTS5 matches tokens instead. Availability is checked rather than assumed —
# a SQLite built without it should degrade to the old behaviour rather than
# fail every search.

FTS_AVAILABLE = False

FTS_TEXT = ("COALESCE(ai_tags,'') || ' ' || COALESCE(user_tags,'') || ' ' || "
            "COALESCE(subject,'') || ' ' || COALESCE(participants,'')")


def _init_fts(conn):
    """Create the index and the triggers that keep it honest.

    Triggers rather than maintenance inside upsert_thread: tags are also
    written by set_thread_tags and rows removed by delete_thread and wipe_db,
    and an index that silently misses one of those paths is worse than none.
    """
    global FTS_AVAILABLE
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS threads_fts USING fts5(
                text,
                tokenize = 'unicode61'
            )
        """)
    except sqlite3.OperationalError as e:
        print(f"FTS5 unavailable ({e}); search falls back to substring matching.")
        FTS_AVAILABLE = False
        return

    for stmt in (
        f"""CREATE TRIGGER IF NOT EXISTS threads_fts_ai AFTER INSERT ON threads BEGIN
              INSERT INTO threads_fts(rowid, text) VALUES (new.rowid, {FTS_TEXT
                .replace('ai_tags', 'new.ai_tags').replace('user_tags', 'new.user_tags')
                .replace('subject', 'new.subject').replace('participants', 'new.participants')});
            END""",
        f"""CREATE TRIGGER IF NOT EXISTS threads_fts_au AFTER UPDATE ON threads BEGIN
              DELETE FROM threads_fts WHERE rowid = old.rowid;
              INSERT INTO threads_fts(rowid, text) VALUES (new.rowid, {FTS_TEXT
                .replace('ai_tags', 'new.ai_tags').replace('user_tags', 'new.user_tags')
                .replace('subject', 'new.subject').replace('participants', 'new.participants')});
            END""",
        """CREATE TRIGGER IF NOT EXISTS threads_fts_ad AFTER DELETE ON threads BEGIN
              DELETE FROM threads_fts WHERE rowid = old.rowid;
            END""",
    ):
        conn.execute(stmt)
    conn.commit()

    FTS_AVAILABLE = True
    _backfill_fts(conn)


def _backfill_fts(conn):
    """Populate the index for rows that predate it."""
    indexed = conn.execute("SELECT COUNT(*) c FROM threads_fts").fetchone()["c"]
    total = conn.execute("SELECT COUNT(*) c FROM threads").fetchone()["c"]
    if indexed >= total or total == 0:
        return
    print(f"Building the full-text index over {total:,} threads...")
    conn.execute("DELETE FROM threads_fts")
    conn.execute(f"INSERT INTO threads_fts(rowid, text) "
                 f"SELECT rowid, {FTS_TEXT} FROM threads")
    conn.commit()
    print("Full-text index ready.")


def has_fts(conn):
    """Whether this database carries the index.

    Asked of the connection rather than a module flag: DB_PATH can point at
    different databases within one process, and a flag set by init_db is
    wrong for any of them that has not been initialised — which fails in the
    worst possible direction, silently restoring the substring matching this
    replaced.
    """
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads_fts'"
    ).fetchone() is not None


def fts_query(words, search_mode):
    """Turn user words into an FTS5 MATCH expression.

    Every term is quoted, which makes it a literal: otherwise a search for
    "and", "or", "not" or a word containing a hyphen would be read as query
    syntax and either error or mean something the user did not type.
    """
    quoted = ['"' + w.replace('"', '""') + '"' for w in words if w]
    if not quoted:
        return None
    joiner = " OR " if search_mode == "or" else " AND "
    return joiner.join(quoted)


def _migrate_to_multi_user(conn):
    """Add user_id to pre-existing single-tenant tables, parking old rows as legacy."""
    if "user_id" not in _columns(conn, "threads"):
        print("Migrating threads to per-user schema...")
        conn.execute("ALTER TABLE threads RENAME TO threads_old")
        conn.execute("""
            CREATE TABLE threads (
                user_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                message_ids TEXT,
                subject TEXT,
                participants TEXT,
                date_first TEXT,
                date_last TEXT,
                has_attachments INTEGER DEFAULT 0,
                attachments TEXT,
                attachments_scanned INTEGER DEFAULT 0,
                web_link TEXT,
                ai_tags TEXT,
                user_tags TEXT,
                manually_reviewed INTEGER DEFAULT 0,
                last_synced TEXT,
                body_char_count INTEGER,
                body_scan_status TEXT DEFAULT 'ok',
                tags_truncated INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, thread_id)
            )
        """)
        old_cols = [c for c in _columns(conn, "threads_old") if c != "user_id"]
        col_list = ", ".join(old_cols)
        conn.execute(
            f"INSERT INTO threads (user_id, {col_list}) "
            f"SELECT ?, {col_list} FROM threads_old",
            (LEGACY_USER_ID,)
        )
        conn.execute("DROP TABLE threads_old")
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) c FROM threads WHERE user_id=?", (LEGACY_USER_ID,)
        ).fetchone()["c"]
        print(f"Parked {n} existing threads as legacy. Run claim_legacy.py to adopt them.")

    if "token_cache" not in _columns(conn, "users"):
        # Token caches used to ride in the Flask session cookie, which is
        # signed but not encrypted — the refresh token inside was readable by
        # anyone holding the cookie. They live here instead now.
        print("Adding users.token_cache; existing sessions will need to sign in again.")
        conn.execute("ALTER TABLE users ADD COLUMN token_cache TEXT")
        conn.commit()

    if "user_id" not in _columns(conn, "sync_state"):
        # Sync state is transient; drop rather than migrate.
        conn.execute("DROP TABLE sync_state")
        conn.execute("""
            CREATE TABLE sync_state (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (user_id, key)
            )
        """)
        conn.commit()


# ── Users ─────────────────────────────────────────────────────────────────────

def upsert_user(user_id, email, display_name, now):
    conn = get_db()
    conn.execute("""
        INSERT INTO users (user_id, email, display_name, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            email=excluded.email,
            display_name=excluded.display_name,
            last_seen=excluded.last_seen
            -- token_cache deliberately untouched: it is written separately
            -- and a sign-in must not wipe a cache a running sync is using.
    """, (user_id, email, display_name, now, now))
    conn.commit()
    conn.close()


def set_token_cache(user_id, token_cache):
    """Persist a provider token cache server-side.

    It holds a long-lived refresh token. Flask's session cookie is signed
    rather than encrypted, so anything kept there is readable by whoever
    holds the cookie — which for a shared deployment means handing out other
    people's mailbox credentials.
    """
    conn = get_db()
    conn.execute("UPDATE users SET token_cache=? WHERE user_id=?",
                 (token_cache, user_id))
    conn.commit()
    conn.close()


def get_token_cache(user_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT token_cache FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return row["token_cache"] if row else None
    finally:
        conn.close()


def clear_token_cache(user_id):
    """Drop stored credentials, so signing out leaves none at rest."""
    conn = get_db()
    conn.execute("UPDATE users SET token_cache=NULL WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_user_by_email(email):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE lower(email)=lower(?)", (email,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def legacy_summary():
    """What is parked under the legacy placeholder, for review before claiming."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, MIN(date_first) oldest, MAX(date_last) newest "
            "FROM threads WHERE user_id=?", (LEGACY_USER_ID,)
        ).fetchone()
        sample = conn.execute(
            "SELECT subject, participants, date_first FROM threads "
            "WHERE user_id=? ORDER BY date_first LIMIT ?", (LEGACY_USER_ID, 8)
        ).fetchall()
        return {"count": row["n"], "oldest": row["oldest"], "newest": row["newest"],
                "sample": [dict(r) for r in sample]}
    finally:
        conn.close()


def legacy_collisions(user_id):
    """Legacy thread_ids the target account already holds.

    The primary key is (user_id, thread_id), so moving one of these violates
    it — and a single collision aborts the whole UPDATE, leaving nothing
    moved. They are expected rather than exceptional: legacy rows predate
    per-user catalogs, and the account has re-synced the same mail since.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT l.thread_id FROM threads l "
            "  JOIN threads t ON t.thread_id = l.thread_id AND t.user_id = ? "
            " WHERE l.user_id = ?", (user_id, LEGACY_USER_ID)
        ).fetchall()
        return {r["thread_id"] for r in rows}
    finally:
        conn.close()


def claim_legacy_threads(user_id, drop_duplicates=False):
    """Adopt pre-migration threads into a real account.

    Rows the account already holds are left alone rather than moved, because
    its own copy came from a later sync and is the fresher one. Returns
    (moved, duplicates) so the caller can report both.
    """
    collisions = legacy_collisions(user_id)
    conn = get_db()
    try:
        if collisions:
            placeholders = ",".join("?" * len(collisions))
            if drop_duplicates:
                conn.execute(
                    f"DELETE FROM threads WHERE user_id=? "
                    f"AND thread_id IN ({placeholders})",
                    [LEGACY_USER_ID, *collisions])
            cur = conn.execute(
                f"UPDATE threads SET user_id=? WHERE user_id=? "
                f"AND thread_id NOT IN ({placeholders})",
                [user_id, LEGACY_USER_ID, *collisions])
        else:
            cur = conn.execute(
                "UPDATE threads SET user_id=? WHERE user_id=?",
                (user_id, LEGACY_USER_ID))
        moved = cur.rowcount
        conn.commit()
        return moved, len(collisions)
    finally:
        conn.close()


# ── Threads ───────────────────────────────────────────────────────────────────

def upsert_thread(user_id, thread):
    conn = get_db()
    conn.execute("""
        INSERT INTO threads (
            user_id, thread_id, message_ids, subject, participants,
            date_first, date_last, has_attachments, attachments,
            attachments_scanned, web_link, ai_tags, user_tags,
            manually_reviewed, last_synced, body_char_count,
            body_scan_status, tags_truncated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, thread_id) DO UPDATE SET
            message_ids=excluded.message_ids,
            date_last=excluded.date_last,
            has_attachments=excluded.has_attachments,
            attachments=excluded.attachments,
            web_link=excluded.web_link,
            last_synced=excluded.last_synced,
            body_char_count=excluded.body_char_count,
            body_scan_status=excluded.body_scan_status,
            tags_truncated=excluded.tags_truncated,
            -- Never replace real tags with nothing: a re-sync writes the
            -- thread before tagging, and a failure there must not wipe the
            -- tags the previous sync paid for.
            ai_tags=CASE WHEN json_array_length(excluded.ai_tags) > 0
                         THEN excluded.ai_tags ELSE threads.ai_tags END
    """, (
        user_id,
        thread["thread_id"],
        json.dumps(thread["message_ids"]),
        thread["subject"],
        json.dumps(thread["participants"]),
        thread["date_first"],
        thread["date_last"],
        thread["has_attachments"],
        json.dumps(thread.get("attachments", [])),
        thread.get("attachments_scanned", 0),
        thread.get("web_link", ""),
        json.dumps(thread.get("ai_tags", [])),
        json.dumps(thread.get("user_tags", [])),
        thread.get("manually_reviewed", 0),
        thread["last_synced"],
        thread.get("body_char_count", 0),
        thread.get("body_scan_status", "ok"),
        thread.get("tags_truncated", 0)
    ))
    conn.commit()
    conn.close()



def get_existing_thread_ids(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT thread_id, date_last FROM threads WHERE user_id=?", (user_id,)
    ).fetchall()
    conn.close()
    return {row["thread_id"]: row["date_last"] for row in rows}


def get_untagged_threads(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT thread_id, message_ids, subject, participants, date_first, "
        "date_last, has_attachments, attachments, web_link, last_synced "
        "FROM threads WHERE user_id=? AND (ai_tags = '[]' OR ai_tags IS NULL)",
        (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_thread_tags(user_id, thread_id, tags, truncated):
    conn = get_db()
    conn.execute(
        "UPDATE threads SET ai_tags=?, tags_truncated=? WHERE user_id=? AND thread_id=?",
        (json.dumps(tags), 1 if truncated else 0, user_id, thread_id)
    )
    conn.commit()
    conn.close()


# ── Sync state ────────────────────────────────────────────────────────────────

# Statuses that mean "a sync thread should be alive right now". If one of
# these is recorded but no thread is running, the sync died — a process
# restart, a crash, or an unhandled exception — and the UI would otherwise
# show a frozen progress bar forever.
ACTIVE_STATUSES = (
    "checking folders", "indexing threads", "updating threads",
    "reading attachments", "downloading attachments", "processing",
    "tagging", "tagging (batch)", "retagging",
)
STALE_AFTER_SECONDS = 300


def _set_state(conn, user_id, key, value):
    conn.execute(
        "INSERT INTO sync_state (user_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
        (user_id, key, value)
    )


def set_sync_after(user_id, value):
    """Per-user Gmail sync window start: "YYYY-MM-DD", or "" for everything."""
    conn = get_db()
    _set_state(conn, user_id, "sync_after", value)
    conn.commit()
    conn.close()


def get_sync_after(user_id):
    """None = never chosen; "" = everything; otherwise a YYYY-MM-DD date."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE user_id=? AND key='sync_after'",
            (user_id,)
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_sync_flag(user_id, value):
    conn = get_db()
    _set_state(conn, user_id, "stop", value)
    conn.commit()
    conn.close()


def get_sync_flag(user_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE user_id=? AND key='stop'", (user_id,)
        ).fetchone()
        return row["value"] if row else "0"
    finally:
        conn.close()


def set_sync_progress(user_id, current, total, status="running"):
    conn = get_db()
    _set_state(conn, user_id, "progress_current", str(current))
    _set_state(conn, user_id, "progress_total", str(total))
    _set_state(conn, user_id, "progress_status", status)
    # Heartbeat: without it a dead sync is indistinguishable from a live one.
    _set_state(conn, user_id, "progress_updated", str(time.time()))
    conn.commit()
    conn.close()


def get_sync_progress(user_id):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT key, value FROM sync_state WHERE user_id=? AND key IN "
            "('progress_current', 'progress_total', 'progress_status', 'progress_updated')",
            (user_id,)
        ).fetchall()
        data = {row["key"]: row["value"] for row in rows}
        status = data.get("progress_status", "idle")

        try:
            age = time.time() - float(data.get("progress_updated", 0))
        except (TypeError, ValueError):
            age = float("inf")

        return {
            "current": int(data.get("progress_current", 0)),
            "total": int(data.get("progress_total", 0)),
            "status": status,
            "stale": status in ACTIVE_STATUSES and age > STALE_AFTER_SECONDS,
        }
    except Exception:
        return {"current": 0, "total": 0, "status": "idle", "stale": False}
    finally:
        conn.close()


def wipe_db(user_id):
    """Delete only the calling user's catalog."""
    conn = get_db()
    conn.execute("DELETE FROM threads WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM sync_state WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# ── Search ────────────────────────────────────────────────────────────────────

SORT_CLAUSES = {
    "date_asc": " ORDER BY date_first ASC",
    "date_desc": " ORDER BY date_last DESC",
    "last_synced": " ORDER BY last_synced DESC",
}


# Returning every match is what made broad searches slow: 11k rows means 55k
# JSON parses and a quarter-million rendered tag elements. Nobody reads past
# the first screenful anyway.
DEFAULT_SEARCH_LIMIT = 200


def search_threads(user_id, query=None, has_attachments=None, has_multiple=None,
                   date_from=None, date_to=None, sort_by="last_synced",
                   search_mode="and", min_attachment_size=None, limit=None,
                   with_total=False):
    conn = get_db()
    sql = "SELECT * FROM threads WHERE user_id=?"
    params = [user_id]

    if query:
        words = query.strip().split()
        match = fts_query(words, search_mode) if has_fts(conn) else None
        if match:
            # Token matching. The old substring path returned 2,688 threads
            # for "car" on the real corpus, 42 of which carried the tag.
            sql += (" AND rowid IN (SELECT rowid FROM threads_fts "
                    "WHERE threads_fts MATCH ?)")
            params.append(match)
        elif words:
            clauses = []
            for word in words:
                clauses.append(
                    "(ai_tags LIKE ? ESCAPE '\\' OR user_tags LIKE ? ESCAPE '\\' "
                    "OR subject LIKE ? ESCAPE '\\' OR participants LIKE ? ESCAPE '\\')")
                # % and _ are LIKE wildcards; a user typing them means the
                # characters, not "match anything".
                escaped = (word.replace("\\", "\\\\")
                               .replace("%", "\\%").replace("_", "\\_"))
                q = f"%{escaped}%"
                params.extend([q, q, q, q])
            joiner = " OR " if search_mode == "or" else " AND "
            sql += " AND (" + joiner.join(clauses) + ")"

    if has_attachments is not None:
        sql += " AND has_attachments=?"
        params.append(1 if has_attachments else 0)
    if has_multiple:
        sql += " AND json_array_length(message_ids) > 1"
    if date_from:
        sql += " AND EXISTS (SELECT 1 FROM json_each(message_ids) WHERE json_extract(value, '$.date') >= ?)"
        params.append(date_from)
    if date_to:
        sql += " AND EXISTS (SELECT 1 FROM json_each(message_ids) WHERE json_extract(value, '$.date') <= ?)"
        params.append(date_to)

    size_kb = _safe_int(min_attachment_size)
    if size_kb:
        sql += " AND EXISTS (SELECT 1 FROM json_each(attachments) WHERE json_extract(value, '$.size') >= ?)"
        params.append(size_kb * 1000)

    total = None
    if with_total:
        count_sql = "SELECT COUNT(*) c FROM (" + sql + ")"
        total = conn.execute(count_sql, params).fetchone()["c"]

    sql += SORT_CLAUSES.get(sort_by, SORT_CLAUSES["last_synced"])
    if limit:
        sql += " LIMIT ?"
        params = params + [int(limit)]

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    results = [dict(r) for r in rows]
    return (results, total) if with_total else results


def _safe_int(value):
    """Coerce untrusted query input to a positive int, or None."""
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None



# ── Delta tokens ──────────────────────────────────────────────────────────────

def get_delta_link(user_id, folder_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE user_id=? AND key=?",
            (user_id, f"delta:{folder_id}")
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_delta_link(user_id, folder_id, delta_link):
    conn = get_db()
    _set_state(conn, user_id, f"delta:{folder_id}", delta_link)
    conn.commit()
    conn.close()


def clear_delta_links(user_id):
    """Force the next sync to re-establish every folder token from scratch."""
    conn = get_db()
    conn.execute(
        "DELETE FROM sync_state WHERE user_id=? AND key LIKE 'delta:%'", (user_id,)
    )
    conn.commit()
    conn.close()


# ── Thread lookup / removal ───────────────────────────────────────────────────

def find_thread_ids_by_message_ids(user_id, message_ids):
    """Map removed message ids back to the threads that contain them.

    A delta 'removed' entry carries only a message id, so this is how a
    deletion (or a move between folders) is traced to a conversation.
    """
    if not message_ids:
        return set()

    conn = get_db()
    found = set()
    try:
        # Chunked to stay well under SQLite's variable limit.
        for i in range(0, len(message_ids), 400):
            chunk = message_ids[i:i + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""
                SELECT DISTINCT t.thread_id
                  FROM threads t, json_each(t.message_ids) m
                 WHERE t.user_id = ?
                   AND json_extract(m.value, '$.id') IN ({placeholders})
                """,
                [user_id, *chunk]
            ).fetchall()
            found.update(r["thread_id"] for r in rows)
    finally:
        conn.close()
    return found


def delete_thread(user_id, thread_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM threads WHERE user_id=? AND thread_id=?", (user_id, thread_id)
    )
    conn.commit()
    conn.close()







def edit_user_tags(user_id, thread_id, add=(), remove=()):
    """Add/remove hand-written tags on a thread.

    user_tags live alongside ai_tags and are never touched by the tagger, so a
    re-sync refreshes the AI tags without disturbing anything you wrote.
    manually_reviewed records that a human has curated this thread.
    Returns the resulting tag list, or None if the thread doesn't exist.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT user_tags FROM threads WHERE user_id=? AND thread_id=?",
            (user_id, thread_id)
        ).fetchone()
        if row is None:
            return None

        try:
            tags = json.loads(row["user_tags"] or "[]")
        except (ValueError, TypeError):
            tags = []

        drop = {t.strip().lower() for t in remove if t and t.strip()}
        tags = [t for t in tags if t.lower() not in drop]

        seen = {t.lower() for t in tags}
        for t in add:
            clean = (t or "").strip().lower()
            if clean and clean not in seen:
                tags.append(clean)
                seen.add(clean)

        conn.execute(
            "UPDATE threads SET user_tags=?, manually_reviewed=1 "
            "WHERE user_id=? AND thread_id=?",
            (json.dumps(tags), user_id, thread_id)
        )
        conn.commit()
        return tags
    finally:
        conn.close()


# ── Access control ────────────────────────────────────────────────────────────
#
# The app is multi-tenant, so a public URL means anyone with a Microsoft
# account could sign in and index their mailbox on the owner's API key.
# Sign-in therefore creates an access request that an admin must approve.

def get_access_request(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM access_requests WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_access_requests(status=None):
    conn = get_db()
    if status:
        rows = conn.execute(
            "SELECT * FROM access_requests WHERE status=? ORDER BY requested_at DESC",
            (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM access_requests ORDER BY "
            "CASE status WHEN 'pending' THEN 0 WHEN 'denied' THEN 1 ELSE 2 END, "
            "requested_at DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_access_request(user_id, email, display_name, now, status="pending"):
    """Record a sign-in attempt. Existing decisions are preserved."""
    conn = get_db()
    conn.execute("""
        INSERT INTO access_requests
            (user_id, email, display_name, status, requested_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            email=excluded.email,
            display_name=excluded.display_name,
            requested_at=excluded.requested_at
    """, (user_id, email, display_name, status, now))
    conn.commit()
    conn.close()


def set_access_decision(user_id, status, now, denied_until=None):
    conn = get_db()
    cur = conn.execute(
        "UPDATE access_requests SET status=?, decided_at=?, denied_until=? "
        "WHERE user_id=?",
        (status, now, denied_until, user_id)
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0


def mark_notified(user_id, now):
    conn = get_db()
    conn.execute("UPDATE access_requests SET notified_at=? WHERE user_id=?", (now, user_id))
    conn.commit()
    conn.close()


def count_threads(user_id):
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) c FROM threads WHERE user_id=?", (user_id,)
    ).fetchone()["c"]
    conn.close()
    return n


# ── Pending tag batches ───────────────────────────────────────────────────────
#
# A submitted batch outlives the process that sent it, so the id and the
# group -> thread_id mapping are persisted. A restarted sync resumes polling
# instead of resubmitting and paying twice.

def add_pending_batch(user_id, batch_id, mapping, submitted_at):
    conn = get_db()
    _set_state(conn, user_id, f"batch:{batch_id}",
               json.dumps({"mapping": mapping, "submitted_at": submitted_at}))
    conn.commit()
    conn.close()


def get_pending_batches(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT key, value FROM sync_state WHERE user_id=? AND key LIKE 'batch:%'",
        (user_id,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["value"])
        except (ValueError, TypeError):
            continue
        out.append((r["key"].split(":", 1)[1], payload.get("mapping", {}),
                    payload.get("submitted_at")))
    return out


def clear_pending_batch(user_id, batch_id):
    conn = get_db()
    conn.execute("DELETE FROM sync_state WHERE user_id=? AND key=?",
                 (user_id, f"batch:{batch_id}"))
    conn.commit()
    conn.close()


def mark_interrupted_syncs():
    """At startup no sync thread can be running, so any active status is a
    corpse from a previous process. Rewrite them rather than leaving a
    progress bar that never moves.

    Returns the number of users whose sync was interrupted.
    """
    conn = get_db()
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    rows = conn.execute(
        f"SELECT user_id FROM sync_state WHERE key='progress_status' "
        f"AND value IN ({placeholders})",
        ACTIVE_STATUSES
    ).fetchall()
    for r in rows:
        _set_state(conn, r["user_id"], "progress_status", "interrupted")
    conn.commit()
    conn.close()
    return len(rows)


def users_with_pending_batches():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM sync_state WHERE key LIKE 'batch:%'"
    ).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


# ── Token usage ───────────────────────────────────────────────────────────────
#
# The tagger already receives usage on every response and used to discard it.
# Recording it makes per-account cost visible in the app instead of something
# you reconstruct from the Anthropic console after the fact.

def record_usage(user_id, input_tokens, output_tokens, cache_read, batched, day):
    conn = get_db()
    conn.execute("""
        INSERT INTO usage_log
            (user_id, day, batched, requests, input_tokens, output_tokens, cache_read_tokens)
        VALUES (?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(user_id, day, batched) DO UPDATE SET
            requests=requests+1,
            input_tokens=input_tokens+excluded.input_tokens,
            output_tokens=output_tokens+excluded.output_tokens,
            cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens
    """, (user_id, day, 1 if batched else 0,
          input_tokens or 0, output_tokens or 0, cache_read or 0))
    conn.commit()
    conn.close()


def usage_by_user():
    """Totals per user, split by whether the work went through the Batch API."""
    conn = get_db()
    rows = conn.execute("""
        SELECT u.user_id, u.email, l.batched,
               SUM(l.requests) requests,
               SUM(l.input_tokens) input_tokens,
               SUM(l.output_tokens) output_tokens
          FROM usage_log l JOIN users u ON u.user_id = l.user_id
         GROUP BY u.user_id, l.batched
    """).fetchall()
    conn.close()

    out = {}
    for r in rows:
        e = out.setdefault(r["user_id"], {
            "email": r["email"], "requests": 0,
            "input_tokens": 0, "output_tokens": 0, "batched_requests": 0,
        })
        e["requests"] += r["requests"]
        e["input_tokens"] += r["input_tokens"]
        e["output_tokens"] += r["output_tokens"]
        if r["batched"]:
            e["batched_requests"] += r["requests"]
    return out


def untagged_by_user():
    """Every user holding untagged threads, whether or not they have usage.

    count_untagged answers for one user, and the admin page used to call it
    only while looping over usage_log rows — so an account that had never
    recorded usage could not be seen to need tagging, and the retag control
    that lived in that loop never rendered.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT t.user_id, COALESCE(u.email, t.user_id) email, COUNT(*) untagged
          FROM threads t
          LEFT JOIN users u ON u.user_id = t.user_id
         WHERE t.ai_tags IS NULL OR t.ai_tags = '[]'
         GROUP BY t.user_id
         ORDER BY untagged DESC
    """).fetchall()
    conn.close()
    return {r["user_id"]: {"email": r["email"], "untagged": r["untagged"]}
            for r in rows}


def usage_since(user_id, day):
    """Token totals for one user from `day` (inclusive), split by batching.

    Days are ISO strings and compare lexicographically, which is the whole
    reason the column stores them that way.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT batched, SUM(requests) requests, SUM(input_tokens) input_tokens, "
            "       SUM(output_tokens) output_tokens "
            "  FROM usage_log WHERE user_id=? AND day>=? GROUP BY batched",
            (user_id, day)
        ).fetchall()
        out = {"requests": 0, "input_tokens": 0, "output_tokens": 0,
               "batched_requests": 0}
        for r in rows:
            out["requests"] += r["requests"] or 0
            out["input_tokens"] += r["input_tokens"] or 0
            out["output_tokens"] += r["output_tokens"] or 0
            if r["batched"]:
                out["batched_requests"] += r["requests"] or 0
        return out
    finally:
        conn.close()


def count_untagged(user_id):
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) c FROM threads WHERE user_id=? "
        "AND (ai_tags IS NULL OR ai_tags='[]')", (user_id,)
    ).fetchone()["c"]
    conn.close()
    return n


# ── Catalog export / import ───────────────────────────────────────────────────
#
# Tags are the expensive part of a catalog. Moving an already-tagged catalog
# between machines should cost nothing rather than re-running the tagger, and
# the same path doubles as a backup for a deployment that has none.

EXPORT_FORMAT = 1


def export_threads(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT thread_id, message_ids, subject, participants, date_first, "
        "date_last, has_attachments, attachments, web_link, ai_tags, user_tags, "
        "manually_reviewed, last_synced, body_char_count, body_scan_status, "
        "tags_truncated FROM threads WHERE user_id=?",
        (user_id,)
    ).fetchall()
    conn.close()
    return {"format": EXPORT_FORMAT, "threads": [dict(r) for r in rows]}


def import_threads(user_id, payload, replace=False):
    """Load exported threads into this user's catalog.

    Delta tokens are deliberately NOT imported: they belong to the mailbox
    connection on the machine that produced them. The importing instance
    establishes its own on the next sync.
    """
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        raise ValueError("unrecognised export format")
    threads = payload.get("threads")
    if not isinstance(threads, list):
        raise ValueError("export contains no threads")

    conn = get_db()
    if replace:
        conn.execute("DELETE FROM threads WHERE user_id=?", (user_id,))

    imported = 0
    for t in threads:
        if not isinstance(t, dict) or not t.get("thread_id"):
            continue
        conn.execute("""
            INSERT INTO threads (
                user_id, thread_id, message_ids, subject, participants,
                date_first, date_last, has_attachments, attachments, web_link,
                ai_tags, user_tags, manually_reviewed, last_synced,
                body_char_count, body_scan_status, tags_truncated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, thread_id) DO UPDATE SET
                message_ids=excluded.message_ids,
                subject=excluded.subject,
                participants=excluded.participants,
                date_first=excluded.date_first,
                date_last=excluded.date_last,
                has_attachments=excluded.has_attachments,
                attachments=excluded.attachments,
                web_link=excluded.web_link,
                ai_tags=CASE WHEN json_array_length(excluded.ai_tags) > 0
                             THEN excluded.ai_tags ELSE threads.ai_tags END,
                user_tags=excluded.user_tags,
                manually_reviewed=excluded.manually_reviewed,
                last_synced=excluded.last_synced,
                body_char_count=excluded.body_char_count,
                body_scan_status=excluded.body_scan_status,
                tags_truncated=excluded.tags_truncated
        """, (
            user_id, t["thread_id"], t.get("message_ids") or "[]", t.get("subject") or "",
            t.get("participants") or "[]", t.get("date_first") or "", t.get("date_last") or "",
            t.get("has_attachments") or 0, t.get("attachments") or "[]", t.get("web_link") or "",
            t.get("ai_tags") or "[]", t.get("user_tags") or "[]",
            t.get("manually_reviewed") or 0, t.get("last_synced") or "",
            t.get("body_char_count") or 0, t.get("body_scan_status") or "ok",
            t.get("tags_truncated") or 0,
        ))
        imported += 1

    conn.commit()
    conn.close()
    return imported
