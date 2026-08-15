# Photos library dashboard

A live web dashboard for an Apple Photos library: what the background pipeline is
doing right now, what it has done, and whether it is able to do anything at all.

```sh
./bin/photo_dashboard.py                 # pick a readable library, serve on :8787
./bin/photo_dashboard.py --list          # what can this Mac actually read?
./bin/photo_dashboard.py --library "/Volumes/Disk/X.photoslibrary"
./bin/photo_dashboard.py --once          # every collector once, as JSON, no server
```

Then open <http://localhost:8787/>.

Python standard library only — no pip install, no CDN, no build step. The page is
served by the same process that does the sampling.

## Read-only

Every connection is opened `mode=ro`, nothing writes to the library, and no lock is
taken. It is safe to leave running while Photos.app is open and while the analysis
daemons are working. The only side effects are outside the bundle: the time-series
file under `snapshots/dashboard/`, and the `log show` / `lsof` / `pgrep` subprocesses.

## What it shows

| Section | Source |
|---|---|
| Background job queue | `ZBACKGROUNDJOBWORKITEM` — depth, mix by `ZJOBTYPE`/`ZJOBFLAGS`, age profile, per-asset attribution, derived completion rate |
| Analysis progress | `ZMEDIAANALYSISASSETATTRIBUTES` version columns, one per worker |
| Search index | `database/search/psi.sqlite` — size, coverage, keyword searchability |
| Can anything run? | unified log: Duet scheduler MNP/CP verdicts, plus which daemons hold the library |
| Write activity | `ATRANSACTION` / `ACHANGE` / `ATRANSACTIONSTRING` — every save, and which subsystem made it |
| Library content | assets, faces, people, keywords, albums, resources |
| Database | table census, and the `MAX(Z_PK)` vs `Z_PRIMARYKEY.Z_MAX` check |
| Storage | bundle inventory, database file sizes, volume free space |

## Sampling tiers

Facts about a library move at very different speeds, so each metric is sampled at a
cadence matched to it and compacted as it ages. Rollups keep avg/min/max, so a
30-day chart still shows spikes rather than averaging them flat.

| Tier | Every | Raw kept | Then | What |
|---|---|---|---|---|
| live | 10 s | 3 h | 1 min for 48 h, 15 min for 30 d | queue depth, WAL size, save rate |
| medium | 60 s | 48 h | 15 min for 30 d, 6 h for 400 d | search index, holders, log verdicts |
| slow | 15 min | 30 d | 6 h for 400 d | asset/face/keyword census, counter health |
| rare | 6 h | forever | — | filesystem inventory |

Each chart picks its own default window to match: the queue defaults to 1 hour, the
search index to 24 hours, the content census to 30 days.

`log show` is the one expensive collector (seconds to tens of seconds), so it runs on
its own thread at its own interval — `--log-interval`, default 180 s. The filesystem
walk is tens of thousands of `stat` calls; disable it with `--no-fs-walk`.

History lives in `snapshots/dashboard/<library>-<inode>.db`, one file per library.
Keyed by inode because two library paths can look alike, and because a shared store
would interleave two libraries' series under the same metric names.

## Things worth knowing

**Libraries under your home directory are unreadable** without Full Disk Access.
That is TCC, not a bug here: a genuine bundle in a protected location is blocked,
while the same bundle on an external volume needs no permission at all. `--list`
says which is which. Note that a blocked library still passes `Path.exists()` — only
an actual open reveals it — and that depending on how the process was launched the
blocked open can hang rather than fail, so the probe runs under a deadline.

**There is no record of finished jobs.** The queue table has no completion log and no
history. The "completing /min" figure is *derived*, from
`pending_now = pending_prev + arrivals − departures`, taking arrivals as the rows
whose `ZTIMESTAMP` falls in the interval. It is labelled as derived on the page.
The nearest thing to a real completion record is the per-worker analysis coverage
panel, which counts assets whose version column for that worker is non-zero.

**Job type and flag meanings are not established.** `ZJOBFLAGS` is a bitmask — every
observed value decomposes into powers of two — so the dashboard shows the bits and
declines to invent names for them.

## Notes measured while building this

Three things that go beyond what the internals document records:

- **`ZIDENTIFIER` is usually an asset UUID.** §19 says `ZBACKGROUNDJOBWORKITEM` has
  "no asset reference", which is true of its *columns* — but 154,154 of 190,682 rows
  in the reference library join directly to `ZASSET.ZUUID`. The queue is in practice
  attributable to individual photos, which is what the "assets with the most work
  queued" panel uses. The remainder are literals (`PLCascadeDonationRequired`) or
  JSON blobs (`{"type":1000,"label":"À la maison"}`).

- **§16's NFC/NFD trap applies to filesystem paths, not just metadata.** For a
  library called `Photothèque.photoslibrary`, `lsof` and `Path.glob` return different
  Unicode normalisations of the same name. A raw substring test matched **0 of 56**
  genuine hits, and the dashboard confidently reported that nothing held a library
  four daemons had open. Both sides are NFC-normalised before comparison.

- **`psi.sqlite`'s `assets` table can hold more rows than the library has assets** —
  40,589 distinct `(uuid_0, uuid_1)` pairs against 21,737 untrashed assets. Why is
  not established, and cannot be from here: §22 records that the byte order of the
  split UUID was never verified, so the rows cannot be joined back to `ZASSET` to
  tell whether they are stale or cover something other than assets. The dashboard
  reports the two counts rather than dividing them into a >100% "coverage".

## Files

```
bin/photo_dashboard.py        entry point / CLI
bin/photodash/collectors.py   every read of the library, one function per concern
bin/photodash/history.py      tiered time-series store, rollups and retention
bin/photodash/server.py       sampler thread + JSON API + static file serving
bin/photodash/static/         index.html, app.js, style.css
```

Section references throughout the code are to `APPLE-PHOTOS-LIBRARY-INTERNALS.md`.
