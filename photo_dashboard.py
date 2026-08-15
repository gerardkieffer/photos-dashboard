#!/usr/bin/env python3
"""Live web dashboard for an Apple Photos library.

Read-only and safe to run at any time, including while Photos is open.

    ./photo_dashboard.py                      # pick a readable library, serve on :8787
    ./photo_dashboard.py --library /Volumes/Disk/X.photoslibrary
    ./photo_dashboard.py --list               # what can this Mac actually read?
    ./photo_dashboard.py --once               # one collection pass to stdout, no server

It shows, live and historically:

  * the background job queue — depth, mix by type and flags, age profile, the
    assets with the most work outstanding, and a derived completion rate
  * whether anything can run at all — the Duet scheduler's MNP/CP verdicts,
    which daemons hold the library, whether Photos.app is open
  * how far analysis has got — per-worker completion from the media-analysis
    version columns, and the search index's own coverage
  * database statistics — table census, primary-key counter health, write
    activity by subsystem from persistent history
  * the library's content — assets, faces, people, keywords, albums, resources

History is sampled at a cadence matched to each kind of data (queue depth every
10 s, the asset census every 15 min) and compacted as it ages, so a month of
history stays small and a 30-day chart still shows the spikes.

Libraries under the home directory are unreadable without Full Disk Access —
that is TCC, not a bug here (internals doc §17). A library on an external volume
needs no permission grant.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phlib as ph
from photodash import collectors as C
from photodash.server import serve


def pick_library(explicit: Path | None) -> Path:
    if explicit:
        lib = Path(explicit)
        if not C.readable(lib):
            ph.eprint(f"ERROR: cannot read {lib}")
            ph.eprint("  Either there is no library there, or macOS is blocking it.")
            ph.eprint("  Libraries under ~ need Full Disk Access (internals doc §17);")
            ph.eprint("  one on an external volume needs no permission at all.")
            raise SystemExit(2)
        return lib
    readable = [e for e in C.discover_libraries() if e["readable"]]
    if not readable:
        ph.eprint("ERROR: no readable Photos library found. Try --list.")
        raise SystemExit(2)
    if len(readable) > 1:
        others = ", ".join(e["name"] for e in readable[1:])
        ph.eprint(f"note: {len(readable)} readable libraries found; watching the largest.")
        ph.eprint(f"      also available (use --library): {others}")
    return Path(readable[0]["path"])


def history_path_for(lib: Path, explicit: Path | None) -> Path:
    """One history file per library.

    A shared store would interleave two libraries' series under the same metric
    names, so switching library would draw a cliff from one library's asset
    count to the other's. §2.5 says to distinguish libraries by inode rather
    than path — two paths can look alike — so the inode goes in the filename and
    the name is there only to make the directory readable.
    """
    if explicit:
        return explicit
    try:
        inode = (lib / "database" / "Photos.sqlite").stat().st_ino
    except OSError:
        inode = 0
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", lib.stem)[:48]
    return ph.WORKDIR / "snapshots" / "dashboard" / f"{safe}-{inode}.db"


def cmd_list() -> int:
    libs = C.discover_libraries()
    if not libs:
        print("no *.photoslibrary bundles found")
        return 1
    width = max(len(l["name"]) for l in libs)
    for l in libs:
        mark = "readable" if l["readable"] else "BLOCKED (needs Full Disk Access, §17)"
        size = f'  {C.human(l["db_bytes"])}' if l.get("db_bytes") else ""
        print(f'  {l["name"]:<{width}}  {mark}{size}')
        print(f'  {"":<{width}}  {l["path"]}')
    return 0


def cmd_once(lib: Path) -> int:
    """One pass of every collector, as JSON. Useful for scripting and for
    checking what the dashboard would show without starting a server."""
    con = C.open_db(lib)
    try:
        out = {
            "identity": C.collect_identity(lib),
            "queue": C.collect_queue(con),
            "queue_mix": C.collect_queue_mix(con),
            "hot_assets": C.collect_queue_hot_assets(con),
            "writes": C.collect_write_activity(con),
            "authors": C.collect_authors(con),
            "changes": C.collect_recent_changes(con),
            "assets": C.collect_assets(con),
            "people": C.collect_people(con),
            "albums": C.collect_albums(con),
            "keywords": C.collect_keywords(con),
            "resources": C.collect_resources(con),
            "coverage": C.collect_analysis_coverage(con),
            "side_tables": C.collect_analysis_side_tables(con),
            "pk_health": C.collect_pk_health(con),
            "census": C.collect_table_census(con),
            "search": C.collect_search_index(lib, con),
            "processes": C.collect_processes(lib),
        }
    finally:
        con.close()
    json.dump(out, sys.stdout, indent=2, default=str)
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", type=Path,
                    help="path to the .photoslibrary bundle (default: first readable)")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default: localhost only)")
    ap.add_argument("--history", type=Path,
                    help="time-series store (default: snapshots/dashboard/<library>-<inode>.db, "
                         "one file per library so their series never interleave)")
    ap.add_argument("--log-minutes", type=int, default=5,
                    help="window for each `log show` query (default: 5)")
    ap.add_argument("--log-interval", type=int, default=180,
                    help="seconds between unified-log passes; `log show` is the "
                         "one expensive collector (default: 180)")
    ap.add_argument("--no-fs-walk", action="store_true",
                    help="skip the filesystem inventory (tens of thousands of stat calls)")
    ap.add_argument("--snapshot-mode", action="store_true",
                    help="never open the library at all: copy Photos.sqlite + -wal + -shm "
                         "aside and read the copy. Direct mode already never writes the "
                         "database or its WAL, but SQLite does update the -shm WAL-index; "
                         "this avoids even that, at the cost of freshness. On the same APFS "
                         "volume the copy is a clone and is effectively free.")
    ap.add_argument("--snapshot-interval", type=float, default=60.0,
                    help="seconds between snapshot refreshes (default: 60). Live metrics "
                         "are only as fresh as this.")
    ap.add_argument("--list", action="store_true",
                    help="list every library found, and whether it can be read")
    ap.add_argument("--once", action="store_true",
                    help="run every collector once, print JSON, exit")
    args = ap.parse_args()

    if args.list:
        return cmd_list()
    lib = pick_library(args.library)
    if args.once:
        return cmd_once(lib)

    print("Apple Photos library dashboard")
    # A factory, not a path: the library can be switched from the UI, and each
    # library must keep its own history file. An explicit --history applies only
    # to the library it was given for; switching away still derives a fresh one
    # rather than interleaving two libraries' series in one file.
    return serve(lib, lambda l: history_path_for(l, args.history if l == lib else None),
                 args.host, args.port, args.log_minutes, args.log_interval,
                 not args.no_fs_walk, args.snapshot_mode,
                 args.snapshot_interval) or 0


if __name__ == "__main__":
    raise SystemExit(main())
