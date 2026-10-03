# Catalog

[![ci](https://github.com/dephrat/catalog-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/dephrat/catalog-mcp/actions/workflows/ci.yml)

Catalog is an MCP server for agent-native recall over your own email
archive: search by whatever you actually remember about a message, not
how it was worded.

You rarely recall how a message was worded — you recall that it was
*about the car loan*, or *from the dentist*, or *had the bank statement
attached*. Catalog syncs a mailbox, extracts text from PDF and DOCX
attachments, and has an LLM generate search tags for every thread:
topics, names, organisations, document types, synonyms, plausible
misspellings. It then serves that tag index to any MCP-compatible
client — Claude Code, Claude Desktop, or your own agent — as four tools
with structured output, a static resource and a resource template, a
prompt, and completions.

On top of the same index sits a web app, with an agentic search loop
called Detective for the case where you can't remember enough to search
directly. That story — how the tagging pipeline works, the web UI,
backups, access control — is further down.

Built against a real 11,000-thread personal archive. That archive (a
Microsoft/Graph mailbox) is what the search layer itself was built and
latency-benchmarked against (see Performance, below); the worked example
and the 7/8 agent benchmark just below run against a separate, smaller
1,834-thread Gmail catalog instead.

---

## Worked example

A real `search_catalog` call against this repo's own catalog (size
below, under Indexing cost). `scripts/mcp_smoke.sh` speaks the same
JSON-RPC shape against a seeded test database; this is the real one.

Request:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_catalog","arguments":{"query":"registration ontario","limit":5}}}
```

Response — trimmed to two of the seven real matches; the business
number in the subject/tags is redacted below since this is a public
repo:

```json
{
  "threads": [
    {
      "thread_id": "19e84881f8ee5bab",
      "subject": "Ontario Business Registry Notice: Important Information Regarding Your Business Number Information",
      "participants": ["you@example.com", "daniel@ephrat.ai", "notify@example.ontario.ca"],
      "date_first": "2026-06-01T18:52:53+00:00",
      "date_last": "2026-06-02T21:56:27+00:00",
      "tags": ["ontario business registry", "business number", "compliance",
               "serviceontario", "registration", "incorporation"],
      "has_attachments": false,
      "web_link": "https://mail.google.com/mail/?authuser=you@example.com#all/19e84881f8ee5bab"
    },
    {
      "thread_id": "19e6a3c7bd55418f",
      "subject": "EPHRAT AI [BIN REDACTED] Registration of Sole Proprietorship",
      "participants": ["registry@example.ontario.ca", "you@example.com", "daniel@ephrat.ai"],
      "date_first": "2026-05-27T16:20:08+00:00",
      "date_last": "2026-06-02T05:42:06+00:00",
      "tags": ["business registration", "sole proprietorship", "ontario",
               "certificate", "business registry", "renewal", "incorporation"],
      "has_attachments": false,
      "web_link": "https://mail.google.com/mail/?authuser=you@example.com#all/19e6a3c7bd55418f"
    }
  ],
  "total": 7
}
```

Narrowed to one finalist, `get_thread` fetches it live from the mailbox
— the one tool that hits the network; everything above came from the
local index:

Request:

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_thread","arguments":{"thread_id":"19e6a3c7bd55418f"}}}
```

Response — body text trimmed, identifying numbers redacted:

```json
{
  "subject": "EPHRAT AI [BIN REDACTED] Registration of Sole Proprietorship",
  "web_link": "https://mail.google.com/mail/?authuser=you@example.com#all/19e6a3c7bd55418f",
  "messages": [
    {
      "from": "registry@example.ontario.ca",
      "to": ["you@example.com"],
      "date": "2026-05-27T16:20:08+00:00",
      "body_text": "Entity Name: EPHRAT AI  BIN: [redacted]  Transaction Number: [redacted]  Dear ..."
    },
    {
      "from": "you@example.com",
      "to": ["daniel@ephrat.ai"],
      "date": "2026-06-02T05:42:06+00:00",
      "body_text": "---------- Forwarded message --------- From: <registry@example.ontario.ca> ..."
    }
  ],
  "attachments": []
}
```

That's the whole loop an agent runs: a fast local search to narrow
candidates, then one live fetch to confirm.

## Benchmark

catalog vs. a raw-Gmail MCP server, same 8 questions, same model
(Sonnet), same harness (`bench/run.py`), hand-graded:

| | hit rate | mean wall time | median wall time | cost/question |
|---|---|---|---|---|
| catalog | 7/8 | 14.8s | 12.7s | ~$0.08 |
| raw Gmail | 4/8 (3 partial) | 21.8s | 17.0s | ~$0.06 |

n=8, one model, one mailbox, and the index covers mid-2026 onward (a
handful of threads earlier) — small enough to call a direction, not a
proof.

## Indexing cost

The first-ever import of a mailbox is bounded by Gmail's per-user API
quota, not by CPU: expect hours for a large mailbox, plus roughly $1–2
of batch tagging. Steady-state sync is fast — a measured run against
this repo's own mailbox synced a week-plus of new mail, including batch
tagging, in 5m46s. As measured after that sync, the catalog behind the
worked example above held 1,834 threads with 0 untagged.

## What the server exposes

- **Tools** — `search_catalog`, `list_tags`, `sync_status`, `get_thread`,
  each advertising a structured `outputSchema` (and a matching
  `structuredContent` echo on every call), not a bare text blob.
- **Resources** — `catalog://tags` (the tag vocabulary, static) and
  `catalog://thread/{thread_id}` (a resource-template mirror of
  `get_thread`), so a client can pull context without a tool call.
- **Prompt** — `find_document(description, tag="")`, a user-invocable
  template that instructs the model to search tags-first and reserve
  `get_thread` for at most two finalists.
- **Completions** — the `find_document` prompt's `tag` argument
  autocompletes from the live tag index.
- **Progress** — `get_thread`, the one tool that makes a live network
  call, reports progress before and after the fetch.

## MCP server

### Setup: a dedicated venv

The server needs its own virtualenv, on Python 3.10+ (the `mcp` SDK's
floor) — separate from the 3.9 venv the rest of this project (and its test
suite) runs on:

```bash
brew install python@3.12   # if you don't already have a 3.10+ interpreter
python3.12 -m venv .venv-mcp
.venv-mcp/bin/pip install -r requirements-mcp.txt
```

### Register with Claude Code

```bash
claude mcp add catalog -- $PWD/.venv-mcp/bin/python $PWD/mcp_server.py
```

Replace `$PWD` with the absolute path to this repo, or run it as-is from the
repo root.

### Register with Claude Desktop

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`
(create it if it does not exist; replace `/path/to/catalog-mcp` with the
absolute path to this repo):

```json
{
  "mcpServers": {
    "catalog": {
      "command": "/path/to/catalog-mcp/.venv-mcp/bin/python",
      "args": ["/path/to/catalog-mcp/mcp_server.py"],
      "env": {
        "DB_PATH": "/path/to/catalog-mcp/catalog.db",
        "CATALOG_USER": "user@example.com"
      }
    }
  }
}
```

`"command": "python"` will not work here: the system `python` almost
certainly lacks the `mcp` package, and the SDK itself requires Python 3.10+.
Point `command` at `.venv-mcp/bin/python` directly, with an absolute path —
Claude Desktop does not run this with the repo as its working directory, so a
relative path (or a bare `python`) resolves against the wrong interpreter, and
with no `DB_PATH` the server would otherwise default to a `catalog.db` next to
`mcp_server.py` itself, which happens to be correct only because the env
block above sets it explicitly; see Configuration below for what happens if
you leave it out instead.

(If the file already exists, merge this under `mcpServers` alongside any other
servers.)

### Configuration

**`DB_PATH`** (optional): Absolute path to the catalog database. If unset,
the server defaults to `catalog.db` next to `mcp_server.py` (i.e. this repo),
which is correct for Claude Code (registered from the repo root above) but
worth setting explicitly for Claude Desktop or any client that may launch the
process from an unrelated working directory.

**`CATALOG_USER`** (optional): The email address whose catalog the server
accesses. If unset, the server assumes a single-user deployment and uses the
sole user in the database. If multiple users are stored and `CATALOG_USER` is
not set, the affected tool calls return `{"error": "multiple users; set
CATALOG_USER"}` — the server process itself keeps running either way, since
each tool call resolves the user independently. Resources degrade the same
way, returning the same `{"error": ...}` shape serialized into their JSON
body instead of a protocol-level failure; completions have no error slot in
the protocol, so the same misconfiguration just returns an empty values
list instead.

### Design: read-only, no sync trigger

The server **never** writes to the database or triggers a mailbox sync. This
split is deliberate: if an agent were to trigger a sync, it could run
double-time alongside the web app's own sync, duplicating work and charges.
Instead, run `sync_cli.py` separately to populate or refresh the catalog before
using the server.

```bash
.venv/bin/python sync_cli.py              # syncs CATALOG_USER if set, else the sole user
.venv/bin/python sync_cli.py --user user@example.com  # syncs a specific account
```

You don't have to wait for the first import to finish before searching:
threads are stored before tagging and the database (SQLite in WAL mode)
serves readers while the sync writes, so the MCP server answers queries
over whatever has landed so far. While a sync is running, `sync_status`
carries a `sync_in_progress` field with live counts so an agent can tell
the user results may still be partial.

`sync_cli.py` exits 0 on success, 1 if the sync itself failed partway through
(see server logs / the `sync_state` table), 2 if credentials are missing or
stale or `CATALOG_USER`/`--user` doesn't resolve to a stored user (re-sign in
via the web app), or 3 if a sync already looks to be running for that account
— checked both in-process and against the same DB-backed heartbeat the web
app's own `/sync` route trusts, so this also catches a sync the web app
started in a different process.

---

## The web app

Everything below is how the catalog actually gets built, and how a
human (rather than an agent) browses it directly: the sync pipeline, the
Detective loop, the Flask UI, backups, access control. The MCP server
above is read-only and depends on this having been run at least once.

### How it works

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

### Detective

A prose description goes in; the loop runs up to 20 rounds, each issuing three
queries in parallel with independent filters. Results are merged, deduplicated
and summarised back into the conversation, so round *n+1* reasons over
everything found so far. It terminates when the model concludes, when queries
run dry, or at the round cap.

The system prompt and the growing history are cached across rounds, which
matters: history is resent from the top every round, so cost grows
quadratically without it.

### Performance

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

### Try it in one minute

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

### Setup with a real mailbox

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

#### Gmail

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

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

379 tests, no network: the mail providers, the Anthropic client and the Graph
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

### Spending

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

### Backups

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

### Access control

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

### Operational notes

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

### Secret scanning

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

### Limitations

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
