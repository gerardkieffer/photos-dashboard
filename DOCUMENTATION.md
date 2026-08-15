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
the read-only guarantees on your own machine, against your own library:

```bash
python3 test_readonly.py
```

## Running it

```bash
python3 photo_dashboard.py
```

Then open <http://localhost:8787/>. The server binds to `127.0.0.1` only, so it is not
reachable from other machines unless you deliberately pass `--host`.

Stop it with `Ctrl-C`. History is kept, so restarting resumes the charts where they
left off.

Useful variants:

```bash
python3 photo_dashboard.py --list                    # which libraries can be read
python3 photo_dashboard.py --library "/Volumes/Disk/X.photoslibrary"
python3 photo_dashboard.py --port 9000
python3 photo_dashboard.py --snapshot-mode           # never open the library at all
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
   SQLite cannot create or write `-wal`, `-shm` or journal files through it.
2. **`PRAGMA query_only`.** The connection itself refuses any statement that would
   write, including one added by mistake in future code.
3. **A SQLite authorizer.** A callback that runs *inside* SQLite as each statement is
   compiled, rejecting every action that is not a read. `INSERT`, `UPDATE`, `DELETE`,
   `CREATE`, `DROP`, `ATTACH` and every pragma outside a small read-only whitelist are
   refused before the statement exists.

All three would have to be removed deliberately for a write to become possible.

Deliberately **not** used: SQLite's `immutable=1`, which is faster but promises that
the file never changes. On a live library held by `photolibraryd` that promise is
false, and it would produce silently stale or torn reads.

### Verifying it yourself

`test_readonly.py` is an executable test rather than a claim in a comment:

```bash
python3 test_readonly.py
```

It does three things:

1. **Enforcement** — issues a real `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP` and
   two dangerous pragmas against your library and checks each is rejected, then checks
   that ordinary reads still work.
2. **Byte-for-byte** — clones your library's database files into a scratch bundle that
   nothing else on the machine has open, SHA-256s every file, runs *every* collector,
   and re-hashes. It compares content, size, modification time **and inode**, so even a
   no-op rewrite or a replaced file would be caught. It runs this twice: once in direct
   mode, once in snapshot mode.
3. **No new files** — confirms no `-wal`, `-shm`, journal or lock file was created.

Expected output ends with:

```
All checks passed: the dashboard reads and never writes.
```

### The one honest caveat

In direct mode, `Photos.sqlite` and its `-wal` come out **byte-identical** — that is
what the test asserts. But SQLite does update the `-shm` file.

The `-shm` is the *WAL index*: a derived scratch file that holds no library data, is
regenerated from the WAL whenever it is missing, and that every reader of a
write-ahead-logged database updates with its read mark — Photos itself included, and
any `sqlite3` command you have ever run against such a file. It is normal and safe.

It is not, however, the same thing as "untouched". If you want the stronger guarantee,
use snapshot mode.

### What else the tool touches

Outside the library bundle only:

- the history file under `snapshots/dashboard/` (see [Stored history](#stored-history));
- in snapshot mode, a scratch copy in the system temporary directory, deleted on exit;
- subprocesses: `log show`, `lsof`, `pgrep`, `defaults read`, and `cp` in snapshot mode.

It never moves, renames, deletes or writes anything in `originals/`, `resources/` or
anywhere else inside a `.photoslibrary` bundle.

## Snapshot mode

```bash
python3 photo_dashboard.py --snapshot-mode
```

Copies `Photos.sqlite`, `-wal` and `-shm` (and the search index) aside and reads the
copy, so **not one byte inside the library bundle is written** — including the `-shm`.
The library is only ever read, and copying is pure reading.

Getting this right required care: the readability probe and library discovery both used
to open the library to test it, and a *successful open* is exactly what updates the
`-shm`. In snapshot mode both go through the copy instead, and discovery falls back to
a permission check rather than an open.

**Cost.** All three files are copied together — copying `Photos.sqlite` alone silently
loses everything still in the WAL, which can be megabytes of recent edits (§2.4). Where
the scratch directory is on the same APFS volume as the library, `cp -c` makes a clone
instead: blocks are shared, nothing is duplicated, and it is effectively free. A 1 GB
library "copies" in less than a second using nearly no extra space. On a different
volume it is a real copy and correspondingly slower.

**Freshness.** Live metrics are only as current as `--snapshot-interval` (default 60 s),
so the queue chart becomes minute-resolution rather than ten-second. The header badge
shows which mode is active.

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
test_readonly.py            executable proof that nothing is written
```

**The sampler owns all reading.** HTTP handlers never touch the library: they serve the
last completed sample out of memory and the history out of SQLite. A slow `log show` or
a spun-down external disk therefore cannot make the page hang.

**Expensive collectors run on their own threads.** The unified-log read takes seconds to
tens of seconds, so it never delays the ten-second queue sample behind it.

**A supervisor owns the current sampler**, so switching library swaps the sampler, the
history file and the snapshot directory together under one lock. An HTTP request can
never observe a half-switched state.

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
| `--host ADDR` | `127.0.0.1` | Bind address. Localhost only by default |
| `--history PATH` | per library | Time-series store location |
| `--log-minutes N` | `5` | Window each `log show` query covers |
| `--log-interval N` | `180` | Seconds between unified-log passes |
| `--no-fs-walk` | off | Skip the filesystem inventory |
| `--snapshot-mode` | off | Read a copy; never open the library |
| `--snapshot-interval N` | `60` | Seconds between snapshot refreshes |
| `--list` | — | List libraries and whether each is readable |
| `--once` | — | Run every collector once, print JSON, exit |

## HTTP API

All responses are JSON. Everything is served from memory or the history file, so no
endpoint blocks on the library.

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
