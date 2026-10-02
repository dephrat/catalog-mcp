# Catalog MCP Server — Spec

Expose Catalog's tag index to LLM agents as an MCP server, so an agent
(Claude Desktop, Claude Code) gets fast semantic recall over the mailbox
instead of looping raw Gmail searches. The web app remains as a demo; the
MCP server becomes the primary interface.

## Why the database stays

The SQLite catalog holds the three things raw mail APIs can't give an
agent per-query: the LLM tag index (topics, names, synonyms, misspellings,
document types — informed by attachment text at tagging time), sub-10ms
FTS over it, and provider-agnostic thread metadata with deep links. Bodies
and attachment text are NOT stored; full content is refetched live from
the provider, so `get_thread` is a live fetch using stored credentials.

## Architecture

```
catalog sync (CLI)      SQLite catalog.db        MCP server (stdio)
OAuth, fetch,     ────▶ threads + tags + FTS ◀── search_catalog (DB, fast)
extract, tag            + token cache            list_tags, sync_status (DB)
                                                 get_thread (live provider
                                                  fetch via stored token)
```

- **Two venvs.** The project stays on Python 3.9 (`.venv`). The MCP SDK
  requires ≥3.10, so the server runs from a dedicated `.venv-mcp` on
  Homebrew Python 3.12. All tool *logic* lives in a 3.9-compatible module
  (`mcp_tools.py`) tested by the existing pytest suite; `mcp_server.py` is
  a thin FastMCP wrapper that only the 3.12 venv imports.
- **Read-only server.** No `trigger_sync` tool: agents must not start
  hour-long, dollar-costing jobs. Freshness is reported, not controlled.
- **Sync gets a CLI** (`sync_cli.py`) so indexing doesn't require the web
  app. `app.run_sync(user_id, provider, token, token_cache)` is already
  session-free and app.py imports headlessly (the test suite does it), so
  the CLI imports app directly rather than extracting 800 lines for no
  behavioral gain.
- **Single-user default.** The server reads `CATALOG_USER` from env, or
  uses the sole user in the DB; errors if ambiguous.

## Tools

1. `search_catalog(query, from_addr="", date_from="", date_to="",
   has_attachments=None, match_any=False, limit=20)` → `{total, threads:
   [{thread_id, subject, participants, date_first, date_last, tags,
   has_attachments, web_link}]}`. Lean rows — agents re-query cheaply.
   `tags` is the merged ai+user tag list. Backed by `db.search_threads`.
2. `get_thread(thread_id)` → `{subject, web_link, messages: [{from,
   to, date, body_text}], attachments: [names]}`. Live provider fetch via
   the stored token cache (refresh as the web app does); bodies stripped
   to text and truncated to 50k chars total. Errors clearly if sign-in
   expired ("run the Catalog web app to sign in again").
3. `list_tags(prefix="", limit=50)` → most-frequent tags matching prefix,
   with counts. Lets the agent discover the folksonomy before querying.
4. `sync_status()` → `{last_synced, thread_count, untagged_count,
   provider}`. Freshness honesty so the agent can caveat stale answers.

## Constraints

- No new dependencies in the 3.9 project venv. `.venv-mcp` gets `mcp` (the
  official SDK) and `python-dotenv` only.
- Existing pytest suite (`.venv/bin/python -m pytest`, currently 332
  tests) must stay green after the sync extraction.
- Tool docstrings are agent-facing contract text: they must state when to
  use the tool and what it costs (fast/slow).
- Registration documented for Claude Desktop and Claude Code
  (`claude mcp add`), plus a stdio smoke test that works without an agent.
