"""Stdio MCP server exposing the Catalog tag index to agent clients.

Thin wrapper only: all logic lives in mcp_tools.py. This file's job is
just the MCP plumbing (tool registration, stdio transport, env loading)
plus docstrings that tell an agent client when to reach for each tool
and what it costs.

Read-only by design: nothing here writes to the database or triggers a
mailbox sync. Run `sync_cli.py` separately to populate/refresh the
catalog before using this server.

Run with the dedicated venv, e.g.:
    .venv-mcp/bin/python mcp_server.py

Note: the installed `mcp` SDK is v2.x, where `FastMCP` was renamed to
`MCPServer` (same decorator-based API, just a new class name).
"""
import functools
import json
import os
import sqlite3
from typing import TypedDict

from mcp_types import Completion, PromptReference

from dotenv import load_dotenv

load_dotenv()

# db.py reads DB_PATH at import time and defaults to the relative
# "catalog.db" — fine for the web app (always run from the repo root) but
# wrong for an MCP client, which launches this process with its own cwd
# (e.g. Claude Desktop uses its own working directory, not this repo). Pin
# an absolute default next to this file *before* anything below imports
# mcp_tools (which imports db), so a .env without DB_PATH still finds the
# real database instead of silently opening/creating an empty one wherever
# the client happened to start us.
os.environ.setdefault(
    "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.db")
)

from mcp.server.mcpserver import MCPServer  # noqa: E402

import mcp_tools  # noqa: E402

mcp = MCPServer("catalog")


# Structured-output TypedDicts, one per tool. `total=False` throughout: every
# field here is optional because the *union* of all the shapes a given
# mcp_tools function can actually return is what the schema has to cover —
# the empty-catalog "notice" path, the plain success path, and (via
# _tolerate_errors) the {"error": ...} path all go through the same
# annotation. Shapes mirror mcp_tools.py's real return dicts exactly; see
# that file's docstrings/_lean_row for the source of truth. mcp_tools.py
# itself is untouched — these are purely a mirror for the SDK's
# schema generator.

class ThreadSummary(TypedDict, total=False):
    """One lean thread projection, as produced by mcp_tools._lean_row."""
    thread_id: str
    subject: str
    participants: list[str]
    date_first: str
    date_last: str
    tags: list[str]
    has_attachments: bool
    web_link: str


class SearchResult(TypedDict, total=False):
    """Return shape of mcp_tools.search_catalog."""
    threads: list[ThreadSummary]
    total: int
    notice: str
    error: str


class TagCount(TypedDict, total=False):
    tag: str
    count: int


class TagsResult(TypedDict, total=False):
    """Return shape of mcp_tools.list_tags."""
    tags: list[TagCount]
    notice: str
    error: str


class StatusResult(TypedDict, total=False):
    """Return shape of mcp_tools.sync_status."""
    last_synced: str | None
    thread_count: int
    untagged_count: int
    provider: str
    notice: str
    error: str


# "from" is a Python keyword, so this one message-shape TypedDict (used only
# inside ThreadResult.messages) is built via the functional TypedDict form
# instead of the class syntax every other TypedDict here uses.
ThreadMessage = TypedDict(
    "ThreadMessage",
    {"from": str, "to": list[str], "date": str, "body_text": str},
    total=False,
)


class ThreadResult(TypedDict, total=False):
    """Return shape of mcp_tools.get_thread."""
    subject: str
    web_link: str
    messages: list[ThreadMessage]
    attachments: list[str]
    truncated: bool
    error: str


def _tolerate_errors(fn):
    """Turn an expected ValueError/sqlite3.Error from mcp_tools into the same
    {"error": "..."} shape get_thread already documents, instead of letting
    it surface as the SDK's opaque "Error executing tool X". Both are
    expected, user-actionable conditions here — a misconfigured CATALOG_USER
    (ValueError, e.g. "no such user" or "multiple users; set CATALOG_USER")
    or a database problem (sqlite3.Error) — not a bug to hide a traceback
    for.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, sqlite3.Error) as e:
            return {"error": str(e)}
    return wrapper


@mcp.tool(structured_output=True)
@_tolerate_errors
def search_catalog(query: str = "", from_addr: str = "", date_from: str = "",
                    date_to: str = "", has_attachments: bool | None = None,
                    match_any: bool = False, limit: int = 20) -> SearchResult:
    """Search the local tag index for threads. Fast: local index only, no network.

    Use this first for almost any lookup. `query` is free-text matched
    against the catalog's full-text index. `from_addr` is NOT a precise
    sender-column filter — it is folded into the same free-text query as
    just another search term (e.g. "query='invoice' from_addr='alice'"
    searches for both words together). Under match_any=True this becomes
    even looser: `from_addr` is just one more OR'd term, not a hard
    restriction to that sender. If you need confidence that a result is
    really from a specific address, check the returned thread's
    `participants` field yourself.

    `date_from`/`date_to` bound date_first/date_last. `has_attachments`
    filters on whether the thread has any attachments. `match_any=True`
    switches the query terms from AND to OR. `limit` caps result count.

    Returns {"threads": [...], "total": N} with a lean projection per
    thread (thread_id, subject, participants, dates, tags,
    has_attachments, web_link) — not full message bodies. For full
    content of a specific thread, follow up with get_thread on 1-3
    finalists from these results.
    """
    return mcp_tools.search_catalog(
        query=query, from_addr=from_addr, date_from=date_from,
        date_to=date_to, has_attachments=has_attachments,
        match_any=match_any, limit=limit,
    )


@mcp.tool(structured_output=True)
@_tolerate_errors
def list_tags(prefix: str = "", limit: int = 50) -> TagsResult:
    """List tags in use across the catalog, with counts. Fast: local index only, no network.

    Use this to discover what tags exist before filtering search_catalog
    results by tag, or to spot-check tagging coverage. `prefix` narrows
    to tags starting with that string; `limit` caps how many are
    returned. Results are ordered by count descending, then tag name.
    """
    return mcp_tools.list_tags(prefix=prefix, limit=limit)


@mcp.tool(structured_output=True)
@_tolerate_errors
def sync_status() -> StatusResult:
    """Report catalog health: thread count, untagged count, last sync, provider. Fast: local index only, no network.

    Use this to sanity-check whether the catalog is populated and how
    stale it is before relying on search results, or to explain to a
    user why a search came back empty. This tool never triggers a sync
    itself — run sync_cli.py separately if the catalog needs refreshing.
    """
    return mcp_tools.sync_status()


@mcp.tool(structured_output=True)
@_tolerate_errors
def get_thread(thread_id: str) -> ThreadResult:
    """Fetch a thread's full message bodies live from the mail provider. Slow: live mailbox fetch — use on 1-3 finalists.

    Unlike the other tools, this one makes a real network round trip to
    the mail provider (and may refresh an auth token) every call, so
    don't call it for every search result — narrow with search_catalog
    first, then fetch full content for only the 1-3 threads you actually
    need to read. Message bodies are never cached locally, so there is
    no faster path to this content.

    Returns {"subject", "web_link", "messages": [...], "attachments"},
    or {"error": "..."} if the thread_id isn't found for this user or the
    provider sign-in has expired.
    """
    return mcp_tools.get_thread(thread_id)


def _tolerate_errors_json(fn):
    """Resource-surface counterpart to _tolerate_errors above.

    Resource handlers return a JSON *string* (not a dict the SDK serializes
    for us), so a resolve_user() ValueError (ambiguous multi-user, no
    CATALOG_USER set) or a sqlite3.Error has to be caught here and
    re-serialized into the same {"error": "..."} shape the tool layer
    produces — otherwise it would propagate past this decorator as a raised
    exception and surface as a protocol-level INTERNAL_ERROR instead of
    degrading the way the equivalent tool call does for the same
    underlying condition.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, sqlite3.Error) as e:
            return json.dumps({"error": str(e)})
    return wrapper


@mcp.resource("catalog://tags")
@_tolerate_errors_json
def tags_resource() -> str:
    """Static resource mirror of list_tags(limit=200), as a JSON string.

    Lets a client pull the tag vocabulary as context (e.g. to ground a
    prompt) without making a tool call. Same read-only query as the
    list_tags tool — just exposed as a resource too.
    """
    return json.dumps(mcp_tools.list_tags(limit=200))


@mcp.resource("catalog://thread/{thread_id}")
@_tolerate_errors_json
def thread_resource(thread_id: str) -> str:
    """Resource-template mirror of get_thread(thread_id), as a JSON string.

    Same live-provider fetch and the same {"error": "..."} shape on an
    unknown thread_id — serialized the same way the get_thread tool
    returns it, not raised as a traceback.
    """
    return json.dumps(mcp_tools.get_thread(thread_id))


@mcp.prompt()
def find_document(description: str, tag: str = "") -> str:
    """Instruction text guiding an agent to find a document in the catalog.

    Tells the model to search tags-first (list_tags/search_catalog by
    tag) before falling back to free-text search, and to only call the
    slow get_thread tool on at most 2 finalists once narrowed down.
    """
    tag_hint = f" The user suggested the tag \"{tag}\" may be relevant — check it first." if tag else ""
    return (
        f"Find the document(s) matching this description: \"{description}\"."
        f"{tag_hint} Use a tags-first strategy: call list_tags (optionally "
        "with a prefix) or search_catalog filtered by tag to narrow down "
        "candidates before trying a broad free-text search. Once you have "
        "narrowed to at most 2 finalists, call get_thread on those 1-2 "
        "thread_ids to confirm and read full content — get_thread is a "
        "slow, live mailbox fetch, so don't call it on more than 2 "
        "candidates."
    )


@mcp.completion()
async def complete_argument(ref, argument, context):
    """Complete the find_document prompt's `tag` argument from the tag index.

    Scoped tightly to spec: only the prompt's `tag` argument gets
    suggestions (via tag_names); every other ref/argument combination
    returns an empty completion rather than guessing — a prefix matching
    no tags is exactly as "no suggestions" as an unrecognized argument,
    not an error either way.
    """
    if (isinstance(ref, PromptReference) and ref.name == "find_document"
            and argument.name == "tag"):
        return Completion(values=mcp_tools.tag_names(prefix=argument.value))
    return Completion(values=[])


if __name__ == "__main__":
    mcp.run(transport="stdio")
