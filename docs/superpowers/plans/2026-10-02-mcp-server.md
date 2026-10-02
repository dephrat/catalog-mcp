# Catalog MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Catalog's tag index to LLM agents as a stdio MCP server, with a sync CLI so indexing no longer needs the web app.

**Architecture:** Tool logic lives in a Python-3.9-compatible `mcp_tools.py` tested by the existing suite; `mcp_server.py` is a thin FastMCP wrapper run from a dedicated Python-3.12 venv (`.venv-mcp`). `search_catalog`/`list_tags`/`sync_status` read SQLite only; `get_thread` does a live provider fetch via the stored token cache. `sync_cli.py` wraps the existing `app.run_sync`.

**Tech Stack:** Python 3.9 (project) + 3.12 (`.venv-mcp`, Homebrew), official `mcp` SDK (FastMCP), existing SQLite/FTS layer.

**Spec:** `docs/superpowers/specs/2026-10-02-mcp-server.md`

## Global Constraints

- No new dependencies in the 3.9 project venv; `.venv-mcp` gets `mcp` and `python-dotenv` only.
- Existing suite must stay green: `.venv/bin/python -m pytest` (332 tests before this work).
- Working dir for all commands: `/Users/danielephrat/Documents/Code/catalog`.
- The server is read-only: no tool may write to the DB or start a sync.
- Tool docstrings state when to use the tool and whether it is fast (DB) or slow (network).
- User resolution everywhere: `CATALOG_USER` env var, else the sole row in `users`, else raise `ValueError("multiple users; set CATALOG_USER")`.
- Commits follow existing style (lowercase imperative subject) and end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Review Focus

- Empty catalog (fresh clone, no sync yet): every tool returns a clear "catalog is empty — run sync_cli.py" shape, not a crash or `[]` with no explanation. → test in Task 1.
- Expired/revoked Google sign-in during `get_thread`: a clear re-auth message, not a traceback. → test in Task 2.
- FTS5 query metacharacters in `query` (quotes, `-`, `*`, `NEAR`): must not raise `sqlite3.OperationalError`. `db.fts_query` already quotes terms — test proves it end-to-end through `search_catalog`. → test in Task 1.
- A `thread_id` not in the catalog (or another user's) passed to `get_thread`: explicit not-found error, no provider call attempted. → test in Task 2.
- Sync CLI run while the web app's sync for the same user is running: `is_running` guard honored, CLI exits non-zero with a message instead of double-syncing. → test in Task 3.

---

### Task 1: mcp_tools.py — DB-backed tools (search_catalog, list_tags, sync_status)

**Files:**
- Create: `mcp_tools.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: `db.search_threads(user_id, query=..., has_attachments=..., date_from=..., date_to=..., search_mode=..., limit=..., with_total=True)`; `db.count_threads(user_id)`, `db.count_untagged(user_id)`, `db.get_db()`.
- Produces (Tasks 2/4 rely on these exact names):
  - `resolve_user() -> str` (per Global Constraints rule)
  - `provider_for(user_id) -> str` — the `users` table has no provider
    column (the web app keeps it in the session), so detect from the
    stored token cache: parsed JSON with a top-level `"refresh_token"`
    key → `"gmail"`; anything else non-empty → `"microsoft"`; no cache →
    `""`. Add a test for each of the three cases.
  - `search_catalog(query="", from_addr="", date_from="", date_to="", has_attachments=None, match_any=False, limit=20) -> dict`
  - `list_tags(prefix="", limit=50) -> dict`
  - `sync_status() -> dict`

- [ ] **Step 1: Write failing tests** in `tests/test_mcp_tools.py`, reusing the existing test DB fixture pattern from `tests/` (temp `DB_PATH`, seeded threads):

```python
def test_resolve_user_prefers_env_then_sole_user(monkeypatch): ...
def test_resolve_user_raises_when_ambiguous(monkeypatch): ...
def test_search_returns_lean_rows_and_total(seeded):
    out = mcp_tools.search_catalog(query="dentist")
    assert set(out["threads"][0]) == {"thread_id", "subject", "participants",
        "date_first", "date_last", "tags", "has_attachments", "web_link"}
    assert out["total"] >= 1
def test_search_empty_catalog_says_so(empty_db):
    assert "run sync_cli.py" in mcp_tools.search_catalog(query="x")["notice"]
def test_search_survives_fts_metacharacters(seeded):
    mcp_tools.search_catalog(query='"unclosed -NEAR( *')  # must not raise
def test_list_tags_returns_counts_matching_prefix(seeded): ...
def test_sync_status_reports_counts_and_last_synced(seeded): ...
```

- [ ] **Step 2: Run tests, verify they fail** — `.venv/bin/python -m pytest tests/test_mcp_tools.py -v` → FAIL (module not found).

- [ ] **Step 3: Implement `mcp_tools.py`.** `tags` = merged, deduped `ai_tags` + `user_tags` (both stored as JSON lists). `list_tags` aggregates tag frequency with one SQL pass over the user's rows (json_each). `sync_status` returns `{last_synced, thread_count, untagged_count, provider}` (`provider` via `provider_for`; `last_synced` = max over threads). Every function resolves the user via `resolve_user()`.

- [ ] **Step 4: Run full suite** — `.venv/bin/python -m pytest` → all pass.

- [ ] **Step 5: Commit** — `git add mcp_tools.py tests/test_mcp_tools.py && git commit` ("add DB-backed MCP tool logic").

### Task 2: get_thread — live provider fetch

**Files:**
- Modify: `mcp_tools.py`
- Test: `tests/test_mcp_tools.py` (extend)

**Interfaces:**
- Consumes: `db.get_token_cache(user_id)`, `providers.get(name).refresh_token(cache)` / `.get_thread(token, thread_id)`, `app.strip_html` is NOT importable here (keep mcp_tools free of app.py) — reimplement body-to-text via the same approach `extractor`/`app.strip_html` uses, or move/copy the ~6-line helper into `mcp_tools.py`.
- Produces: `get_thread(thread_id) -> dict` with keys `subject, web_link, messages: [{from, to, date, body_text}], attachments: [str]`, total body text truncated to 50_000 chars with a `truncated: True` flag.

- [ ] **Step 1: Write failing tests** (mock `providers.get` with a fake provider; no network):

```python
def test_get_thread_fetches_live_and_strips_html(seeded, fake_provider): ...
def test_get_thread_unknown_id_errors_without_provider_call(seeded): ...
def test_get_thread_expired_signin_gives_reauth_message(seeded, dead_provider):
    # refresh_token -> (None, None) must produce err mentioning signing in again
def test_get_thread_truncates_at_50k_and_flags_it(seeded, fake_provider): ...
```

- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement.** Validate thread_id exists in DB for the user first; refresh token from stored cache; map provider messages to the output shape.
- [ ] **Step 4: Full suite green.**
- [ ] **Step 5: Commit** ("add live get_thread to MCP tool logic").

### Task 3: sync_cli.py

**Files:**
- Create: `sync_cli.py`
- Test: `tests/test_sync_cli.py`

**Interfaces:**
- Consumes: `mcp_tools.resolve_user()`, `db.get_token_cache`, `providers.get`, `app.run_sync(user_id, provider, token, token_cache)`, `app.is_running("sync_state", user_id)` (check actual helper signature in app.py:396).
- Produces: `main(argv=None) -> int` (0 success, 2 auth needed, 3 already running); `python sync_cli.py [--user EMAIL]`.

- [ ] **Step 1: Write failing tests** — monkeypatch `app.run_sync` and token plumbing; assert exit codes for: happy path calls run_sync with refreshed token; dead refresh → exit 2 with sign-in message; `is_running` true → exit 3, run_sync NOT called.
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement** (argparse, `load_dotenv()` before importing app).
- [ ] **Step 4: Full suite green.**
- [ ] **Step 5: Commit** ("add sync CLI").

### Task 4: mcp_server.py + .venv-mcp

**Files:**
- Create: `mcp_server.py`, `requirements-mcp.txt` (`mcp`, `python-dotenv`)
- Modify: `.gitignore` only if `.venv-mcp/` isn't already covered by the `.venv/`-style rules (it isn't — add `.venv-mcp/`).

**Interfaces:**
- Consumes: the four `mcp_tools` functions, exactly as named in Tasks 1–2.
- Produces: FastMCP server named `catalog`, one `@mcp.tool()` per function, docstrings per Global Constraints (search/list/status: "fast, local index"; get_thread: "slow, live mailbox fetch — use on 1–3 finalists").

- [ ] **Step 1: Create venv** — `export PATH=/opt/homebrew/bin:$PATH && brew install python@3.12` (skip if present) `&& python3.12 -m venv .venv-mcp && .venv-mcp/bin/pip install -r requirements-mcp.txt`.
- [ ] **Step 2: Implement `mcp_server.py`** — FastMCP, stdio transport, `load_dotenv()` at startup; tool wrappers return `mcp_tools` dicts directly.
- [ ] **Step 3: Smoke test over real stdio** (no agent): pipe a JSON-RPC `initialize` + `tools/list` to `.venv-mcp/bin/python mcp_server.py` and assert the four tool names appear in the output. Script it as `scripts/mcp_smoke.sh`; run it; expect `search_catalog`, `get_thread`, `list_tags`, `sync_status` in output.
- [ ] **Step 4: Full 3.9 suite still green** (server file must not be imported by it).
- [ ] **Step 5: Commit** ("add stdio MCP server").

### Task 5: Registration docs + README

**Files:**
- Modify: `README.md` (new "MCP server" section after Detective)

**Interfaces:** none produced; documents Task 4's launch command.

- [ ] **Step 1: Write README section**: what the server is (agent-native interface over the index), Claude Code registration (`claude mcp add catalog -- /abs/path/.venv-mcp/bin/python /abs/path/mcp_server.py` — written with a `$PWD`-style placeholder), Claude Desktop JSON snippet, `CATALOG_USER`, the read-only/no-trigger_sync design choice, and the sync CLI usage.
- [ ] **Step 2: Verify smoke test still passes** (`scripts/mcp_smoke.sh`).
- [ ] **Step 3: Commit** ("document MCP server and sync CLI"); push per standing rule after merge.
