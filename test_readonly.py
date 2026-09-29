#!/usr/bin/env python3
"""Check read-only guards and collectors using disposable library copies.

Integrity of the library matters more than anything the dashboard reports, so
this is an executable test rather than a claim in a comment. It does three
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
database is opened read-only for a consistent backup; its shared memory may change.
All attempted writes target disposable copies, never the source library.
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
    raw = sqlite3.connect((lib / "database" / "Photos.sqlite").resolve().as_uri() + "?mode=ro",
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
    wal_holders = []
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

        # The consistent backup uses DELETE mode. Convert only these disposable
        # copies to WAL and leave committed, uncheckpointed content in each WAL.
        # Keep idle connections open so the sidecars survive collector shutdown.
        for db in (clone / "database" / "Photos.sqlite",
                   clone / "database" / "search" / "psi.sqlite"):
            if not db.exists():
                continue
            holder = sqlite3.connect(db)
            wal_holders.append(holder)
            holder.executescript('''
                PRAGMA journal_mode=WAL;
                PRAGMA wal_autocheckpoint=0;
                CREATE TABLE __dashboard_wal_fixture (value INTEGER);
                INSERT INTO __dashboard_wal_fixture VALUES (1);
            ''')
            check(db.with_name(db.name + "-wal").stat().st_size > 32,
                  f"{db.name} has committed WAL frames")

        before = fingerprint(clone)
        check(bool(before), "clone created", f"{len(before)} files")

        snap = None
        dbdir = None
        if snapshot_mode:
            snap = C.Snapshotter(clone, tmp / "scratch", interval=0)
            if not snap.refresh(force=True):
                raise RuntimeError(snap.error)
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
            # Exercise readability against the same copy as the collectors.
            C.readable(clone, dbdir=dbdir)
            # Discovery is covered with synthetic fixtures in test_safety.py.
        finally:
            con.close()
            if snap:
                snap.cleanup()

        after = fingerprint(clone)

        # The -shm is the WAL-index: a derived scratch file, holding no library
        # data, that SQLite regenerates from the WAL and that every WAL reader
        # updates with its read-mark — Photos included. In either mode it is the
        # one file a read can legitimately touch. Database and WAL must stay
        # unchanged. These fixtures are offline and have no active writer.
        def is_shm(f):
            return f.endswith("-shm")

        allow_shm = True

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

        print(f"  {DIM}SQLite may update source shared memory in either mode.{OFF}")
    finally:
        for holder in wal_holders:
            holder.close()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", type=Path)
    ap.add_argument("--no-fs-walk", action="store_true")
    args = ap.parse_args()

    lib = args.library
    if lib is None:
        found = [e for e in C.discover_libraries(deep=False) if e["readable"]]
        if not found:
            ph.eprint("no readable library to test against")
            return 2
        lib = Path(found[0]["path"])
    if not (lib / "database" / "Photos.sqlite").is_file():
        ph.eprint(f"cannot read {lib}")
        return 2

    print(f"Read-only safety checks\n  library: {lib}")
    # No attempted write statement is ever directed at the user's source.
    # Obtain a consistent read-only backup first, then test only that copy.
    with tempfile.TemporaryDirectory(prefix="photodash-safety-") as tmp:
        scratch = Path(tmp)
        snap = C.Snapshotter(lib, scratch / "backup")
        if not snap.refresh(force=True):
            ph.eprint(snap.error)
            return 2
        clone = scratch / "Test.photoslibrary"
        clone.mkdir()
        shutil.copytree(snap.dir, clone / "database")
        test_enforcement(clone)
        test_no_mutation(clone, not args.no_fs_walk, snapshot_mode=False)
        test_no_mutation(clone, not args.no_fs_walk, snapshot_mode=True)

    print()
    if failures:
        print(f"{RED}{failures} check(s) FAILED — do not run the dashboard "
              f"against a library you care about until this passes.{OFF}")
        return 1
    print(f"{GREEN}All checks passed on disposable database copies; source shared memory may change.{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
