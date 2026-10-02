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
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

import mcp_tools

load_dotenv()

mcp = MCPServer("catalog")


@mcp.tool()
def search_catalog(query: str = "", from_addr: str = "", date_from: str = "",
                    date_to: str = "", has_attachments: bool | None = None,
                    match_any: bool = False, limit: int = 20) -> dict:
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


@mcp.tool()
def list_tags(prefix: str = "", limit: int = 50) -> dict:
    """List tags in use across the catalog, with counts. Fast: local index only, no network.

    Use this to discover what tags exist before filtering search_catalog
    results by tag, or to spot-check tagging coverage. `prefix` narrows
    to tags starting with that string; `limit` caps how many are
    returned. Results are ordered by count descending, then tag name.
    """
    return mcp_tools.list_tags(prefix=prefix, limit=limit)


@mcp.tool()
def sync_status() -> dict:
    """Report catalog health: thread count, untagged count, last sync, provider. Fast: local index only, no network.

    Use this to sanity-check whether the catalog is populated and how
    stale it is before relying on search results, or to explain to a
    user why a search came back empty. This tool never triggers a sync
    itself — run sync_cli.py separately if the catalog needs refreshing.
    """
    return mcp_tools.sync_status()


@mcp.tool()
def get_thread(thread_id: str) -> dict:
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
