# Photos Library Dashboard

A live, read-only web dashboard for an Apple Photos library — what its background
pipeline is doing right now, what it has already done, and whether it is able to do
anything at all.

Photos does an enormous amount of work behind your back: indexing for search, detecting
faces, classifying scenes, rendering thumbnails, uploading to iCloud. It tells you
almost nothing about it. This shows you.

![macOS](https://img.shields.io/badge/macOS-Sequoia%20%7C%20Tahoe-lightgrey)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![Read only](https://img.shields.io/badge/library%20access-read--only%20(verified)-brightgreen)

---

## Why

If you have ever wondered why Photos has been "still analysing" for three days, why a
keyword you added is not searchable, or why the fans are spinning after an import —
the answers exist, scattered across a Core Data store, a private search index and the
unified log. None of them are visible in the app.

This gathers them into one page and, just as importantly, **tells you when something is
not knowable** rather than inventing a plausible number.

## Features

- **Background job queue** — depth, mix by type and flags, age profile, and which
  photos have the most work outstanding
- **Analysis progress** — per-worker completion, from the one place in the database that
  genuinely records finished work
- **Search index** — size, coverage, and exactly which keywords Photos cannot yet find
- **Can anything run?** — the Duet scheduler's verdicts from the unified log, plus which
  daemons hold the library. This is the usual answer to "why is nothing happening"
- **Write activity** — every save Photos makes, and which subsystem made it
- **Library census** — assets, faces, people, keywords, albums, resources
- **Save-integrity check** — catches a specific corruption that silently breaks *all*
  editing in a library, and that `PRAGMA integrity_check` reports as `ok`
- **Real history** — sampled at a cadence matched to each kind of data, compacted as it
  ages, stored in SQLite and reloaded across restarts
- **Explanations built in** — every panel has an ⓘ explaining the mechanism behind it

## Requirements

macOS, Python 3.10+, a browser. **No dependencies** — standard library only, no
`pip install`, no build step.

## Quick start

```bash
git clone https://github.com/<you>/photos-dashboard.git
cd photos-dashboard
python3 photo_dashboard.py --list
python3 photo_dashboard.py
```

Open <http://localhost:8787/>. Binds to localhost only.

If `--list` shows your library as `BLOCKED`, that is macOS TCC, not a bug — see
[DOCUMENTATION.md](DOCUMENTATION.md#a-library-is-listed-as-blocked). A library on an
external volume needs no permission at all.

## Your library is never modified

This reads a database that is difficult to replace and impossible to un-corrupt, so
read-only is **enforced three independent ways**, not promised once:

1. `mode=ro` on the connection URI — the OS file handle is read-only
2. `PRAGMA query_only` on the connection
3. a **SQLite authorizer** that rejects every non-read action inside SQLite, before a
   statement is even compiled

And it ships with an executable proof rather than a claim:

```bash
python3 test_readonly.py
```

which issues real writes and checks each is refused, then clones a library, SHA-256s
every file, runs every collector, and re-hashes — comparing content, size, mtime **and
inode**.

**Direct mode leaves `Photos.sqlite` and its WAL byte-identical.** One honest caveat:
SQLite does update the `-shm` file, the WAL index — a derived scratch file holding no
library data that *every* reader of a WAL database touches, Photos included.

**`--snapshot-mode` avoids even that**, reading a copy so nothing inside the bundle is
written. On the same APFS volume the copy is a clone: a 923 MB library "copies" in
0.27 s using no extra space.

## Screenshot

Run it and see — the dashboard renders live data from your own library, so a screenshot
here would only show someone else's photo counts.

## Documentation

- **[DOCUMENTATION.md](DOCUMENTATION.md)** — setup, usage, how it works, full
  troubleshooting guide
- **[APPLE-PHOTOS-LIBRARY-INTERNALS.md](APPLE-PHOTOS-LIBRARY-INTERNALS.md)** — the
  measured technical reference the collectors are built from. Every rule in it was
  verified against real libraries; where something was observed but not explained, it
  says so

Warnings in the app point at the relevant section of both.

## A note on honesty

Apple documents almost none of this, and much of it can only be learned by measurement.
The project's rule, inherited from the internals document, is to **say what was measured
and say plainly when something is not established**.

So you will see, on the dashboard itself:

- job flags shown as decomposed bits rather than given invented names, because their
  meanings are not established
- the completion rate labelled *derived*, because Photos keeps no record of finished
  jobs — rows are simply deleted
- the search index shown as two counts rather than a percentage, because it can contain
  more rows than the library has assets and the reason is not establishable from outside

A dashboard that guessed would be more comfortable to read and worse to rely on.

## Findings

Things measured while building this, beyond what the reference document records:

- **The job queue *is* attributable to individual photos.**
  `ZBACKGROUNDJOBWORKITEM` has no foreign key to `ZASSET`, but 154,154 of 190,682 rows
  carried an asset UUID in `ZIDENTIFIER` that joins straight to `ZASSET.ZUUID`.
- **Job type 11 is cascade donation** — settled by direct evidence: all 681 rows of that
  type carry the literal identifier `PLCascadeDonationRequired`.
- **The NFC/NFD trap applies to filesystem paths, not just metadata.** For a library
  with an accented name, `lsof` and `Path.glob` return different Unicode normalisations
  of it. A raw substring test matched **0 of 56** real hits, and the dashboard
  confidently reported that nothing held a library four daemons had open.
- **The search index can hold more rows than the library has assets** — 40,589 against
  21,737. The split-UUID byte order is not established, so the rows cannot be joined
  back to find out why.

## Limitations

- macOS only, and it reads a private, undocumented schema — Apple can change it at any
  time. Entity numbers are resolved at runtime precisely because they *do* move.
- Verified against `LibrarySchemaVersion` 5001 (macOS Sequoia / Tahoe). Other versions
  will probably work, since nothing is hard-coded, but are untested.
- Reading a library in your home folder needs Full Disk Access.
- The unified-log panels need `log show`, which is slow and occasionally empty.

## Contributing

Issues and pull requests welcome. Two things to keep in mind:

1. **Never add a write path.** `test_readonly.py` must keep passing, and any new library
   access must go through the hardened connection helpers.
2. **Do not invent meanings.** If a number's significance is not established, say so —
   that is a feature of this project, not a gap in it. Evidence for a mapping is very
   welcome; a plausible guess presented as fact is not.

Run before submitting:

```bash
python3 test_readonly.py
python3 -m py_compile photo_dashboard.py photodash/*.py test_readonly.py
```

## Licence

Not yet chosen — add one before publishing if you intend others to reuse this.

## Acknowledgements

Built on `APPLE-PHOTOS-LIBRARY-INTERNALS.md`, a reference assembled by measuring real
Photos libraries directly rather than by reading documentation that does not exist.
