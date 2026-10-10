# Sync Range — Spec

Let a Gmail user choose how far back Catalog indexes, from the app
itself: pick a range before the first import (with a cost preview), and
widen it later with one click — which is also how an existing user
imports the rest of their inbox. Born from a real failure: the owner's
wifi-password email predates the hardcoded June-2026 env-var window, and
widening required SSH and a hand-cleared cursor.

## Scope

Gmail only (Gmail's `q=after:` is a native server-side filter; Microsoft
Graph's delta feed has none). Microsoft accounts see the control
disabled with an honest "Gmail only" note. The `GMAIL_SYNC_QUERY` env
var remains as a deployment-level default, overridden by the per-user
setting when one exists.

## Behavior

1. **Per-user setting** `sync_after` (ISO date string or "" = everything),
   stored in the existing `sync_state` table. Read by `run_sync` and
   passed down to the Gmail provider per call — never via process env.
2. **First import UI** (Gmail, zero threads): next to "Scan my emails",
   a range selector — Last year / Last 3 years / Everything / custom
   date — defaulting to Everything, plus a cost preview fetched on
   selection: estimated thread count (Gmail's `resultSizeEstimate` for
   the chosen window, one cheap API call) and estimated tagging dollars
   (count x the measured ~$0.0006/thread, labeled an estimate).
3. **Widening** (Gmail, threads exist, user picks an older/"" range):
   stores the setting, clears the Gmail history cursor so the next scan
   does a full walk with the wider window, and tells the user what will
   happen ("next scan re-walks the mailbox; existing threads are kept
   and not re-tagged; only older mail is added"). Dedup by thread id
   makes widening purely additive. The same Scan button runs it.
4. **Narrowing never deletes.** A narrower range only bounds FUTURE full
   walks; indexed threads stay. Copy says so.
5. **CLI parity**: `sync_cli.py --since YYYY-MM-DD` and `--since all`
   set the same per-user setting (including the widen-clears-cursor
   rule) before running.

## Constraints

- MailProvider interface stays provider-agnostic: the Gmail provider
  gains an optional per-call sync-window argument; Microsoft ignores the
  feature entirely (no interface breakage — verify the interface
  conformance test still passes).
- Full suite green (382 now); scanner clean; MCP server untouched.
- No new dependencies.
- Cost preview is an estimate and must be labeled one
  (resultSizeEstimate is approximate; tagging price shifts with thread
  size).

## Acceptance

A Gmail user with a June-2026-windowed catalog opens the app, picks
"Everything", sees an estimate, clicks Scan, and older mail flows in
with no SSH, no env edit, no cursor surgery. Tests cover: setting
stored/read per user; widen clears cursor exactly when the new window is
older; narrow doesn't; Microsoft path untouched; CLI flag equivalence.
