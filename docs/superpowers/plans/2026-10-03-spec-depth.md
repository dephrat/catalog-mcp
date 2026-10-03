# Spec-Depth Milestone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add structured output, resources, a prompt with tag completion, and progress reporting to the catalog MCP server, and rewrite the README around the agent story with measured numbers.

**Architecture:** All new server surface lives in `mcp_server.py` (3.12 venv, SDK v2 `MCPServer`); any new queryable logic goes in `mcp_tools.py` (3.9, pytest-covered). The smoke test remains the server-level gate and grows to cover each new surface.

**Tech Stack:** mcp SDK v2 (`MCPServer`, `Context`, `structured_output`, resource templates, `@prompt`, `@completion`), existing SQLite layer.

**Spec:** `docs/superpowers/specs/2026-10-03-spec-depth.md`

## Global Constraints

- No new tools; the four existing tool names and parameter signatures must not change (additive `Context` param on get_thread is allowed — the SDK injects it, clients see no schema change).
- `mcp_tools.py` stays Python-3.9 compatible; full suite `.venv/bin/python -m pytest` stays green (374 now).
- Server stays read-only; resources and completions must not write.
- Smoke test `bash scripts/mcp_smoke.sh` must pass after every task that touches the server.
- Commits: lowercase imperative subject + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Review Focus

- A completion request for a prefix matching no tags must return an empty list, not an error. → test in Task 2.
- `resources/read` of `catalog://thread/<unknown-id>` must return the same not-found error shape as the tool, not a traceback. → smoke/unit in Task 2.
- `structured_output=True` with dicts that include optional `error`/`notice`/`truncated` keys must validate (TypedDict `total=False`); a tool returning `{"error": ...}` must not fail schema validation. → smoke assertion in Task 1.
- `get_thread` without a client that supports progress must still work (progress is best-effort). → smoke in Task 3 (smoke client sends no progressToken).
- README numbers must match the measured facts (7/8 vs 4/8; 14.8s/21.8s means; ~6 min steady-state sync; 1,834 threads) — no rounding up. → Task 4 self-check.

---

### Task 1: Structured output on all four tools

**Files:**
- Modify: `mcp_server.py`
- Modify: `scripts/mcp_smoke.sh`

**Interfaces:**
- Produces: TypedDicts in mcp_server.py: `SearchResult`, `ThreadResult`, `TagsResult`, `StatusResult` (`total=False`, fields mirroring the real mcp_tools return dicts incl. optional `error`/`notice`/`truncated`). Tool decorators gain `structured_output=True`.

- [ ] **Step 1: Extend smoke test first** (it is the failing test): after tools/list, assert via jq that each of the four tools carries a non-empty `outputSchema`; after the existing sync_status tools/call, assert the response contains `structuredContent.thread_count`.
- [ ] **Step 2: Run smoke — expect FAIL** (no outputSchema yet).
- [ ] **Step 3: Implement** TypedDicts + `structured_output=True`; annotate each wrapper's return type. Keep `_tolerate_errors` — confirm an `{"error": ...}` return still validates (include `error: str` in each TypedDict).
- [ ] **Step 4: Run smoke — PASS. Run 3.9 suite — green (unchanged).**
- [ ] **Step 5: Commit** ("add structured output schemas to all tools").

### Task 2: Resources + prompt + tag completion

**Files:**
- Modify: `mcp_server.py`, `mcp_tools.py`, `tests/test_mcp_tools.py`, `scripts/mcp_smoke.sh`

**Interfaces:**
- Consumes: `mcp_tools.get_thread(thread_id)`, `mcp_tools.list_tags(prefix, limit)`.
- Produces in mcp_tools.py: `tag_names(prefix="", limit=50) -> list[str]`-shaped helper (3.9 typing: `List[str]`) returning just names for completion use (reuses list_tags' query; add a focused unit test incl. empty-prefix-no-match → `[]`).
- Produces in mcp_server.py: resource `catalog://tags` (JSON string of `list_tags(limit=200)`), resource template `catalog://thread/{thread_id}` (returns `json.dumps(mcp_tools.get_thread(thread_id))` — the error dict serializes the same way), prompt `find_document(description: str, tag: str = "")` returning instruction text that tells the model to use search_catalog (tags-first strategy) then get_thread on at most 2 finalists, and a `@mcp.completion()` handler that completes the prompt's `tag` argument via `tag_names(prefix=...)` and returns `[]` for anything else.

- [ ] **Step 1: Write failing unit tests** for `tag_names` (returns names only, prefix filter, no-match → `[]`) in tests/test_mcp_tools.py.
- [ ] **Step 2: Run them — FAIL.**
- [ ] **Step 3: Implement `tag_names` in mcp_tools.py; unit tests PASS; full suite green.**
- [ ] **Step 4: Extend smoke test**: resources/list (assert `catalog://tags` present), resources/templates/list (assert the thread template), resources/read of `catalog://tags` (assert a known-shape JSON), prompts/list (assert `find_document`), completion/complete for prompt arg `tag` with a prefix seeded in the smoke DB (assert the seeded tag comes back) and with prefix "zzz" (assert empty values). Run — FAIL.
- [ ] **Step 5: Implement the server surfaces. Smoke PASS.**
- [ ] **Step 6: Commit** ("add resources, find_document prompt, and tag completion").

### Task 3: Progress reporting on get_thread

**Files:**
- Modify: `mcp_server.py`, `scripts/mcp_smoke.sh`

**Interfaces:**
- Consumes: SDK `Context` (`ctx.report_progress`). get_thread wrapper gains `ctx: Context` (SDK-injected; tool schema unchanged — smoke asserts get_thread's inputSchema still lists only thread_id as required).

- [ ] **Step 1: Implement**: before the mcp_tools.get_thread call `await/call ctx.report_progress(0, ...)` + message "fetching thread from mailbox", after it progress complete. Wrap in try/except so clients without a progressToken never break the call (Review Focus).
- [ ] **Step 2: Smoke**: assert get_thread still callable (existing call path) and its inputSchema's required == ["thread_id"]. PASS.
- [ ] **Step 3: Commit** ("report progress during live thread fetch").

### Task 4: README overhaul

**Files:**
- Modify: `README.md`

**Interfaces:** none; documents Tasks 1–3 surfaces.

- [ ] **Step 1: Restructure**: title/intro leads with the MCP server (agent-native recall over your email archive); sections: worked transcript (take a REAL search_catalog request/response from the smoke server or a claude -p run, trimmed), benchmark table (catalog 7/8 hand-graded vs raw-Gmail 4/8; mean 14.8s vs 21.8s; ~$0.08/question; n=8, one mailbox, honesty caveat), indexing cost paragraph (first import: hours, Gmail-quota-bound + ~$1-2 batch tagging; steady-state sync measured 5m46s for a week-plus of mail; 1,834 threads, 0 untagged), spec surfaces list (4 tools with outputSchema, resources, prompt, completions, progress), then the existing setup/registration sections, then the demoted web-app story.
- [ ] **Step 2: Accuracy self-check** against Review Focus numbers; verify every command in the README verbatim-matches the repo (paths, venvs).
- [ ] **Step 3: Run smoke once more (unchanged) + 3.9 suite. Commit** ("lead README with the MCP server and measured numbers").
