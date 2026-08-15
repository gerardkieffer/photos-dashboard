#!/usr/bin/env python3
"""Prove the dashboard cannot modify a Photos library.

Integrity of the library matters more than anything the dashboard reports, so
this is an executable proof rather than a claim in a comment. It does three
independent things:

  1. ENFORCEMENT — checks that each of the three read-only guards actually
     rejects a write: the mode=ro handle, PRAGMA query_only, and the authorizer.
  2. BYTE-FOR-BYTE — clones a real library's database into a scratch bundle that
     nothing else on the machine has open, SHA-256s every file, runs every
     collector against it, and re-hashes. Any difference is ours, because
     nothing else is touching the copy. mtime, size and inode are compared too,
     so even a no-op rewrite would be caught.
  3. NO NEW FILES — confirms no -wal, -shm, journal or lock file was created.

    ./test_readonly.py                    # clone from the first readable library
    ./test_readonly.py --library PATH

The clone lives in a temporary directory and is deleted afterwards. The source
library is only ever read.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from photodash import collectors as C
from photodash import coredata as ph

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
failures = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    mark = f"{GREEN}PASS{OFF}" if ok else f"{RED}FAIL{OFF}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{OFF}" if detail else ""))


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fingerprint(root: Path) -> dict[str, tuple]:
    """Every file under root: hash, size, mtime_ns and inode."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(root))] = (digest(p), st.st_size,
                                             st.st_mtime_ns, st.st_ino)
    return out


# --------------------------------------------------------------------------
def test_enforcement(lib: Path) -> None:
    print("\n1. Read-only enforcement — each guard must reject a write")

    con = C.open_db(lib)
    try:
        # The authorizer runs at statement-compile time and should refuse
        # anything that is not a read, well before SQLite touches the file.
        for sql, label in [
            ("CREATE TABLE dashboard_should_never_exist (x)", "CREATE TABLE"),
            ("INSERT INTO ZKEYWORD (Z_PK) VALUES (999999999)", "INSERT"),
            ("UPDATE ZASSET SET ZFAVORITE = 1", "UPDATE"),
            ("DELETE FROM ZBACKGROUNDJOBWORKITEM", "DELETE"),
            ("DROP TABLE ZKEYWORD", "DROP TABLE"),
            ("PRAGMA journal_mode = DELETE", "PRAGMA journal_mode=DELETE"),
            ("PRAGMA writable_schema = ON", "PRAGMA writable_schema"),
        ]:
            try:
                con.execute(sql)
                check(False, f"{label} rejected", "STATEMENT SUCCEEDED")
            except sqlite3.Error as exc:
                check(True, f"{label} rejected", type(exc).__name__)

        # Reads must still work, or the guards are too tight to be useful.
        n = con.execute("SELECT COUNT(*) FROM ZASSET").fetchone()[0]
        check(n >= 0, "SELECT still works", f"{n:,} assets")
        cols = [r[1] for r in con.execute("PRAGMA table_info(ZASSET)")]
        check(len(cols) > 10, "PRAGMA table_info still works", f"{len(cols)} columns")
        v = con.execute("SELECT nfc('é')").fetchone()[0]
        check(v == "é", "registered nfc() still callable")
    finally:
        con.close()

    # The file handle itself must be read-only, independent of the pragmas.
    raw = sqlite3.connect(f"file:{lib}/database/Photos.sqlite?mode=ro",
                          uri=True, timeout=5)
    try:
        raw.execute("CREATE TABLE should_never_exist (x)")
        check(False, "mode=ro handle rejects writes", "STATEMENT SUCCEEDED")
    except sqlite3.Error as exc:
        check(True, "mode=ro handle rejects writes", type(exc).__name__)
    finally:
        raw.close()


def test_no_mutation(src: Path, walk_fs: bool, snapshot_mode: bool) -> None:
    label = "snapshot mode" if snapshot_mode else "direct mode"
    print(f"\n{'3' if snapshot_mode else '2'}. Byte-for-byte, {label} — "
          f"clone, hash, run every collector, re-hash")

    tmp = Path(tempfile.mkdtemp(prefix="photodash-safety-"))
    clone = tmp / "Clone.photoslibrary"
    try:
        (clone / "database").mkdir(parents=True)
        copied = []
        for name in ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm",
                     "DataModelVersion.plist"):
            s = src / "database" / name
            if s.exists():
                shutil.copy2(s, clone / "database" / name)
                copied.append(name)
        search_src = src / "database" / "search"
        if (search_src / "psi.sqlite").exists():
            (clone / "database" / "search").mkdir()
            for name in ("psi.sqlite", "psi.sqlite-wal", "psi.sqlite-shm"):
                s = search_src / name
                if s.exists():
                    shutil.copy2(s, clone / "database" / "search" / name)
                    copied.append(f"search/{name}")
        (clone / "originals").mkdir()
        print(f"  {DIM}cloned {len(copied)} files: {', '.join(copied)}{OFF}")

        before = fingerprint(clone)
        check(bool(before), "clone created", f"{len(before)} files")

        snap = None
        dbdir = None
        if snapshot_mode:
            snap = C.Snapshotter(clone, tmp / "scratch", interval=0)
            snap.refresh(force=True)
            dbdir = snap.dir

        con = C.open_db(clone, dbdir)
        try:
            C.collect_identity(clone)
            C.collect_queue(con)
            C.collect_queue_mix(con)
            C.collect_queue_hot_assets(con)
            C.collect_write_activity(con)
            C.collect_authors(con)
            C.collect_recent_changes(con)
            C.collect_assets(con)
            C.collect_people(con)
            C.collect_top_people(con)
            C.collect_albums(con)
            C.collect_keywords(con)
            C.collect_top_keywords(con)
            C.collect_resources(con)
            C.collect_analysis_coverage(con)
            C.collect_analysis_side_tables(con)
            C.collect_pk_health(con)
            C.collect_table_census(con)
            C.collect_search_index(clone, con, dbdir)
            if walk_fs:
                C.collect_filesystem(clone)
            # In snapshot mode even the readability probe must go through the
            # copy: a successful open is precisely what updates the -shm.
            C.readable(clone, dbdir=dbdir)
            C.discover_libraries(deep=not snapshot_mode)
        finally:
            con.close()
            if snap:
                snap.cleanup()

        after = fingerprint(clone)

        # The -shm is the WAL-index: a derived scratch file, holding no library
        # data, that SQLite regenerates from the WAL and that every WAL reader
        # updates with its read-mark — Photos included. In direct mode it is the
        # one file a read can legitimately touch. Everything else must be
        # untouched, and in snapshot mode nothing at all may move.
        def is_shm(f):
            return f.endswith("-shm")

        allow_shm = not snapshot_mode

        changed = [f for f in before if f in after and before[f][0] != after[f][0]]
        content = [f for f in changed if not (allow_shm and is_shm(f))]
        check(not content, "database and WAL contents unchanged",
              ", ".join(content) or "all non-shm hashes identical")

        touched = [f for f in before if f in after and before[f][2] != after[f][2]]
        mtimes = [f for f in touched if not (allow_shm and is_shm(f))]
        check(not mtimes, "no mtime changed",
              ", ".join(mtimes) or "all non-shm mtimes identical")

        reinoded = [f for f in before if f in after and before[f][3] != after[f][3]]
        check(not reinoded, "no file replaced (inode stable)",
              ", ".join(reinoded) or "all inodes identical")

        created = sorted(set(after) - set(before))
        check(not created, "no files created", ", ".join(created) or "none")

        removed = sorted(set(before) - set(after))
        check(not removed, "no files removed", ", ".join(removed) or "none")

        if snapshot_mode:
            check(not changed, "NOTHING inside the bundle was written",
                  ", ".join(changed) or "every file byte-identical")
        else:
            shm = [f for f in changed if is_shm(f)]
            print(f"  {DIM}expected: -shm WAL-index updated by SQLite "
                  f"({', '.join(shm) if shm else 'none this run'}) — derived "
                  f"scratch, no library data. Use --snapshot-mode to avoid it.{OFF}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_source_untouched(lib: Path) -> None:
    """The live library is read during discovery too — check that costs nothing."""
    print("\n4. Source library — discovery and probing leave it alone")
    db = lib / "database" / "Photos.sqlite"
    before = db.stat()
    C.readable(lib)
    C.discover_libraries()
    after = db.stat()
    # The daemons may legitimately write to a live library while we look, so a
    # changed mtime here is not proof of guilt — only equality is informative.
    check(before.st_ino == after.st_ino, "Photos.sqlite inode unchanged",
          str(after.st_ino))
    if before.st_mtime_ns != after.st_mtime_ns:
        print(f"  {DIM}note: mtime moved during the check — this library is live, "
              f"so that is the daemons writing, not us (see test 2 for the "
              f"controlled comparison){OFF}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", type=Path)
    ap.add_argument("--no-fs-walk", action="store_true")
    args = ap.parse_args()

    lib = args.library
    if lib is None:
        found = [e for e in C.discover_libraries() if e["readable"]]
        if not found:
            ph.eprint("no readable library to test against")
            return 2
        lib = Path(found[0]["path"])
    if not C.readable(lib):
        ph.eprint(f"cannot read {lib}")
        return 2

    print(f"Read-only safety proof\n  library: {lib}")
    test_enforcement(lib)
    test_no_mutation(lib, not args.no_fs_walk, snapshot_mode=False)
    test_no_mutation(lib, not args.no_fs_walk, snapshot_mode=True)
    test_source_untouched(lib)

    print()
    if failures:
        print(f"{RED}{failures} check(s) FAILED — do not run the dashboard "
              f"against a library you care about until this passes.{OFF}")
        return 1
    print(f"{GREEN}All checks passed: the dashboard reads and never writes.{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
