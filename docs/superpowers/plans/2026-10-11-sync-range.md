# Sync Range Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gmail users choose/extend how far back Catalog indexes from the app (with a cost preview) or the CLI — no env edits, no cursor surgery.

**Architecture:** A per-user `sync_after` value in `sync_state` flows app → provider as an explicit per-call argument (Gmail only; env `GMAIL_SYNC_QUERY` stays as deployment default). Widening to an older date clears the Gmail cursor so the next ordinary scan re-walks with the wider window; dedup makes it additive.

**Tech Stack:** existing Flask app, db.py sync_state, gmail.py REST layer. No new deps.

**Spec:** `docs/superpowers/specs/2026-10-11-sync-range.md`

## Global Constraints

- Full suite green (`.venv/bin/python -m pytest`, 382 now); `scripts/check_secrets.py --all` clean; smoke test untouched and passing.
- MailProvider base interface signature unchanged; `tests/test_providers.py`-style interface-conformance test must still pass (locate it; the suite has one asserting every provider implements the interface).
- Microsoft behavior byte-identical.
- Narrowing never deletes indexed threads.
- Commits: lowercase imperative subject + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Review Focus

- Widen with a sync ALREADY running must not clear the cursor mid-run (guard with the same running checks /sync uses) → test in Task 2.
- `sync_after=""` (everything) vs unset (fall back to env GMAIL_SYNC_QUERY) are different states; test both → Task 1.
- Preview with an invalid/future date → clean 400-style error, no provider call crash → Task 2.
- A Microsoft user POSTing to the range endpoint → explicit "gmail only" rejection, nothing stored → Task 2.
- CLI `--since` on a mailbox mid-first-import (no cursor yet) must not crash on clear → Task 4.

---

### Task 1: Per-user window plumbing (db → run_sync → Gmail provider)

**Files:**
- Modify: `db.py`, `app.py` (run_sync + detect_changes path), `providers.py` (GmailProvider only), `gmail.py`
- Test: `tests/test_gmail.py`, `tests/test_sync.py` (or wherever run_sync's change-detection is tested — follow existing homes)

**Interfaces:**
- Produces in db.py: `set_sync_after(user_id, value)` / `get_sync_after(user_id) -> str | None` (None = unset; "" = everything) using `_set_state`/sync_state.
- Produces in gmail.py: `history_changes(access_token, start_history_id=None, sync_query=None)` — explicit arg wins over env; `_full_enumeration(headers, sync_query=None)` likewise (env fallback preserved at gmail.py:180).
- Produces in providers.py: `GmailProvider.changes_for_source(..., cursor)` reads nothing new itself; instead GmailProvider gains `set_sync_window(after: str | None)`-free design — DECISION: pass through `changes_for_source(access_token, source_id, cursor, sync_query=None)` as a keyword-only optional on BOTH base and providers with default None (base signature gains an optional kwarg = non-breaking; Microsoft ignores it). app.py's detect_changes passes `sync_query=_window_query(user_id, provider)` where `_window_query` returns `f"after:{d}"` for a stored date, `""` for stored-everything, None when unset.
- [ ] Step 1: failing tests — set/get round-trip incl. ""-vs-None; gmail full enumeration honors explicit sync_query over env; explicit "" disables env window; Microsoft provider ignores the kwarg (interface test still green).
- [ ] Step 2: run → FAIL. Step 3: implement. Step 4: full suite green. Step 5: commit ("plumb per-user gmail sync window").

### Task 2: Range + preview endpoints

**Files:**
- Modify: `app.py`
- Test: `tests/test_sync_range.py` (new)

**Interfaces (consumed by Task 3 UI):**
- `GET /sync/range` → `{provider, sync_after, editable}` (editable false + reason for non-Gmail).
- `POST /sync/range` JSON `{after: "YYYY-MM-DD" | ""}` → stores via db.set_sync_after; if widening (new window older than current effective window, or "" when current is dated) AND no sync running (reuse /sync's running guards) → `db.clear_delta_links(user_id)` and respond `{stored: true, full_rewalk_next_scan: true}`; narrowing responds `{stored: true, full_rewalk_next_scan: false}`. Non-Gmail → 400 `{error: "sync range is gmail-only"}`. Sync running → 409.
- `GET /sync/range/preview?after=...` → one Gmail `messages.list` with `q=after:... (or none)` `maxResults=1`, returns `{estimated_threads: resultSizeEstimate, estimated_tagging_usd: round(est*0.0006, 2), note: "estimate"}`. Invalid date → 400.
- All three `@login_required`.
- [ ] Step 1: failing tests (mock provider/gmail HTTP; cover the four Review Focus items that belong here). Step 2: FAIL. Step 3: implement. Step 4: suite green. Step 5: commit ("add sync range endpoints with cost preview").

### Task 3: UI

**Files:**
- Modify: `templates/index.html`
**Interfaces:** consumes Task 2 endpoints exactly as specified.
- [ ] Step 1: For Gmail users, next to the Scan control: current-range line ("indexing mail since 2026-05-31" / "indexing everything") + "change" opens a small inline panel — presets Last year / Last 3 years / Everything / custom date; on selection fetch preview and show "≈ N threads, ≈ $X tagging (estimate)"; Save POSTs /sync/range; on `full_rewalk_next_scan` show "next scan will re-walk your mailbox — existing threads are kept, older mail is added"; narrowing shows "existing indexed mail is kept". Non-Gmail: the control renders disabled with "range control is Gmail-only". Match the app's existing dark style and copy voice (lowercase, terse).
- [ ] Step 2: manual verification against the local dev server with the real Gmail account (document in report: range read, preview numbers, widen flow up to — but NOT including — clicking Scan on the full mailbox). Full suite green (templates have JS-free tests only where they exist — don't invent a JS test harness).
- [ ] Step 3: commit ("add sync range control to the app").

### Task 4: CLI flag + docs

**Files:**
- Modify: `sync_cli.py`, `README.md`
- Test: `tests/test_sync_cli.py`
**Interfaces:** `--since YYYY-MM-DD | all` → db.set_sync_after (+"" for all) + clear_delta_links when widening and no cursorless crash when none exists; then proceeds into the normal sync path.
- [ ] Step 1: failing tests (flag stores setting; widen clears cursor; no-cursor case safe; invalid date exits 2 with message). Step 2: FAIL. Step 3: implement + README section ("choosing how far back to index": UI + CLI, gmail-only note, additive-widening explanation, estimate caveat). Step 4: suite green + scanner clean. Step 5: commit ("add --since to sync CLI and document range control").
