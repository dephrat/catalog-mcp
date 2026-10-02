# Catalog

[![ci](https://github.com/dephrat/catalog/actions/workflows/ci.yml/badge.svg)](https://github.com/dephrat/catalog/actions/workflows/ci.yml)

Search your own email by whatever you actually remember about it.

You rarely recall how a message was worded — you recall that it was *about the
car loan*, or *from the dentist*, or *had the bank statement attached*. Catalog
syncs a mailbox, extracts text from PDF and DOCX attachments, and has an LLM
generate search tags for every thread: topics, names, organisations, document
types, synonyms, plausible misspellings. Then it lets you search those.

On top of the tag index sits **Detective**, an agentic loop for the case where
you can't remember enough to search directly. You describe what you're looking
for in prose; it runs parallel queries, reads the results, follows leads it
finds in them, and either surfaces the thread or explains where the document
probably is and who to ask.

Built against a real 11,000-thread personal archive.

---

## How it works

```
Graph delta feed ──▶ changed thread ids
                         │
                         ▼
                  refetch whole threads ──▶ extract bodies + attachments
                                                      │
                                                      ▼
                                            Claude Haiku (batched)
                                                      │
                                                      ▼
                                            SQLite: threads + tags
                                                   │        │
                                        filtered search   Detective loop
```

**Sync is incremental.** Each mail folder has a delta cursor; a re-sync fetches
only what changed. Delta is used as a *change detector* rather than a data
source — it reports which conversations moved, then those conversations are
refetched in full, so thread reconstruction always sees complete context. On a
real mailbox this is the difference between re-reading 11k threads and reading
about nine.

A folder's cursor only advances once the threads it reported are in the
database. Moving it earlier is the one unrecoverable mistake available here:
the next scan would simply never mention that mail again. Re-reading a folder
costs one delta call and nothing else, so when a sync fails anywhere before
storage, every cursor is held and the next run re-reads.

**Tagging is batched.** A first-time import goes through Anthropic's Message
Batches API at half price. Threads are stored *before* tagging, so a large
import is searchable by subject and sender within minutes while tags fill in
behind it. Small incremental syncs stay on the real-time API, where the saving
is pennies and the latency is not.

**Providers are abstracted.** Change detection is modelled as *sources with
cursors*, not folders with tokens, because Microsoft Graph exposes a delta feed
per folder while Gmail exposes one mailbox-wide history feed. A provider
needing a single cursor returns a single source. Messages are normalised at the
provider boundary so nothing above it knows which mail API it is talking to.

## Detective

A prose description goes in; the loop runs up to 20 rounds, each issuing three
queries in parallel with independent filters. Results are merged, deduplicated
and summarised back into the conversation, so round *n+1* reasons over
everything found so far. It terminates when the model concludes, when queries
run dry, or at the round cap.

The system prompt and the growing history are cached across rounds, which
matters: history is resent from the top every round, so cost grows
quadratically without it.

## MCP server

Catalog exposes the tag index to agent platforms as a read-only MCP server.
Four tools let an agent search fast (local index only), list tags, check catalog
health, or fetch full thread content live on 1–3 finalists. The server is
agent-native: it works the same whether called from Claude Code, Claude Desktop,
or any MCP-compatible client.

### Register with Claude Code

```bash
claude mcp add catalog -- $PWD/.venv-mcp/bin/python $PWD/mcp_server.py
```

Replace `$PWD` with the absolute path to this repo, or run it as-is from the
repo root. The server requires a dedicated venv; see Task 4's notes on
setup (`requirements-mcp.txt`, `.venv-mcp`).

### Register with Claude Desktop

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`
(create it if it does not exist; replace `$HOME` with your home directory):

```json
{
  "mcpServers": {
    "catalog": {
      "command": "python",
      "args": ["$HOME/path/to/catalog/mcp_server.py"],
      "env": {
        "CATALOG_USER": "user@example.com"
      }
    }
  }
}
```

(If the file already exists, merge this under `mcpServers` alongside any other
servers.)

### Configuration

**`CATALOG_USER`** (optional): The email address whose catalog the server
accesses. If unset, the server assumes a single-user deployment and uses the
sole user in the database. If multiple users are stored and `CATALOG_USER` is
not set, the server exits with an error.

### Design: read-only, no sync trigger

The server **never** writes to the database or triggers a mailbox sync. This
split is deliberate: if an agent were to trigger a sync, it could run
double-time alongside the web app's own sync, duplicating work and charges.
Instead, run `sync_cli.py` separately to populate or refresh the catalog before
using the server.

```bash
python sync_cli.py              # syncs CATALOG_USER if set, else the sole user
python sync_cli.py --user user@example.com  # syncs a specific account
```

`sync_cli.py` exits 0 on success, 2 if credentials are stale (re-sign in via the
web app) or 3 if a sync is already running for that account.

## Performance

Measured on the 11,000-thread corpus, p50 / p95 over 30 runs:

| Query | Matches | p50 | p95 |
|---|---|---|---|
| two terms, narrow | 15 | 78ms | 319ms |
| one term, broad | 1,409 | 67ms | 186ms |
| matches nearly everything | 11,109 | 33ms | 72ms |

Search goes through SQLite FTS5, which matches tokens rather than
substrings. That distinction turned out to matter more than it sounds: on the
real corpus `LIKE '%car%'` returned 2,593 threads, of which 41 actually
concerned a car — the rest were `carpet`, `scarf`, `Carol`. `man` returned
1,356 matches and none were genuine. Short queries were noise.

| query | matched before | matches now | without the token |
|---|---|---|---|
| `car` | 2,593 | 160 | 0 |
| `art` | 1,537 | 146 | 0 |
| `man` | 1,356 | 8 | 0 |
| `dentist` | 26 | 24 | 0 |

The index is maintained by triggers rather than by the write helpers, because
tags are also written by `set_thread_tags` and rows removed by `delete_thread`
and `wipe_db` — an index that quietly misses one of those paths is worse than
none. Availability is read from the database rather than a module flag, so a
database without the index degrades to substring matching instead of failing.

The database runs in WAL mode. A sync writes from several worker threads, and
under the default rollback journal a second writer can get `SQLITE_BUSY`
immediately rather than waiting out its timeout.

Results are capped at 200 per page. Before that cap, a broad query took 3.7s —
not from the query, but from serialising and rendering every match.

## Try it in one minute

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py --demo
```

No configuration, no mailbox, no keys. `--demo` seeds a fabricated catalog —
326 threads spanning a decade of one invented household's mail: the dentist,
the bank, the plumber, the school, a sister planning midsummer, and the bulk
mail around all of it — then serves the real app against it. Search and every
filter are fully live; try `honda car loan`, `water heater invoice`, or
`annika midsummer`. Detective works too if `ANTHROPIC_API_KEY` is set.

Then open http://127.0.0.1:5000. On macOS that port is usually taken by
AirPlay Receiver, so `PORT=5001 python app.py --demo` if it refuses to bind.

Demo mode cannot be reached in a deployment: it requires being executed as a
script with the flag, and gunicorn rejects unknown arguments, so a worker
cannot start with `--demo` and no environment variable can switch it on. It
also writes to its own `demo_catalog.db`, never a real catalog.

## Setup with a real mailbox

```bash
cp .env.example .env      # then fill it in
python app.py
```

Requires an Azure app registration for personal Microsoft accounts
(`Mail.Read`, `User.Read`) and an Anthropic API key. `.env.example` documents
every variable, including which Azure field is which — the client secret is the
**Value** column, not the Secret ID, and it is shown exactly once.

`ADMIN_EMAIL` is required. Access control is fail-closed: with it unset, nobody
can sign in, including you.

### Gmail

Gmail is a second provider behind the same abstraction; sign in at
`/login?provider=gmail` (the plain `/login` stays Microsoft, the default
provider). Setup, in Google Cloud Console:

1. Create a project, then under **APIs & Services** enable the **Gmail API**.
2. Configure the OAuth consent screen (External is fine; while the app is in
   *Testing* status, add your own address as a test user — refresh tokens for
   testing apps expire after 7 days, so publish the app for real use).
3. **Credentials → Create credentials → OAuth client ID → Web application**,
   with your `REDIRECT_URI` (e.g. `http://localhost:5000/callback`) as an
   authorised redirect URI.
4. Put the client id and secret in `.env` as `GOOGLE_CLIENT_ID` and
   `GOOGLE_CLIENT_SECRET`.

The only scope requested is `gmail.readonly`. The signed-in identity is the
Gmail address itself, so add it to `ADMIN_EMAIL` (comma-separated alongside
the Microsoft one) or approve it at `/admin`. Change detection uses the
mailbox-wide history feed; Gmail only retains history for about a week, so a
long pause between syncs triggers an automatic full re-enumeration — cheap,
because unchanged threads are dropped before tagging.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

328 tests, no network: the mail providers, the Anthropic client and the Graph
and Gmail transports are all stubbed, so the suite runs offline in about fifteen seconds.
CI runs the suite plus both secret scans (tracked files and full history) on
every push — the pre-commit hook only protects clones that opted in via
`core.hooksPath`, so the same gates run where nothing can skip them.

Most of them encode a specific failure this project has already had — a
re-sync wiping tags it had paid for, a partial batch marking the remainder
done, per-item throttling hidden inside a successful `$batch` response, a
delta cursor advancing past mail that was never stored. Reverting any of
those fixes turns a test red, which is the only real evidence a regression
suite works.

The sync tests drive `run_sync` through the provider interface rather than
through Graph, so a second provider inherits them.

## Spending

Tagging and Detective cost money, and it is the operator's money regardless of
whose mailbox is being indexed. Approval is binary — it says who may sign in,
not how much they may spend.

`USER_SPEND_LIMIT_USD` caps each account's spend for the calendar month.
Reaching it pauses syncing and Detective for that account; search keeps working
on everything already indexed. Admins are exempt, since the limit exists to
bound guests rather than to interrupt the person paying. Unset means no limit,
which is the right default for a single-operator instance and the wrong one the
moment anybody else is approved.

Measured figures, so the number can be chosen rather than guessed: an
11.6k-thread mailbox costs about **$6.65** to tag through the Batch API, or
**$14.31** in real time; a Detective session runs **$0.10–0.30**.

## Backups

```bash
python backup.py                  # take one, keeping the last 7
python backup.py --list
python backup.py --restore catalog-backup-….db.gz --yes
```

The app also takes one itself every `BACKUP_INTERVAL_HOURS` (24 by default, 0
to turn it off), from a timer thread rather than a platform cron job — job
state already lives in this process, and the single gunicorn worker means
exactly one scheduler with no coordination to get wrong. `/admin` reports the
newest snapshot's age, read from the files on disk rather than from a counter
the process keeps, so a scheduler that has died shows an ageing backup instead
of a reassuring number.

Snapshots use SQLite's online backup API rather than a file copy, because
`cp` on a live database can capture a write in progress and produce a file
that only fails when you finally need it. Each one is opened, integrity
checked and row-counted before it is kept — an unverified backup is a guess.
The 11k-thread catalog compresses to about 3 MB.

A restore keeps the database it replaces, so restoring the wrong snapshot is
itself recoverable. Snapshots land beside the database by default, which
covers a bad import or a wipe; pass `--dir`, or copy them elsewhere, to cover
losing the disk.

## Access control

Catalog is multi-tenant — each signed-in account gets an isolated catalog — so a
public deployment would otherwise let anyone index their mailbox on the
operator's API key. Sign-in therefore creates an approval request; an
unapproved account gets no session at all. Approve or deny from `/admin`, or
from emailed links that are HMAC-signed with an expiry and apply on POST rather
than GET, because mail scanners prefetch links and would otherwise approve
every request that reached an inbox.

The sign-in itself carries a random `state` through the OAuth round trip,
checked on return and spent once. Without it an attacker can hand a victim a
callback URL carrying the attacker's authorisation code, and the victim ends
up holding a session bound to someone else's mailbox — indexing it on the
operator's key.

## Operational notes

A few things this handles because they actually happened:

- A deploy mid-batch kills the polling thread. Batch ids are persisted and
  collected at startup; progress carries a heartbeat so a dead sync is
  distinguishable from a slow one, and the UI offers a restart instead of a
  frozen bar.
- Graph returns `429 ApplicationThrottled` for individual items *inside* an
  otherwise-successful `$batch` response. Those are retried with backoff rather
  than recorded as permanent failures, which would silently drop attachment
  text.
- A transient API error must not delete a pending batch record — the batch is
  probably finished and already billed. Records are kept and retried, and
  dropped only past the results retention window.
- Catalogs export and import with tags intact, so moving one between machines
  costs nothing instead of re-running the tagger.

## Secret scanning

`scripts/check_secrets.py` blocks secrets and real mailbox data at commit time.
It reads file *bytes* rather than diffs, because a vim swap file is binary: git
prints "Binary files differ" and a diff-based grep sees nothing while a live
credential sits in the blob.

```bash
git config core.hooksPath .githooks     # enable the pre-commit hook
python scripts/check_secrets.py --all      # tracked files
python scripts/check_secrets.py --history  # every blob in every commit
python scripts/check_secrets.py --tree DIR --strict   # a publish candidate
```


## Limitations

- Substring matching, not full-text search. No stemming, no ranking.
- No pagination beyond the 200-result cap.
- Detective's breadth control is prompt-driven; the only mechanical signal is a
  result-count threshold.
- Single gunicorn worker by design — job state is in-process. Concurrency comes
  from threads.
- Attachment extraction covers PDF and DOCX only.

## Design notes

[`NOTES.md`](NOTES.md) covers why the architecture is shaped this way — the
delta-as-change-detector decision, measured tagging costs and the levers that
were rejected on the numbers, where prompt caching helps and where it cannot,
and the production failures that shaped the code.

## License

Apache 2.0. You can run it, fork it, and build on it. Keep the copyright
notice and the `NOTICE` file with anything you redistribute, and say so if you
modified it.

Worth being clear about what that does and does not cover: the licence governs
this code, not the design. If you want to build your own agentic search loop
over a tagged archive, the README describes how this one works and you owe
nothing for reading it.

## Stack

Python 3, Flask, SQLite, Jinja2, vanilla JavaScript (no build step). Microsoft
Graph for mail, Anthropic Claude for tagging and Detective.
