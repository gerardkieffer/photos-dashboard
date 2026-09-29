# Photos Library Dashboard — documentation

Everything needed to set the dashboard up, use it, understand what it is telling you,
and fix it when it misbehaves.

- [Requirements](#requirements)
- [Installation](#installation)
- [Running it](#running-it)
- [Choosing which library to watch](#choosing-which-library-to-watch)
- [How the dashboard protects your library](#how-the-dashboard-protects-your-library)
- [Snapshot mode](#snapshot-mode)
- [Reading the dashboard](#reading-the-dashboard)
- [How progress is measured](#how-progress-is-measured)
- [Sampling tiers](#sampling-tiers)
- [Stored history](#stored-history)
- [How it works internally](#how-it-works-internally)
- [Command-line reference](#command-line-reference)
- [HTTP API](#http-api)
- [Reference](#reference)
- [Troubleshooting](#troubleshooting)

Section numbers written like §3.2 refer to `APPLE-PHOTOS-LIBRARY-INTERNALS.md`, the
measured technical reference included in this folder. The code comments cite the same
numbers.

---

## Requirements

| | |
|---|---|
| Operating system | macOS. The tool reads Apple Photos libraries and uses `log`, `lsof` and `pgrep`. |
| Python | 3.10 or newer (3.11+ recommended). `python3 --version` to check. |
| Dependencies | **None.** Standard library only — no `pip install`, no virtualenv, no build step. |
| Browser | Anything current. The page uses no frameworks and loads nothing from the internet. |
| Permissions | None at all for a library on an external volume. Full Disk Access for one under your home directory — see [Troubleshooting](#a-library-is-listed-as-blocked). |

The one thing worth knowing before you start: **a Photos library inside your home
folder is blocked by macOS** unless the program reading it has Full Disk Access. A copy
on an external disk needs no permission and is the easiest place to start (§17).

## Installation

There is nothing to install. Copy the folder anywhere and run it:

```bash
git clone https://github.com/gerardkieffer/photos-dashboard.git
cd photos-dashboard
python3 photo_dashboard.py --list
```

If you prefer, make the entry point executable once:

```bash
chmod +x photo_dashboard.py test_readonly.py
```

Optional but recommended before pointing it at a library you care about — this verifies
collector compatibility using a disposable backup of your library:

```bash
python3 test_readonly.py
```

## Running it

```bash
python3 photo_dashboard.py
```

Then open <http://localhost:8787/>. The server binds to `127.0.0.1` only, so it is not
reachable from other machines. Non-loopback `--host` addresses are rejected.

Stop it with `Ctrl-C`. History is kept, so restarting resumes the charts where they
left off.

Useful variants:

```bash
python3 photo_dashboard.py --list                    # which libraries can be read
python3 photo_dashboard.py --library "/Volumes/Disk/X.photoslibrary"
python3 photo_dashboard.py --port 9000
python3 photo_dashboard.py --snapshot-mode           # query consistent database backups
python3 photo_dashboard.py --once > report.json      # one pass, no server
```

### Leaving it running

The sampler is deliberately cheap: a full pass of every collector takes a few seconds,
and most passes take milliseconds. It is designed to be left running for days so the
history is worth looking at. The only collector with real cost is the unified-log read
(`--log-interval`, default every 180 s) and the filesystem walk (every 6 hours,
disable with `--no-fs-walk`).

## Choosing which library to watch

Three ways, in order of precedence:

1. **Explicit** — `--library "/Volumes/Disk/X.photoslibrary"`. Always wins.
2. **The picker in the page header** — switch at runtime without restarting. The
   sampler, the history file and the snapshot directory all move together. Only
   libraries that discovery already found can be chosen, so the endpoint cannot be used
   to point the server at an arbitrary path.
3. **Automatic** — with no `--library`, the readable library with the largest database
   is chosen. When more than one is readable the others are listed on startup.

`--list` shows everything found and whether it can actually be opened:

```
  Vacation.photoslibrary       readable  412.6 MiB
                               /Volumes/Disk/Vacation.photoslibrary
  Family.photoslibrary         BLOCKED (needs Full Disk Access)
                               /Users/you/Pictures/Family.photoslibrary
```

Discovery looks beside the tool, in its parent folder, in `~/Pictures`, and on every
mounted volume.

## How the dashboard protects your library

This is the part that matters most, so it is enforced rather than promised.

### Three independent guards

Every connection to a Photos database passes through all three:

1. **`mode=ro` in the connection URI.** The operating system file handle is read-only.
   The source database and WAL are read-only; SQLite may update shared-memory files.
2. **`PRAGMA query_only`.** The connection itself refuses any statement that would
   write, including one added by mistake in future code.
3. **A SQLite authorizer.** A callback that runs *inside* SQLite as each statement is
   compiled, rejecting every action that is not a read. `INSERT`, `UPDATE`, `DELETE`,
   `CREATE`, `DROP`, `ATTACH` and every pragma outside a small read-only whitelist are
   refused before the statement exists.

These guards protect library connections. History uses a separate writable connection
that rejects destinations inside libraries, hard links, and unrelated existing databases.

Deliberately **not** used: SQLite's `immutable=1`, which is faster but promises that
the file never changes. On a live library held by `photolibraryd` that promise is
false, and it would produce silently stale or torn reads.

### Verifying it yourself

Run synthetic regressions without opening a Photos library:

```bash
python3 -m unittest -v test_safety test_review_regressions
python3 test_docs.py
```

The tests cover rejected history destinations, database write guards, backups with WAL
commits between every copy chunk, interrupted queries/copies, failed library switches,
shutdown cleanup, consistent integrity checks, and HTTP access rules.

Optionally run `python3 test_readonly.py --library PATH` for collector compatibility.
It first creates a consistent read-only backup, without an extra source readability
probe, then runs all attempted writes against disposable copies only. The collector
checks convert those copies to WAL mode and keep committed WAL frames present while
comparing database/WAL contents before and after collection, in both access modes.
The fixture connections do not commit during these byte comparisons; concurrent
commits are tested separately. This is a regression check, not an unconditional proof.

### The one honest caveat

SQLite readers, including backup readers in snapshot mode, can create or update the
`-shm` WAL index. The source database and WAL are opened read-only. Read-only does not
mean no locks, no shared-memory writes, or no disk load. For zero access to the original
library, use an independent offline copy and keep a verified backup of your photos.

### What else the tool touches

- History under `snapshots/dashboard/`, or a validated `--history` destination.
- Snapshot databases in a private temporary directory, removed on normal shutdown.
- Read-only subprocesses: `log show`, `lsof`, `pgrep`, and `defaults read`.

It does not move, rename, delete or write original photos or media resources.

## Snapshot mode

```bash
python3 photo_dashboard.py --snapshot-mode --no-fs-walk
```

SQLite's backup API creates a consistent copy of each database while coordinating with
live writers. Main and search databases are individually consistent, not an atomic
cross-database snapshot. The source connections are read-only but may update `-shm`.
Raw copying of separate database/WAL files while Photos is active is not used.

Each backup holds an explicit source read transaction across small copy chunks, so
commits by other connections do not repeatedly restart the copy. WAL writers can keep
committing, but the retained read view can delay checkpoints and grow the source WAL.
On a database using rollback journaling, the read transaction can delay writer commits.

A new generation is published only after both backups succeed. Failed attempts leave
the previous generation intact and display an error. Before the first successful
backup, the dashboard reports starting or unavailable. The sampler performs backups
asynchronously, so the first copy does not hold the API lock. Backup attempts have a
120-second cooperative budget checked between chunks, including cancellation checks.
These checks cannot interrupt a stalled filesystem call. After an attempt ends, the
next attempt waits the configured interval, including after failure.

**Cost.** These are full database copies, not APFS clones. Photo and video originals
are not copied. Every successful refresh rewrites both databases in full, even if
unchanged; failed attempts can also write substantial partial copies before cleanup.
Budget disk space for two generations of the main and search databases, plus possible
source WAL growth. A slowly retiring library can temporarily retain its own generation
while a newly selected library starts.
`--snapshot-dir /existing/scratch/parent` selects a location outside any library;
the default is the system temporary directory. Normal shutdown cancels work and waits
for readers to finish before removing scratch data; a stalled disk can delay exit.
Crashes or forced termination may require manual removal of `photodash-snap-*`
directories after the dashboard has stopped.

**Freshness.** `--snapshot-interval` defaults to 300 seconds (five minutes).
It applies to attempts, including failures. `--snapshot-mode --once` also uses a backup.
Discovery in snapshot mode only lists candidate libraries; backup creation determines
whether access is actually permitted.

**Large libraries.** Use `--no-fs-walk` to omit the file inventory. When enabled, the
inventory runs on a separate worker at startup and every six hours. Database SELECTs
have a 15-second execution budget; lock waits are bounded separately. These limits
cannot interrupt a stalled filesystem call. Database size and asset/file counts matter
more than total original-media bytes; this has not been benchmarked on a 1.5 TB library.

## Reading the dashboard

The page is organised top to bottom from "what is happening now" to "what this library
contains". Most panels carry an **ⓘ** that explains the underlying mechanism, and the
**Reference** section decodes the values seen in your library.

| Section | Answers |
|---|---|
| Status tiles | Is work pending, is it moving, is anything allowed to run, is the library healthy? |
| Background job queue | How much work is outstanding, of what kind, how old, and for which photos |
| Analysis progress | How much of the per-asset analysis has genuinely completed, per worker |
| Can anything run? | The scheduler's verdicts, and which daemons hold the library |
| Write activity | Every save Photos makes, and which subsystem made it |
| Library content | Assets, faces, people, keywords, albums, resources |
| Database | Table census and the primary-key counter check |
| Storage | Bundle inventory, database file sizes, free space |
| Events | Notable transitions, with pointers to the relevant documentation |
| Collector diagnostics | Sampling tiers, per-collector cost, history store size |

Each chart has its own window selector, defaulting to a range that suits the data: the
queue to 1 hour, the search index to 24 hours, the content census to 30 days.

### The status tiles

- **Pending jobs** — rows in `ZBACKGROUNDJOBWORKITEM`. A large number is not by itself
  a problem; a freshly imported or newly promoted library legitimately queues hundreds
  of thousands. What matters is whether it is falling.
- **Completing (derived)** — see [How progress is measured](#how-progress-is-measured).
- **Drain ETA** — pending ÷ net drain rate. Treat it as "hours or weeks", not a date.
- **Scheduler** — whether analysis is permitted right now.
- **Library held by** — how many daemons have the library open. Zero means nothing will
  progress.
- **Search index** — rows in the index against assets in the library.
- **Keywords searchable** — how many keyword titles Photos can actually find.
- **Save integrity** — the check described under
  [Save integrity reports a fault](#save-integrity-reports-a-fault).

## How progress is measured

**Photos keeps no record of finished jobs.** Rows are deleted when done: no completion
log, no timestamp, no tombstone. So a completion rate has to be inferred, and the
dashboard says so wherever it shows one.

Between two samples:

```
pending_now = pending_prev + arrivals − departures
```

Arrivals are countable — rows whose `ZTIMESTAMP` falls inside the interval — so
departures follow. It is a sound estimate, not a counter Photos maintains.

For work that is genuinely, verifiably finished, use the **analysis coverage** panel
instead. `ZASSETANALYSISSTATE`, the table that sounds like it should answer this, is
empty in every library measured. But `ZMEDIAANALYSISASSETATTRIBUTES` has one row per
asset and one version column per analysis worker, left at zero until that worker has
processed the asset. A version greater than zero is direct evidence of completed work.
Video workers show recorded counts without a percentage because their eligible asset
population is not established. Workers with zero recorded versions show "no recorded
analysis"; this does not establish whether the worker is applicable or stalled.
In JSON, those percentages are `null`, and video-worker totals are also `null`.

## Sampling tiers

Facts about a library move at very different speeds. Queue depth changes every few
seconds; the number of photos changes over days. Sampling both at one interval would
either miss the fast signal or waste space on the slow one.

| Tier | Every | Raw kept | Compacted to | What |
|---|---|---|---|---|
| live | 10 s | 3 h | 1 min for 48 h, then 15 min for 30 d | queue depth, WAL size, save rate |
| medium | 60 s | 48 h | 15 min for 30 d, then 6 h for 400 d | search index, holders, log verdicts |
| slow | 15 min | 30 d | 6 h for 400 d | asset/face/keyword census, counter health |
| rare | 6 h | kept | — | filesystem inventory |

Compaction keeps **minimum and maximum** as well as the mean, so a 30-day chart still
shows spikes rather than averaging them into a flat line. It is idempotent and runs on
every sampler pass, touching only rows that have aged past their tier's raw window.

## Stored history

Samples are written to SQLite **outside** your photo library:

```
snapshots/dashboard/<library-name>-<inode>.db
```

One file per library, keyed by the database's inode. Two library paths can look alike
(§2.5 recommends identifying by inode for exactly this reason), and a shared store would
interleave two libraries' series under the same metric names — switching library would
draw a cliff from one library's asset count to another's.

The file **persists across restarts** and is reloaded automatically, so charts keep
their history when you stop and start the server.

These files contain data derived from your library — keyword titles, people's names,
original filenames, album names, library paths. The bundled `.gitignore` excludes
`snapshots/` for that reason. Do not publish them.

To start fresh, stop the server and delete the file. To keep history somewhere else,
pass `--history /path/to/file.db`.

## How it works internally

```
photo_dashboard.py          CLI: argument parsing, library selection, entry point
photodash/
  coredata.py               Core Data helpers: entity numbers, join tables, NFC
  collectors.py             every read of a library, one function per concern
  history.py                tiered time-series store, rollups and retention
  server.py                 sampler thread, supervisor, JSON API, static files
  docs.py                   the table of documentation pointers used by messages
  static/                   index.html, app.js, glossary.js, style.css
test_readonly.py            optional collector checks on disposable copies
test_safety.py              synthetic safety regressions (no Photos library needed)
test_review_regressions.py  concurrent backup, cancellation, lifecycle and recovery tests
```

**The sampler owns metric collection.** Snapshot and chart requests serve completed
samples and history. Library discovery and selection can inspect library paths and
may wait for disk access.

**Expensive collectors run on their own threads.** The unified-log read takes seconds to
tens of seconds, and filesystem inventory can be slow; each has its own worker.
Shutdown and switching retain resources until workers have stopped.

**A supervisor owns the current sampler.** It prepares replacement resources outside
the API lock and atomically publishes the new session. Preparation/start failures leave
the previous session running and report a switch error. Old sessions retire in the
background; cleanup waits for all their workers. A newly selected snapshot library whose
first backup fails reports unavailable rather than a successful empty sample.

### Rules the collectors follow

These come from `APPLE-PHOTOS-LIBRARY-INTERNALS.md` and are the difference between
correct numbers and confidently wrong ones:

- **Entity numbers are not stable** (§2.2). `Z_ENT` values change across a Repair
  Library even with an unchanged schema version — measured, Keyword moved 47 → 52. So
  they are resolved at read time on the connection that uses them, never cached.
- **Join tables are named after those numbers** (§2.3), so tables are found by pattern
  and columns are matched on the word they contain, not the number.
- **Compare text NFC-normalised** (§16). Values occur in both normalisation forms;
  comparing raw bytes produced ~1,200 phantom "missing" keywords in one measurement.
  This applies to filesystem paths too — see the note in the README about `lsof`.
- **Strings in `psi.sqlite` are NUL-terminated** (§19), so they are stripped before
  comparison or every exact match fails silently.
- **`-180.0` means "no location"** (§8), not NULL.
- **`MAX(Z_PK) > Z_PRIMARYKEY.Z_MAX` is a live fault** (§3.2), not cosmetic.

## Command-line reference

| Option | Default | Meaning |
|---|---|---|
| `--library PATH` | auto | The `.photoslibrary` bundle to watch |
| `--port N` | `8787` | HTTP port |
| `--host ADDR` | `127.0.0.1` | Only `127.0.0.1` or `localhost` accepted |
| `--history PATH` | per library | Time-series store location |
| `--log-minutes N` | `5` | Window each `log show` query covers |
| `--log-interval N` | `180` | Seconds between unified-log passes |
| `--no-fs-walk` | off | Skip the filesystem inventory |
| `--snapshot-mode` | off | Query consistent SQLite backups; source shared memory may change |
| `--snapshot-interval N` | `300` | Seconds between snapshot attempts |
| `--snapshot-dir PATH` | system temp | Existing scratch parent outside any library |
| `--list` | — | List libraries and whether each is readable |
| `--once` | — | Run every collector once, print JSON, exit |

## HTTP API

API responses are JSON. Requests require the per-run `X-PhotosDashboard-Token` header;
the browser reads the token from the dashboard page and supplies it automatically.
Host must match localhost or 127.0.0.1 on the bound port. Cross-origin/browser cross-site
requests are rejected, and POST requires JSON with a body no larger than 16 KiB.
The service is loopback-only, without TLS or remote-user authentication. Do not expose
it through proxies or tunnels. Processes running as your user can still access it.
Chart/snapshot endpoints use cached data; library discovery/selection may access disk.

| Endpoint | Purpose |
|---|---|
| `GET /api/snapshot` | The complete current state |
| `GET /api/series?metrics=a,b&window=3600` | Time series, auto-downsampled |
| `GET /api/events?limit=200` | Recorded state transitions |
| `GET /api/snapshot-history?name=queue_mix` | Historical composite snapshots |
| `GET /api/libraries` | Discovered libraries and the current one |
| `GET /api/docs` | The documentation-pointer table |
| `GET /api/health` | Liveness and last sample time |
| `POST /api/select-library` | Switch library. Body `{"path": "..."}` |

`--once` produces the same data as `/api/snapshot` without starting a server, which is
the easy way to script against it.

## Reference

### Job types and flags

`ZJOBTYPE` and `ZJOBFLAGS` route each work item to a worker. Apple publishes no mapping,
and this tool does not invent one. What *is* established:

- **Type 11 is cascade donation.** Settled by direct evidence: every row of this type
  carries the literal identifier `PLCascadeDonationRequired`, which names
  `PLBackgroundJobCascadeDonationWorker`. Donation is how Photos hands content to
  Spotlight.
- **Type 2 is mixed.** Mostly per-asset, but some identifiers are JSON objects such as
  `{"type":1700,"label":"Summer Fair"}` naming a place, event or category rather than a
  photo. Those inner numbers are **not** album kinds — only two of the eight observed
  exist in `ZGENERICALBUM.ZKIND`, and those albums have no title — so they are a
  separate taxonomy whose meaning is not established.
- **`ZJOBFLAGS` is a bitmask.** Every observed value decomposes cleanly into powers of
  two (3 = 1|2, 14 = 2|4|8, 1026 = 2|1024), so the dashboard shows the individual bits.
  What each bit means is not established.

The **Reference** section in the page also lists the background workers Photos runs,
read from the Objective-C class list of Apple's `PhotoLibraryServices` framework. Those
names are real, but which worker a given `ZJOBTYPE` routes to is not established, so the
list is a catalogue of the work Photos does — not a decoder for the numbers.

### The search index

`database/search/psi.sqlite` answers a question the main database cannot: *has Photos
actually noticed this metadata yet?*

Keyword indexing runs **independently of the analysis queue**. A backlog of hundreds of
thousands of pending jobs does not mean your keywords are unsearchable — measured on a
real library, keywords were essentially fully indexed while 48,330 analysis jobs were
still queued (§19).

Category numbers: **1200** is your own keywords, **1500** and **1501** are Apple's scene
recognition in singular and plural form. Others are not established.

The index's `assets` table can hold **more rows than the library has assets** — measured
at 40,589 distinct UUID pairs against 21,737 untrashed assets. Why is not establishable
from outside: §22 records that the byte order of the split UUID (`uuid_0`, `uuid_1`) was
never verified, so those rows cannot be joined back to `ZASSET` to find out whether they
are stale or cover something other than assets. The dashboard reports the two counts
rather than dividing them into a meaningless ">100% coverage".

### Other values

| Value | Meaning |
|---|---|
| `ZLOCALAVAILABILITY` | `1` the file is on this Mac, `-1` it is not (iCloud only) |
| `ZBUNDLESCOPE` | `0` your own library, `2` an iCloud Shared Album asset with no local file |
| `ZTRASHEDSTATE` | `1` in Recently Deleted |
| `ZHIDDEN` / `ZVISIBILITYSTATE` | Independent mechanisms, not two names for one thing |
| `ZADJUSTMENTSSTATE` | `0` unedited, `2` and `3` both mean edited |
| Latitude `-180.0` | The "no location" sentinel, not a coordinate |
| CP / MNP / AMNP | Can Proceed / May Not Proceed / Absolutely May Not Proceed |

---

## Troubleshooting

### A library is listed as BLOCKED

**Symptom.** `--list` shows `BLOCKED (needs Full Disk Access)`, or startup fails with
`ERROR: cannot read <path>`.

**Cause.** macOS TCC protects genuine Photos library bundles in protected locations.
This is not specific to this tool — it applies to anything reading them. Measured (§17):

| Location | Result |
|---|---|
| A library in `~/Pictures` | blocked |
| A folder merely *named* `*.photoslibrary` in `~/Pictures` | readable |
| A genuine library on an external volume | **readable** |

So neither the extension nor the location alone is the trigger: it is a genuine bundle
in a protected location. Being the System Photo Library is *not* the discriminator.

**Fixes**, easiest first:

1. **Work from an external volume.** A library there needs no permission grant at all,
   and is also the safest place to experiment.
2. **Grant Full Disk Access** to the program running Python — System Settings →
   Privacy & Security → Full Disk Access. Add your terminal app (Terminal, iTerm) or
   whichever app launches the script, then restart that app completely.

**Note.** A blocked library still passes `Path.exists()`; only an actual open reveals
it. And depending on how the process was launched, a blocked open can *hang* rather than
fail — waiting on a consent prompt nobody is looking at. The probe therefore runs under
a deadline, and an unanswered probe counts as unreadable. If a library you expect to be
readable shows as blocked, try again from a terminal where you can see prompts.

### No library was found

`--list` reports nothing. Discovery looks beside the tool, in its parent folder, in
`~/Pictures`, and on every mounted volume — one level deep in each.

- Is the external volume mounted? `ls /Volumes`
- Is the library nested deeper than one level? Pass `--library` with the full path.
- Is it a real bundle? It must contain `database/Photos.sqlite`.

### The library became unavailable

**Symptom.** The status turns red with `library unavailable`; the page keeps showing the
last good sample.

**Cause.** The store could not be opened. Almost always an external volume that was
unmounted, went to sleep, or was disconnected.

The sampler backs off and retries rather than hammering a missing disk, and recovers by
itself once the volume returns — no restart needed. If it persists, check the volume is
mounted and re-run `--list`.

### Save integrity reports a fault

**This is the most serious thing the dashboard can tell you, and it is not caused by
this tool** — it only reports it.

**Symptom.** The Save integrity tile reads `FAULT`, or a red banner names tables where
`MAX(Z_PK) > Z_MAX`. In Photos itself: typed titles or captions vanish when you press
Enter, and new keywords render greyed out and are then discarded.

**Cause.** Core Data does not use SQLite's `AUTOINCREMENT`. Every primary key is
allocated from `Z_PRIMARYKEY.Z_MAX + 1`, and that counter must be written back. If a
table's highest row exceeds its counter, the next save Photos attempts collides. Because
**every** save also writes a persistent-history transaction, this breaks *all* editing in
the library — not just the rows that were written (§3.2).

It is almost always caused by an external tool writing to the library without
maintaining the counters.

**Critically:** `PRAGMA integrity_check` returns `ok` in this state, so it cannot be
used to rule the problem out (§20.2). This comparison is the only check that catches it.

**What to do.** Read §3.2 and §20.2 before touching anything. The repair is to make
`MAX(Z_PK)` and `Z_MAX` agree again for the affected tables, which means either removing
the offending rows or advancing the counter — a write to your library, and therefore
something to do deliberately, from a backup, with Photos closed. This dashboard will not
do it for you. Confirm the diagnosis in the log first:

```bash
/usr/bin/log show --last 10m --predicate 'process IN {"Photos","photolibraryd"}' --style compact \
  | grep -iE "save failed|constraint|PHPhotosErrorDomain"
```

`Persitent History - Transaction constraint primary key violation` (Apple's own
spelling) confirms it.

### Nothing is progressing

**Symptom.** Pending jobs flat, completion rate zero, a banner saying nothing holds the
library.

**Cause.** A Photos library only gets background processing while a daemon has it open.
A **secondary** library gets none at all unless Photos.app is open on it. The System
Photo Library is held permanently (§2.5).

**Fixes.**

- Open Photos.app on that library and leave it open.
- Or promote it: Photos → Settings → General → *Use as System Photo Library*. This is a
  separate switch from iCloud Photos — a library can be the system library with iCloud
  off.

Note that work started by Photos.app is **cancelled when Photos quits**, so opening and
closing the app repeatedly makes no net progress.

If the holders panel says some processes could not be inspected, "no holders" cannot be
concluded — that is reported separately rather than being reported as unattended.

### Analysis never seems to run

**Symptom.** The library is held, the queue is large, but nothing drains. The scheduler
tile reads `fully throttled`.

**Cause.** Not a fault. The Duet Activity Scheduler evaluates each analysis job against
weighted policies, and the heaviest — **Device Activity** — refuses to run the work
while somebody is at the keyboard. Over a two-hour window of normal use, one measurement
counted 4,008 `MNP` decisions against 1,384 `CP` (§2.5).

**Fix.** Leave the machine genuinely alone: on mains power, sleep prevented, nobody
typing. Note that `caffeinate -s` was **not** sufficient in one test; the Energy Saver
settings were needed instead.

Expect this to be slow. Measured throughput while the machine was in use was roughly 2–7
queue items per minute against a queue of 48,000 — days of idle time, not an overnight
job.

Importantly, this does **not** block keyword search: indexing runs independently of the
analysis queue (§19).

### Scheduler shows "no decisions logged"

Usually normal — it means no analysis work was evaluated in the sampled window, which is
what you see when the queue is idle or the library is not being worked on.

If you expect verdicts and see none:

- The window may be too short. Try `--log-minutes 30`.
- Reading the unified log can be slow. Check the collector diagnostics panel: the `log`
  collector routinely takes 1–3 seconds and sometimes much longer.
- Verify by hand:

```bash
/usr/bin/log show --last 30m --predicate 'eventMessage CONTAINS "mediaanalysisd.photos"' --style compact | head
```

Note `log` alone hits a zsh builtin and fails; the absolute path is required (§20).

### Collector errors

The diagnostics panel lists any collector that raised, with a timestamp. The sampler
isolates each collector, so one failure leaves the rest working and the affected panel
simply goes stale.

Common causes: the volume disappeared mid-pass; `lsof` was denied; a table this macOS
version does not have. If one persists, `--once` prints the same collection with a full
traceback path to inspect.

### Keywords show as "not yet indexed"

Usually a matter of time — indexing is triggered per asset and catches up on its own.
Keywords added by writing directly to the database do get indexed, and quickly (§19).

If a keyword never appears, remember the comparison traps: strings in the index are
NUL-terminated and text occurs in both Unicode normalisation forms. The dashboard
handles both; a hand-rolled query against `psi.sqlite` frequently does not, and reports
phantom missing keywords.

### The page is blank or charts say "collecting…"

Charts need at least two samples. The live tier samples every 10 s, but the slow tier is
every 15 minutes and the filesystem inventory every 6 hours, so some panels are legitimately
empty for a while after a first start. The collector diagnostics panel shows each tier's
pass count.

If the page is entirely blank, check the terminal for a traceback and confirm the server
is listening:

```bash
curl -s http://127.0.0.1:8787/api/health
```

### Port already in use

```
OSError: [Errno 48] Address already in use
```

Another process — often a previous run — holds the port. Either stop it or use another:

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
python3 photo_dashboard.py --port 9000
```
