"""Read-only collectors for the Photos library dashboard.

Every function here is safe to run against a live library, including one that
Photos.app and the analysis daemons currently hold open. Read-only is enforced
three ways (see the `connections` section below). test_readonly.py checks
collector behavior byte-for-byte on disposable library copies.

One honest caveat, measured rather than assumed: opening a WAL database — even
read-only — lets SQLite update the `-shm` WAL-index, the derived scratch file
that carries reader marks and no library data. Every WAL reader does this,
Photos included, and the file is regenerated from the WAL whenever it is
missing. `Snapshotter` (snapshot mode) provides the
option of running expensive queries on a consistent local backup. Backup
coordination can also update the source shared-memory file.

See APPLE-PHOTOS-LIBRARY-INTERNALS.md for why each query is shaped the way it
is — the section references in the comments below are to that document.

The rules that shape this file:

  * §2.2  Z_ENT numbers are not stable. Everything is resolved at runtime, in
          the same process that uses it. Nothing is cached across restarts.
  * §2.3  Join-table names embed entity numbers; match on the word, not the
          number (coredata.resolve_*_join does this).
  * §5    Core Data timestamps are seconds since 2001-01-01 UTC.
  * §16   Compare text NFC-normalised; psi.sqlite strings are NUL-terminated
          (§19) so they need rstrip('\\x00') too.
  * §3.2  MAX(Z_PK) > Z_PRIMARYKEY.Z_MAX is a live fault, not cosmetic: it
          breaks every subsequent save in the library.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
import threading
import time
from collections import Counter
from pathlib import Path

from . import coredata as ph
from .safety import outside_library

APPLE_EPOCH = ph.APPLE_EPOCH
LOG = "/usr/bin/log"          # §20: bare `log` hits a zsh builtin

# Daemons that hold a library. §2.5 measured which ones actually do the work —
# checking only photolibraryd misses the bulk of it.
DAEMONS = ["photolibraryd", "photoanalysisd", "mediaanalysisd", "cloudphotod",
           "managedcomputekitservice", "photoanalysisd-cache", "Photos"]

# ZBACKGROUNDJOBWORKITEM.ZJOBFLAGS is a bitmask: the observed values decompose
# cleanly into powers of two (3=1|2, 14=2|4|8, 1026=2|1024, 8194=2|8192). The
# bit *meanings* are not established, so we decompose and show the bits rather
# than inventing names for them.
def decompose_flags(flags: int) -> list[int]:
    return [1 << i for i in range(32) if flags & (1 << i)]


def sh(cmd: list[str], timeout: int = 120, stop_event=None) -> str:
    try:
        if stop_event is not None:
            if stop_event.is_set():
                return ""
            with subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as proc:
                deadline = time.monotonic() + timeout
                while not stop_event.is_set() and time.monotonic() < deadline:
                    try:
                        return proc.communicate(timeout=0.25)[0]
                    except subprocess.TimeoutExpired:
                        continue
                proc.kill()
                proc.communicate()
                return ""
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:                                        # noqa: BLE001
        return ""


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def apple_to_unix(v):
    return None if v is None else float(v) + APPLE_EPOCH


def unix_to_apple(v: float) -> float:
    return v - APPLE_EPOCH


# --------------------------------------------------------------------------
# library discovery
# --------------------------------------------------------------------------
def _probe(db: Path) -> bool:
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        _harden(con)
        con.execute("SELECT Z_ENT FROM Z_PRIMARYKEY LIMIT 1").fetchone()
    finally:
        con.close()
    return True


def readable(lib: Path, timeout: float = 4.0, dbdir: Path | None = None) -> bool:
    """§17: a genuine bundle under the home directory is TCC-blocked without
    Full Disk Access. Probing beats guessing — and the probe has to be a real
    open, because TCC lets stat() through: Path.exists() returns True for a
    blocked Photos.sqlite that sqlite3 then refuses to open.

    The probe runs on a throwaway thread with a deadline. Depending on how the
    process was launched, a TCC-protected open does not always fail fast: from a
    terminal it returns 'unable to open database file' immediately, but under a
    GUI-launched parent it can block indefinitely inside open(2) waiting on a
    consent prompt that nobody is looking at. A spun-down or disconnected
    external volume behaves the same way. Neither may be allowed to wedge the
    server on startup, so an unanswered probe counts as not readable.

    `dbdir` points the probe at a snapshot copy instead of the library, when checking
    a completed backup. Backup creation itself opens the source read-only and
    can update its shared-memory file.
    """
    try:
        if not (lib / "database" / "Photos.sqlite").exists():
            return False
    except (PermissionError, OSError):
        return False
    db = (dbdir or (lib / "database")) / "Photos.sqlite"

    result: list[bool] = []

    def run():
        try:
            result.append(_probe(db))
        except (sqlite3.Error, PermissionError, OSError):
            result.append(False)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return bool(result and result[0])


def discover_libraries(deep: bool = True) -> list[dict]:
    """Every library we can actually open, most obvious candidates first.

    `deep=False` decides readability from file permissions instead of opening
    the store. It is less certain — TCC lets stat() through, so a blocked
    library can look readable — but opening a WAL database is precisely what
    updates its -shm, so snapshot mode uses a shallow probe and defers actual
    readability to backup creation.
    """
    seen: dict[str, dict] = {}
    roots = [ph.PROJECT_DIR, ph.PROJECT_DIR.parent, Path.home() / "Pictures"]
    roots += [p for p in Path("/Volumes").glob("*") if p.is_dir()]
    for root in roots:
        try:
            candidates = list(root.glob("*.photoslibrary"))
        except (PermissionError, OSError):
            continue
        for lib in candidates:
            key = str(lib)
            if key in seen:
                continue
            if deep:
                ok = readable(lib)
            else:
                db = lib / "database" / "Photos.sqlite"
                try:
                    ok = db.exists() and os.access(db, os.R_OK)
                except OSError:
                    ok = False
            entry = {"path": key, "name": lib.name, "readable": ok,
                     "probe": "open" if deep else "stat-only",
                     "volume": str(root)}
            if ok:
                try:
                    st = (lib / "database" / "Photos.sqlite").stat()
                    entry["inode"] = st.st_ino     # §2.5: identity is the inode
                    entry["db_bytes"] = st.st_size
                except OSError:
                    pass
            seen[key] = entry
    # Readable first, then largest store first: when several libraries are
    # visible the substantial one is almost always the intended target, and a
    # deterministic order beats "whichever sorted first alphabetically".
    return sorted(seen.values(),
                  key=lambda e: (not e["readable"], -e.get("db_bytes", 0), e["name"]))


# --------------------------------------------------------------------------
# connections
#
# The integrity of the library matters more than anything this tool reports, so
# read-only is enforced three independent ways rather than trusted once:
#
#   1. the URI opens the file with mode=ro, so the OS file handle is read-only
#      the database and WAL remain read-only (the -shm may be updated);
#   2. PRAGMA query_only makes the *connection* refuse any statement that would
#      write, even one issued by mistake in code added later;
#   3. an authorizer whitelists the operations a reader needs and denies every
#      other action inside SQLite, before a statement is even compiled.
#
# Any one of these would stop a write. All three have to be removed on purpose.
#
# Deliberately NOT used: `immutable=1`, which promises SQLite the file never
# changes. It is faster, but on a live library held by photolibraryd that
# promise is false, and it would produce silently stale or torn reads.
# --------------------------------------------------------------------------

# The pragmas a reader legitimately needs. PRAGMA is the one authorizer action
# that can both read and write, so it is whitelisted by name, not wholesale.
_READ_PRAGMAS = {
    "table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
    "database_list", "collation_list", "foreign_key_list", "function_list",
    "page_count", "page_size", "freelist_count", "encoding", "user_version",
    "application_id", "journal_mode", "compile_options",
}

# Actions a read-only consumer needs: reading tables and columns, running
# SELECTs, calling functions (our nfc()), and opening a read transaction.
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_RECURSIVE,
}


def _readonly_authorizer(action, arg1, arg2, db_name, trigger):
    """Deny anything that is not a read. Runs inside SQLite at compile time."""
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        arguments = {"table_info", "table_xinfo", "index_list", "index_info",
                     "index_xinfo", "foreign_key_list"}
        return sqlite3.SQLITE_OK if name in _READ_PRAGMAS and (
            arg2 is None or name in arguments) else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _harden(con: sqlite3.Connection) -> sqlite3.Connection:
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    con.set_authorizer(_readonly_authorizer)
    return con


class ReadConnection(sqlite3.Connection):
    """Bound each SELECT's execution time, including cursor iteration."""

    query_timeout = 15.0
    stop_event = None

    def execute(self, sql, parameters=()):
        deadline = time.monotonic() + self.query_timeout
        self.set_progress_handler(lambda: int(
            time.monotonic() >= deadline or
            (self.stop_event is not None and self.stop_event.is_set())), 1000)
        return super().execute(sql, parameters)


def open_readonly(db: Path, query_timeout: float = 15.0,
                  stop_event=None) -> sqlite3.Connection:
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True,
                          timeout=2, factory=ReadConnection)
    con.query_timeout = query_timeout
    con.stop_event = stop_event
    _harden(con)
    ph.register_nfc(con)
    return con


def open_db(lib: Path, dbdir: Path | None = None, **kwargs) -> sqlite3.Connection:
    return open_readonly((dbdir or (lib / "database")) / "Photos.sqlite", **kwargs)


def open_psi(lib: Path, dbdir: Path | None = None) -> sqlite3.Connection | None:
    psi = (dbdir or (lib / "database")) / "search" / "psi.sqlite"
    if not psi.exists():
        return None
    try:
        return open_readonly(psi)
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        return None


@contextmanager
def read_transaction(con):
    """Use one SQLite view for a multi-query integrity check."""
    owned = not con.in_transaction
    if owned:
        con.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            con.rollback()


# --------------------------------------------------------------------------
# snapshot mode — reading a copy instead of the library
# --------------------------------------------------------------------------
DB_FILES = ["Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm"]
PSI_FILES = ["psi.sqlite", "psi.sqlite-wal", "psi.sqlite-shm"]


class Snapshotter:
    """Publish complete SQLite backups; failed refreshes retain the last copy.

    The source is opened mode=ro, so its database and WAL are not written.
    SQLite may update the source -shm while coordinating with live writers.
    The two databases are individually consistent, not a cross-database
    transaction. For zero access to the original, use an offline library copy.
    """

    def __init__(self, library: Path, scratch: Path, interval: float = 300.0,
                 timeout: float = 120.0):
        self.library = Path(library)
        self.root = outside_library(scratch, library)
        self.root.mkdir(parents=True, exist_ok=True)
        if any(self.root.iterdir()):
            raise ValueError("Snapshot scratch directory must be empty")
        self.dir = self.root / "unavailable"
        self.interval = interval
        self.timeout = timeout
        self.stop_event = threading.Event()
        self.last_copy = 0.0
        self.last_attempt = 0.0
        self.last_duration = 0.0
        self.error: str | None = None

    def _backup(self, src: Path, dst: Path, deadline: float) -> None:
        def progress(status, remaining, total):
            if self.stop_event.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("Snapshot backup cancelled or exceeded its time budget")
        source = open_readonly(src)
        try:
            target = sqlite3.connect(dst)
            try:
                # Pin one read view across steps. Without this, commits on
                # another connection restart a chunked backup indefinitely.
                # WAL writers can continue; checkpoints must retain this view.
                with read_transaction(source):
                    source.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                    progress(sqlite3.SQLITE_OK, 0, 0)
                    source.backup(target, pages=256, progress=progress, sleep=0.05)
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()
        finally:
            source.close()

    def refresh(self, force: bool = False) -> bool:
        if not force and time.monotonic() - self.last_attempt < self.interval:
            return False
        self.last_attempt = time.monotonic()
        t0 = time.monotonic()
        stage = None
        published = False
        try:
            stage = Path(tempfile.mkdtemp(prefix="generation-", dir=self.root))
            deadline = t0 + self.timeout
            self._backup(self.library / "database" / "Photos.sqlite",
                         stage / "Photos.sqlite", deadline)
            psi = self.library / "database" / "search" / "psi.sqlite"
            if psi.exists():
                (stage / "search").mkdir()
                self._backup(psi, stage / "search" / "psi.sqlite", deadline)
            previous = self.dir
            self.dir = stage
            published = True
            self.last_copy = time.time()
            self.last_duration = time.monotonic() - t0
            self.error = None
            if previous.exists():
                shutil.rmtree(previous, ignore_errors=True)
            return True
        except (OSError, sqlite3.Error) as exc:
            self.error = str(exc)
            return False
        finally:
            # Covers KeyboardInterrupt and unexpected failures as well as
            # ordinary I/O errors. Never remove the published generation.
            if stage is not None and not published:
                shutil.rmtree(stage, ignore_errors=True)
            # Wait a full interval after completion/failure, even when the
            # attempt itself took longer than the configured interval.
            self.last_attempt = time.monotonic()

    def status(self) -> dict:
        return {
            "mode": "snapshot", "dir": str(self.dir),
            "interval_s": self.interval, "last_copy": self.last_copy or None,
            "last_duration_s": round(self.last_duration, 3),
            "apfs_clone": False, "error": self.error,
            "note": "Consistent SQLite backups; source shared-memory files may be updated.",
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def raise_if_interrupted(exc: sqlite3.Error) -> None:
    """Cancellation is missing data, never an empty successful result.

    Python 3.10 does not expose sqlite_errorcode on exceptions.
    """
    if (getattr(exc, "sqlite_errorcode", None) == 9 or
            (isinstance(exc, sqlite3.OperationalError) and str(exc) == "interrupted")):
        raise exc


def scalar(con, sql: str, *args, default=None):
    try:
        row = con.execute(sql, args).fetchone()
        return row[0] if row and row[0] is not None else default
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        return default


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------
def collect_identity(lib: Path) -> dict:
    db = lib / "database" / "Photos.sqlite"
    out = {"path": str(lib), "name": lib.name}
    try:
        st = db.stat()
        out["db_bytes"] = st.st_size
        out["inode"] = st.st_ino
        out["db_mtime"] = st.st_mtime
    except OSError:
        return out
    for suffix in ("-wal", "-shm"):
        p = db.with_name(db.name + suffix)
        out[f"{suffix.strip('-')}_bytes"] = p.stat().st_size if p.exists() else 0

    plist = lib / "database" / "DataModelVersion.plist"
    if plist.exists():
        try:
            import plistlib
            data = plistlib.loads(plist.read_bytes())
            out["schema_version"] = data.get("LibrarySchemaVersion")
            out["meta_schema_version"] = data.get("MetaSchemaVersion")
        except Exception:                                    # noqa: BLE001
            pass

    # §2.5: the promoted-library check that actually works is lsof, but the
    # preference dump is a cheap corroborating signal.
    prefs = sh(["defaults", "read", "com.apple.photolibraryd"], timeout=10)
    out["is_system_library_hint"] = str(lib) in prefs
    return out


# --------------------------------------------------------------------------
# tier: live — the background job queue
# --------------------------------------------------------------------------
def collect_queue(con) -> dict:
    """Queue depth, arrival rate and the age profile of what is waiting."""
    now_a = unix_to_apple(time.time())
    out = {
        "pending": scalar(con, "SELECT COUNT(*) FROM ZBACKGROUNDJOBWORKITEM", default=0),
        "delayed": scalar(con,
            "SELECT COUNT(*) FROM ZBACKGROUNDJOBWORKITEM "
            "WHERE ZDELAYUNTILDATE IS NOT NULL AND ZDELAYUNTILDATE > ?",
            now_a, default=0),
        "distinct_identifiers": scalar(con,
            "SELECT COUNT(DISTINCT ZIDENTIFIER) FROM ZBACKGROUNDJOBWORKITEM", default=0),
    }
    for label, secs in (("60s", 60), ("5m", 300), ("1h", 3600), ("24h", 86400)):
        out[f"arrivals_{label}"] = scalar(con,
            "SELECT COUNT(*) FROM ZBACKGROUNDJOBWORKITEM WHERE ZTIMESTAMP > ?",
            now_a - secs, default=0)
    oldest = scalar(con, "SELECT MIN(ZTIMESTAMP) FROM ZBACKGROUNDJOBWORKITEM")
    newest = scalar(con, "SELECT MAX(ZTIMESTAMP) FROM ZBACKGROUNDJOBWORKITEM")
    out["oldest_ts"] = apple_to_unix(oldest)
    out["newest_ts"] = apple_to_unix(newest)
    out["oldest_age_s"] = (time.time() - out["oldest_ts"]) if oldest else 0
    return out


def collect_queue_mix(con) -> dict:
    """The job mix, by (ZJOBTYPE, ZJOBFLAGS), with an age histogram.

    §19 records that ZBACKGROUNDJOBWORKITEM has "no asset reference". That is
    true of its columns, but measured on this library 154,154 of 190,682 rows
    carry an asset UUID in ZIDENTIFIER, so the queue *is* mostly attributable —
    we report the attributable fraction per bucket rather than assuming either
    way. The remaining identifiers are literals such as
    'PLCascadeDonationRequired' or JSON blobs like {"type":1000,"label":...}.
    """
    now_a = unix_to_apple(time.time())
    bands = [("lt_1h", 3600), ("lt_6h", 6 * 3600), ("lt_24h", 86400),
             ("lt_7d", 7 * 86400)]
    rows = []
    try:
        cur = con.execute("""
            SELECT ZJOBTYPE jt, ZJOBFLAGS jf, COUNT(*) n,
                   MIN(ZTIMESTAMP) oldest, MAX(ZTIMESTAMP) newest,
                   SUM(CASE WHEN ZIDENTIFIER GLOB
                       '[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]-*-*-*-*'
                       THEN 1 ELSE 0 END) uuidish,
                   SUM(CASE WHEN ZDELAYUNTILDATE > ? THEN 1 ELSE 0 END) delayed,
                   SUM(CASE WHEN ZTIMESTAMP > ? THEN 1 ELSE 0 END) lt_1h,
                   SUM(CASE WHEN ZTIMESTAMP > ? THEN 1 ELSE 0 END) lt_6h,
                   SUM(CASE WHEN ZTIMESTAMP > ? THEN 1 ELSE 0 END) lt_24h,
                   SUM(CASE WHEN ZTIMESTAMP > ? THEN 1 ELSE 0 END) lt_7d
              FROM ZBACKGROUNDJOBWORKITEM
             GROUP BY 1, 2 ORDER BY n DESC
        """, (now_a, *[now_a - s for _, s in bands]))
        for r in cur:
            rows.append({
                "jobtype": r["jt"], "flags": r["jf"],
                "flag_bits": decompose_flags(r["jf"] or 0),
                "count": r["n"], "delayed": r["delayed"] or 0,
                "attributable": r["uuidish"] or 0,
                "oldest_ts": apple_to_unix(r["oldest"]),
                "newest_ts": apple_to_unix(r["newest"]),
                "age": {
                    "lt_1h": r["lt_1h"] or 0,
                    "lt_6h": (r["lt_6h"] or 0) - (r["lt_1h"] or 0),
                    "lt_24h": (r["lt_24h"] or 0) - (r["lt_6h"] or 0),
                    "lt_7d": (r["lt_7d"] or 0) - (r["lt_24h"] or 0),
                    "older": r["n"] - (r["lt_7d"] or 0),
                },
            })
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        pass

    # Non-UUID identifiers are the named, enumerable jobs — worth listing.
    named = []
    try:
        for r in con.execute("""
            SELECT ZIDENTIFIER id, COUNT(*) n FROM ZBACKGROUNDJOBWORKITEM
             WHERE ZIDENTIFIER NOT GLOB
               '[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]-*-*-*-*'
             GROUP BY 1 ORDER BY n DESC LIMIT 25"""):
            named.append({"identifier": r["id"], "count": r["n"]})
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        pass
    return {"buckets": rows, "named_identifiers": named}


def collect_queue_hot_assets(con, limit: int = 20) -> list[dict]:
    """Assets with the most queued work, resolved to real filenames.

    Only possible because ZIDENTIFIER holds asset UUIDs (see collect_queue_mix).
    """
    out = []
    try:
        for r in con.execute("""
            SELECT w.ZIDENTIFIER uuid, COUNT(*) n,
                   MIN(w.ZTIMESTAMP) oldest,
                   GROUP_CONCAT(DISTINCT w.ZJOBTYPE) types
              FROM ZBACKGROUNDJOBWORKITEM w
             GROUP BY 1 HAVING n > 1 ORDER BY n DESC LIMIT ?""", (limit,)):
            out.append({"uuid": r["uuid"], "count": r["n"],
                        "oldest_ts": apple_to_unix(r["oldest"]),
                        "jobtypes": r["types"]})
        uuids = [o["uuid"] for o in out]
        if uuids:
            marks = ",".join("?" * len(uuids))
            names = {r["ZUUID"]: r for r in con.execute(
                f"""SELECT a.ZUUID, a.ZKIND, a.ZWIDTH, a.ZHEIGHT, a.ZDATECREATED,
                           aa.ZORIGINALFILENAME fn
                      FROM ZASSET a
                      LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
                     WHERE a.ZUUID IN ({marks})""", uuids)}
            for o in out:
                m = names.get(o["uuid"])
                if m:
                    o["filename"] = m["fn"]
                    o["kind"] = "video" if m["ZKIND"] == 1 else "photo"
                    o["dimensions"] = f'{m["ZWIDTH"]}x{m["ZHEIGHT"]}'
                    o["created"] = apple_to_unix(m["ZDATECREATED"])
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        pass
    return out


# --------------------------------------------------------------------------
# tier: live — write activity via persistent history (§3.4)
# --------------------------------------------------------------------------
def collect_write_activity(con) -> dict:
    """Persistent-history transactions are the record of every save.

    MAX(Z_PK) rather than COUNT(*): ACHANGE has millions of rows here and the
    primary key is monotonic, so the max is index-backed and instant while
    still giving a usable rate.
    """
    now_a = unix_to_apple(time.time())
    out = {
        "transaction_max_pk": scalar(con, "SELECT MAX(Z_PK) FROM ATRANSACTION", default=0),
        "change_max_pk": scalar(con, "SELECT MAX(Z_PK) FROM ACHANGE", default=0),
        "string_pool": scalar(con, "SELECT COUNT(*) FROM ATRANSACTIONSTRING", default=0),
    }
    for label, secs in (("60s", 60), ("5m", 300), ("1h", 3600), ("24h", 86400)):
        out[f"transactions_{label}"] = scalar(con,
            "SELECT COUNT(*) FROM ATRANSACTION WHERE ZTIMESTAMP > ?",
            now_a - secs, default=0)
    last = scalar(con, "SELECT MAX(ZTIMESTAMP) FROM ATRANSACTION")
    out["last_transaction_ts"] = apple_to_unix(last)
    out["seconds_since_last_write"] = (time.time() - out["last_transaction_ts"]) \
        if last else None
    return out


def collect_authors(con, window_s: int = 3600, limit: int = 15) -> list[dict]:
    """Which subsystem has been writing, from the ATRANSACTIONSTRING pool (§3.4).

    ZCONTEXTNAMETS indexes the string pool; the names are things like
    'CPL library: -[PLCloudBatchDownloader handleIncomingBatch:] … .CPL BG',
    which says far more about what Photos is doing than any counter does.
    """
    since = unix_to_apple(time.time() - window_s)
    out = []
    try:
        for r in con.execute("""
            SELECT s.ZNAME name, COUNT(*) n, MAX(t.ZTIMESTAMP) last
              FROM ATRANSACTION t
              JOIN ATRANSACTIONSTRING s ON s.Z_PK = t.ZCONTEXTNAMETS
             WHERE t.ZTIMESTAMP > ?
             GROUP BY 1 ORDER BY n DESC LIMIT ?""", (since, limit)):
            out.append({"name": r["name"], "count": r["n"],
                        "last_ts": apple_to_unix(r["last"])})
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        pass
    return out


def collect_recent_changes(con, window_s: int = 3600) -> list[dict]:
    """What kind of object changed recently, resolved through Z_PRIMARYKEY.

    ACHANGE.ZENTITY holds a Z_ENT, which §2.2 says is not stable — so it is
    mapped back to a name at read time, in this same connection.
    """
    ents = {v: k for k, v in ph.entity_numbers(con).items()}
    since = unix_to_apple(time.time() - window_s)
    out = []
    try:
        for r in con.execute("""
            SELECT c.ZENTITY ent, c.ZCHANGETYPE ct, COUNT(*) n
              FROM ACHANGE c
              JOIN ATRANSACTION t ON t.Z_PK = c.ZTRANSACTIONID
             WHERE t.ZTIMESTAMP > ?
             GROUP BY 1,2 ORDER BY n DESC LIMIT 25""", (since,)):
            out.append({
                "entity": ents.get(r["ent"], f"Z_ENT {r['ent']}"),
                "z_ent": r["ent"],
                # §3.4: ZCHANGETYPE 0 = insert, 1 = update
                "change": {0: "insert", 1: "update"}.get(r["ct"], f"type {r['ct']}"),
                "count": r["n"],
            })
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        pass
    return out


# --------------------------------------------------------------------------
# tier: slow — library content census
# --------------------------------------------------------------------------
def collect_assets(con) -> dict:
    """§4.2: the near-universal correct filter is ZBUNDLESCOPE=0 AND ZTRASHEDSTATE=0."""
    q = lambda w, *a: scalar(con, f"SELECT COUNT(*) FROM ZASSET WHERE {w}", *a, default=0)
    out = {
        "total": scalar(con, "SELECT COUNT(*) FROM ZASSET", default=0),
        "own": q("ZBUNDLESCOPE = 0"),
        "shared_album": q("ZBUNDLESCOPE = 2"),          # §4.1: no local file
        "active": q("ZBUNDLESCOPE = 0 AND ZTRASHEDSTATE = 0"),
        "trashed": q("ZTRASHEDSTATE = 1"),
        "hidden": q("ZHIDDEN = 1"),
        "excluded": q("ZVISIBILITYSTATE = 2"),          # §4.2: not the same as hidden
        "favorites": q("ZFAVORITE = 1"),
        "photos": q("ZKIND = 0 AND ZBUNDLESCOPE = 0 AND ZTRASHEDSTATE = 0"),
        "videos": q("ZKIND = 1 AND ZBUNDLESCOPE = 0 AND ZTRASHEDSTATE = 0"),
        "edited": q("ZADJUSTMENTSSTATE IN (2,3)"),      # §11: 2 and 3 both mean edited
        # §8: -180.0 is the "no location" sentinel, not NULL.
        "with_location": q("ZLATITUDE IS NOT NULL AND ZLATITUDE > ? "
                           "AND ZBUNDLESCOPE = 0 AND ZTRASHEDSTATE = 0", ph.NO_LOCATION),
        "cloud_synced": q("ZCLOUDLOCALSTATE = 1"),      # §13
    }
    rng = con.execute(
        "SELECT MIN(ZDATECREATED) a, MAX(ZDATECREATED) b FROM ZASSET "
        "WHERE ZTRASHEDSTATE = 0 AND ZDATECREATED IS NOT NULL").fetchone()
    if rng and rng["a"]:
        out["oldest_capture_ts"] = apple_to_unix(rng["a"])
        out["newest_capture_ts"] = apple_to_unix(rng["b"])
    out["newest_import_ts"] = apple_to_unix(
        scalar(con, "SELECT MAX(ZADDEDDATE) FROM ZASSET"))
    return out


def collect_people(con) -> dict:
    """§7: a named face is the join of ZDETECTEDFACE, ZPERSON and ZASSET,
    filtered ZHIDDEN=0 AND ZISINTRASH=0."""
    return {
        "faces_total": scalar(con, "SELECT COUNT(*) FROM ZDETECTEDFACE", default=0),
        "faces_visible": scalar(con,
            "SELECT COUNT(*) FROM ZDETECTEDFACE WHERE ZHIDDEN=0 AND ZISINTRASH=0",
            default=0),
        "faces_named": scalar(con, """
            SELECT COUNT(*) FROM ZDETECTEDFACE f
              JOIN ZPERSON p ON p.Z_PK = f.ZPERSONFORFACE
             WHERE f.ZHIDDEN=0 AND f.ZISINTRASH=0
               AND TRIM(COALESCE(p.ZFULLNAME,'')) <> ''""", default=0),
        "faces_manual": scalar(con,
            "SELECT COUNT(*) FROM ZDETECTEDFACE WHERE ZMANUAL = 1", default=0),
        "persons_total": scalar(con, "SELECT COUNT(*) FROM ZPERSON", default=0),
        "persons_named": scalar(con,
            "SELECT COUNT(*) FROM ZPERSON WHERE TRIM(COALESCE(ZFULLNAME,'')) <> ''",
            default=0),
        "faceprints": scalar(con, "SELECT COUNT(*) FROM ZDETECTEDFACEPRINT", default=0),
        "facecrops": scalar(con, "SELECT COUNT(*) FROM ZFACECROP", default=0),
        # §22 lists ZDEFERREDREBUILDFACE as part of the recognition machinery;
        # a non-zero count means face work is outstanding.
        "deferred_rebuild": scalar(con,
            "SELECT COUNT(*) FROM ZDEFERREDREBUILDFACE", default=0),
    }


def collect_top_people(con, limit: int = 12) -> list[dict]:
    out = []
    try:
        for r in con.execute("""
            SELECT ZFULLNAME name, ZFACECOUNT n FROM ZPERSON
             WHERE TRIM(COALESCE(ZFULLNAME,'')) <> ''
             ORDER BY ZFACECOUNT DESC LIMIT ?""", (limit,)):
            out.append({"name": r["name"], "faces": r["n"]})
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        pass
    return out


def collect_albums(con) -> dict:
    """§10: album-like objects share ZGENERICALBUM and are told apart by
    Z_ENT + ZKIND, both resolved at runtime."""
    ents = ph.entity_numbers(con)
    album, folder = ents.get("Album"), ents.get("Folder")
    out = {
        "generic_total": scalar(con, "SELECT COUNT(*) FROM ZGENERICALBUM", default=0),
        "user_albums": scalar(con,
            "SELECT COUNT(*) FROM ZGENERICALBUM WHERE Z_ENT=? AND ZKIND=2",
            album, default=0),
        "folders": scalar(con,
            "SELECT COUNT(*) FROM ZGENERICALBUM WHERE Z_ENT=? AND ZKIND=4000",
            folder, default=0),
        "import_sessions": scalar(con,
            "SELECT COUNT(*) FROM ZGENERICALBUM WHERE ZKIND=1506", default=0),
        "shared_albums": scalar(con,
            "SELECT COUNT(*) FROM ZGENERICALBUM WHERE ZKIND=1505", default=0),
        "smart_albums": scalar(con,
            "SELECT COUNT(*) FROM ZGENERICALBUM WHERE ZKIND IN (1507,1552) "
            "OR (ZKIND BETWEEN 1600 AND 1641)", default=0),
        "moments": scalar(con, "SELECT COUNT(*) FROM ZMOMENT", default=0),
        "memories": scalar(con, "SELECT COUNT(*) FROM ZMEMORY", default=0),
    }
    try:
        table, alb_col, ast_col = ph.resolve_album_join(con)
        out["album_memberships"] = scalar(con, f"SELECT COUNT(*) FROM {table}", default=0)
        out["album_join_table"] = table
    except LookupError:
        pass
    return out


def collect_keywords(con) -> dict:
    """§6: keywords hang off ZADDITIONALASSETATTRIBUTES, never ZASSET."""
    out = {"keywords_total": scalar(con, "SELECT COUNT(*) FROM ZKEYWORD", default=0)}
    try:
        table, attr_col, kw_col = ph.resolve_keyword_join(con)
        out["keyword_join_table"] = table
        out["keyword_links"] = scalar(con, f"SELECT COUNT(*) FROM {table}", default=0)
        # The same keyword can be linked twice through different rows (§6),
        # so count distinct pairs for "assets tagged".
        out["assets_tagged"] = scalar(con, f"""
            SELECT COUNT(DISTINCT aa.ZASSET) FROM {table} j
              JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.Z_PK = j.{attr_col}""", default=0)
    except LookupError:
        pass
    return out


def collect_top_keywords(con, limit: int = 15) -> list[dict]:
    try:
        table, attr_col, kw_col = ph.resolve_keyword_join(con)
    except LookupError:
        return []
    out = []
    try:
        for r in con.execute(f"""
            SELECT k.ZTITLE t, COUNT(DISTINCT aa.ZASSET) n
              FROM {table} j
              JOIN ZKEYWORD k ON k.Z_PK = j.{kw_col}
              JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.Z_PK = j.{attr_col}
             WHERE k.ZTITLE IS NOT NULL
             GROUP BY 1 ORDER BY n DESC LIMIT ?""", (limit,)):
            out.append({"keyword": ph.nfc(r["t"]), "assets": r["n"]})
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        pass
    return out


def collect_resources(con) -> dict:
    """§12: ZINTERNALRESOURCE is the inventory of every file backing an asset.
    ZLOCALAVAILABILITY 1 = on this Mac, -1 = not (the 'Optimise Mac Storage' case)."""
    out = {
        "resources_total": scalar(con, "SELECT COUNT(*) FROM ZINTERNALRESOURCE", default=0),
        "local_present": scalar(con,
            "SELECT COUNT(*) FROM ZINTERNALRESOURCE WHERE ZLOCALAVAILABILITY = 1",
            default=0),
        "local_absent": scalar(con,
            "SELECT COUNT(*) FROM ZINTERNALRESOURCE WHERE ZLOCALAVAILABILITY = -1",
            default=0),
        "remote_present": scalar(con,
            "SELECT COUNT(*) FROM ZINTERNALRESOURCE WHERE ZREMOTEAVAILABILITY = 1",
            default=0),
        "bytes_local": scalar(con,
            "SELECT SUM(ZDATALENGTH) FROM ZINTERNALRESOURCE WHERE ZLOCALAVAILABILITY = 1",
            default=0),
        "cloud_masters": scalar(con, "SELECT COUNT(*) FROM ZCLOUDMASTER", default=0),
    }
    by_type = []
    try:
        for r in con.execute(
            "SELECT ZRESOURCETYPE t, COUNT(*) n, SUM(ZDATALENGTH) b "
            "FROM ZINTERNALRESOURCE GROUP BY 1 ORDER BY n DESC"):
            by_type.append({"type": r["t"], "count": r["n"], "bytes": r["b"] or 0})
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        pass
    out["by_type"] = by_type
    return out


def collect_analysis_coverage(con) -> dict:
    """Per-worker analysis completion, from the *VERSION columns.

    ZASSETANALYSISSTATE is empty in every library measured (§19), so it cannot
    answer "how far has analysis got". ZMEDIAANALYSISASSETATTRIBUTES can:
    it has one row per asset and a version column per analysis worker, left at
    0 until that worker has run. A version > 0 is therefore direct evidence of
    completed work — the closest thing to a "finished jobs" record that exists.
    """
    try:
        cols = [r["name"] for r in con.execute(
            "PRAGMA table_info(ZMEDIAANALYSISASSETATTRIBUTES)")]
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        return {}
    vcols = [c for c in cols if c.endswith("VERSION")]
    if not vcols:
        return {}
    sel = ", ".join(f"SUM(CASE WHEN {c} > 0 THEN 1 ELSE 0 END)" for c in vcols)
    try:
        row = con.execute(
            f"SELECT COUNT(*), {sel} FROM ZMEDIAANALYSISASSETATTRIBUTES").fetchone()
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        return {}
    total = row[0] or 0
    workers = []
    for c, done in zip(vcols, row[1:]):
        label = c[1:-len("VERSION")].title()             # ZVISUALSEARCHVERSION -> Visualsearch
        # Eligibility for video workers is not established (e.g. Live Photos).
        # All assets would be a misleading completion denominator.
        denominator = None if c.startswith("ZVIDEO") else total
        workers.append({"column": c, "label": label, "done": done or 0,
                        "total": denominator,
                        "pct": (100.0 * done / denominator)
                        if done and denominator else None})
    workers.sort(key=lambda w: w["done"])
    return {"total": total, "workers": workers}


def collect_analysis_side_tables(con) -> dict:
    return {
        "scene_classifications": scalar(con,
            "SELECT COUNT(*) FROM ZSCENECLASSIFICATION", default=0),
        "scene_prints": scalar(con, "SELECT COUNT(*) FROM ZSCENEPRINT", default=0),
        "computed_attributes": scalar(con,
            "SELECT COUNT(*) FROM ZCOMPUTEDASSETATTRIBUTES", default=0),
        "visual_search": scalar(con,
            "SELECT COUNT(*) FROM ZVISUALSEARCHATTRIBUTES", default=0),
        # §19: exists but was empty in every library measured. Reported so the
        # dashboard shows the fact rather than silently omitting it.
        "asset_analysis_state_rows": scalar(con,
            "SELECT COUNT(*) FROM ZASSETANALYSISSTATE", default=0),
    }


# --------------------------------------------------------------------------
# tier: slow — integrity (§3.2)
# --------------------------------------------------------------------------
def collect_pk_health(con) -> dict:
    with read_transaction(con):
        return _collect_pk_health(con)


def _collect_pk_health(con) -> dict:
    """Compare MAX(Z_PK) against Z_PRIMARYKEY.Z_MAX for every entity.

    §3.2: a table whose highest row exceeds its counter means the next save
    Photos attempts collides, and *every* edit in the library then fails
    silently. PRAGMA integrity_check returns ok in this state (§20.2), so this
    check is not redundant with it — it is the only one that catches it.
    """
    faults, checked = [], 0
    try:
        entries = con.execute(
            "SELECT Z_NAME, Z_ENT, Z_MAX FROM Z_PRIMARYKEY").fetchall()
        tables = {r["name"].upper() for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        return {"checked": 0, "faults": [], "ok": None}

    for e in entries:
        name = e["Z_NAME"]
        # Model entities live in Z<NAME>; the internal history tables are
        # ACHANGE / ATRANSACTION / ATRANSACTIONSTRING (§3.2).
        for candidate in (f"Z{name}".upper(), f"A{name}".upper()):
            if candidate not in tables:
                continue
            mx = scalar(con, f"SELECT COALESCE(MAX(Z_PK),0) FROM {candidate}", default=0)
            checked += 1
            if mx > (e["Z_MAX"] or 0):
                faults.append({"table": candidate, "entity": name,
                               "max_pk": mx, "z_max": e["Z_MAX"],
                               "behind_by": mx - (e["Z_MAX"] or 0)})
            break
    return {"checked": checked, "faults": faults, "ok": not faults}


def collect_table_census(con, limit: int = 60) -> list[dict]:
    """Row counts for the tables worth watching, plus their Z_MAX."""
    zmax = {}
    try:
        zmax = {r["Z_NAME"].upper(): r["Z_MAX"] for r in
                con.execute("SELECT Z_NAME, Z_MAX FROM Z_PRIMARYKEY")}
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        pass
    try:
        names = [r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name LIKE 'Z%' OR name LIKE 'A%') ORDER BY name")]
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        return []
    out = []
    for t in names:
        if t.startswith("Z_RT_"):                 # r-tree internals: noise
            continue
        n = scalar(con, f"SELECT COUNT(*) FROM {t}", default=None)
        if n is None:
            continue
        entity = t[1:] if t.startswith(("Z", "A")) else t
        out.append({"table": t, "rows": n, "z_max": zmax.get(entity.upper())})
    out.sort(key=lambda r: -r["rows"])
    return out[:limit]


# --------------------------------------------------------------------------
# search index (§19)
# --------------------------------------------------------------------------
def collect_search_index(lib: Path, con, dbdir: Path | None = None) -> dict:
    psi_path = lib / "database" / "search" / "psi.sqlite"
    out: dict = {"present": psi_path.exists()}
    if not out["present"]:
        return out
    st = psi_path.stat()
    out["bytes"] = st.st_size
    out["mtime"] = st.st_mtime
    for suffix in ("-wal", "-shm"):
        p = psi_path.with_name(psi_path.name + suffix)
        out[f"{suffix.strip('-')}_bytes"] = p.stat().st_size if p.exists() else 0

    spotlight = lib / "database" / "search" / "Spotlight"
    out["spotlight_present"] = spotlight.exists()

    psi = open_psi(lib, dbdir)
    if psi is None:
        out["error"] = "psi.sqlite present but not readable"
        return out
    try:
        out["assets_indexed"] = scalar(psi, "SELECT COUNT(*) FROM assets", default=0)
        out["groups_total"] = scalar(psi, "SELECT COUNT(*) FROM groups", default=0)
        out["assets_total"] = scalar(con,
            "SELECT COUNT(*) FROM ZASSET WHERE ZTRASHEDSTATE = 0", default=0)
        # The index can hold more rows than the library has assets — measured
        # here at 40,589 distinct (uuid_0, uuid_1) pairs against 21,737 untrashed
        # assets. Why is not established: §22 records that the byte order of the
        # split UUID was never verified, so these rows cannot be joined back to
        # ZASSET to find out whether they are stale, or cover objects other than
        # assets. Flagged rather than divided into a >100% "coverage".
        out["index_exceeds_library"] = out["assets_indexed"] > out["assets_total"]

        cats = []
        for r in psi.execute(
            "SELECT category, COUNT(*) n FROM groups GROUP BY 1 ORDER BY n DESC LIMIT 20"):
            cats.append({"category": r["category"], "count": r["n"],
                         # §19 documents only these three category numbers.
                         "meaning": {1200: "user keywords",
                                     1500: "Apple scene labels (singular)",
                                     1501: "Apple scene labels (plural)"}
                                    .get(r["category"])})
        out["categories"] = cats

        # §19: strings here are NUL-terminated, and §16 says compare NFC.
        strip = lambda s: ph.nfc((s or "").rstrip("\x00"))
        titles = {strip(t) for (t,) in
                  con.execute("SELECT ZTITLE FROM ZKEYWORD WHERE ZTITLE IS NOT NULL")}
        titles.discard("")
        indexed = {strip(s) for (s,) in
                   psi.execute("SELECT content_string FROM groups WHERE category=1200")}
        indexed |= {strip(s) for (s,) in
                    psi.execute("SELECT normalized_string FROM groups WHERE category=1200")}
        missing = sorted(t for t in titles
                         if t not in indexed and t.lower() not in indexed)
        out["keywords_total"] = len(titles)
        out["keywords_indexed"] = len(titles) - len(missing)
        out["keywords_missing"] = missing[:40]
        out["keywords_missing_count"] = len(missing)
    except sqlite3.Error as exc:
        raise_if_interrupted(exc)
        out["error"] = str(exc)
    finally:
        psi.close()
    return out


# --------------------------------------------------------------------------
# processes and the unified log (§2.5, §20)
# --------------------------------------------------------------------------
def collect_processes(lib: Path) -> dict:
    """Which daemons hold this library, by inspecting each candidate's open files.

    A whole-system `lsof` takes seconds and shows up as load; §2.5 names the
    processes that matter, so we ask each of them directly instead. `-b -w`
    keeps lsof off blocking stat() calls, which otherwise stall on a network
    mount and produce warnings on stderr.

    The path comparison is NFC-normalised on both sides. §16 documents this for
    keyword and person values, but it applies just as much to paths: for a
    library called 'Photothèque.photoslibrary', lsof and Path.glob hand back
    different Unicode normalisations of the same name, so a raw substring test
    matched 0 of 56 genuine hits and the dashboard confidently reported that
    nothing held a library four daemons had open.
    """
    holders, running = [], {}
    libstr = ph.nfc(str(lib))
    unreadable = 0
    for name in DAEMONS:
        pids = sh(["pgrep", "-x", name], timeout=10).split()
        running[name] = len(pids)
        for pid in pids:
            out = sh(["lsof", "-b", "-w", "-p", pid], timeout=20)
            if not out.strip():
                unreadable += 1          # no privilege to inspect this process
                continue
            n = sum(1 for line in out.splitlines() if libstr in ph.nfc(line))
            if n:
                holders.append({"process": name, "pid": int(pid), "handles": n})
    holders.sort(key=lambda h: -h["handles"])
    return {
        "holders": holders,
        "running": running,
        "photos_running": running.get("Photos", 0) > 0,
        "holder_count": len(holders),
        "unreadable_processes": unreadable,
        # §2.5: a library nothing holds gets no background processing at all.
        # Only claim that when we could actually see every candidate's files —
        # "found none" and "could not look" are different answers.
        "unattended": len(holders) == 0 and unreadable == 0,
    }


DECISION_RE = re.compile(r"Decision: ([A-Z]+)")
POLICY_RE = re.compile(r"name: ([A-Za-z ]+Policy), policyWeight: ([0-9.]+)")


def collect_log(minutes: int = 5, stop_event=None) -> dict:
    """Scheduler verdicts and save errors from the unified log.

    §2.5: whether analysis is *allowed* to run is recorded nowhere on disk —
    only here. MNP = May Not Proceed, CP = Can Proceed, AMNP = Absolutely May
    Not Proceed. The heavy policy is 'Device Activity': it refuses to run while
    somebody is at the keyboard.

    This is the one expensive collector (`log show` takes seconds to tens of
    seconds), so it runs on its own slower cadence.
    """
    out: dict = {"window_minutes": minutes, "collected_at": time.time()}

    dec = sh([LOG, "show", "--last", f"{minutes}m", "--predicate",
              'eventMessage CONTAINS "mediaanalysisd.photos"', "--style", "compact"],
             timeout=180, stop_event=stop_event)
    verdicts = Counter(DECISION_RE.findall(dec))
    out["decisions"] = dict(verdicts)
    out["decisions_total"] = sum(verdicts.values())
    policies = Counter(f"{m[0]} (w={m[1]})" for m in POLICY_RE.findall(dec))
    out["policies"] = policies.most_common(8)
    cp, mnp = verdicts.get("CP", 0), verdicts.get("MNP", 0) + verdicts.get("AMNP", 0)
    if not verdicts:
        out["throttle_state"] = "no decisions logged"
    elif mnp and not cp:
        out["throttle_state"] = "fully throttled"
    elif mnp > cp:
        out["throttle_state"] = "mostly throttled"
    elif cp:
        out["throttle_state"] = "running"
    out["can_proceed_pct"] = (100.0 * cp / (cp + mnp)) if (cp + mnp) else None

    txt = sh([LOG, "show", "--last", f"{minutes}m", "--predicate",
              'process IN {"Photos","photolibraryd","mediaanalysisd","photoanalysisd"}',
              "--style", "compact"], timeout=180, stop_event=stop_event)
    out["log_lines"] = txt.count("\n")
    # §20.2: this exact string (Apple's own spelling) is the signature of a
    # primary-key counter that has fallen behind.
    out["constraint_violations"] = txt.count("constraint primary key violation")
    out["photokit_ok"] = len(re.findall(r"success: YES", txt))
    out["photokit_fail"] = len(re.findall(r"success: NO", txt))
    # §20: match the specific cancellation text — grepping for "cancel" catches
    # routine xpc_connection_cancel() teardown and means nothing.
    out["scene_cancelled"] = txt.count("Scene asset processing cancelled")
    out["face_cancelled"] = txt.count("Face asset processing cancelled")
    out["user_cancelled"] = txt.count("userCanceledErr")
    out["spotlight_donations"] = len(re.findall(r"Donating (\d+) items? to Spotlight", txt))
    out["search_index_jobs"] = txt.count("search jobs needing processing")

    interesting = []
    for line in txt.splitlines():
        if re.search(r"save failed|constraint|PHPhotosErrorDomain|Repair|rebuild",
                     line, re.I):
            interesting.append(line.strip()[:400])
    out["notable_lines"] = interesting[-25:]
    return out


# --------------------------------------------------------------------------
# tier: rare — filesystem inventory
# --------------------------------------------------------------------------
def collect_filesystem(lib: Path, stop_event=None) -> dict:
    """Walk the bundle. Expensive (tens of thousands of files), hence 'rare'."""
    out: dict = {"collected_at": time.time()}
    try:
        vfs = os.statvfs(lib)
        out["volume_free_bytes"] = vfs.f_bavail * vfs.f_frsize
        out["volume_total_bytes"] = vfs.f_blocks * vfs.f_frsize
    except OSError:
        pass

    def cancelled():
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("Filesystem inventory cancelled")

    dirs = {}
    for sub in ("originals", "resources/derivatives", "resources/renders",
                "database", "resources/cpl", "private", "scopes"):
        root = lib / sub
        n = total = 0
        if root.exists():
            for dirpath, dirnames, filenames in os.walk(root):
                cancelled()
                # §1: digiKam creates originals/.dtrash and moves files there.
                # A naive walk counts them as library content; prune dot-dirs.
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for f in filenames:
                    cancelled()
                    try:
                        total += os.stat(os.path.join(dirpath, f)).st_size
                        n += 1
                    except OSError:
                        pass
        dirs[sub] = {"files": n, "bytes": total}
    out["directories"] = dirs

    # §1 again: report .dtrash separately rather than hiding it, since it still
    # occupies space inside the bundle.
    dtrash = lib / "originals" / ".dtrash"
    if dtrash.exists():
        n = total = 0
        for dirpath, _, filenames in os.walk(dtrash):
            cancelled()
            for f in filenames:
                cancelled()
                try:
                    total += os.stat(os.path.join(dirpath, f)).st_size
                    n += 1
                except OSError:
                    pass
        out["dtrash"] = {"files": n, "bytes": total}

    # §4.1: originals are UUID.ext; companions carry a _N suffix and are not
    # separate assets. Anchor on <UUID> followed immediately by '.'.
    originals = lib / "originals"
    assets = companions = 0
    if originals.exists():
        for dirpath, dirnames, filenames in os.walk(originals):
            cancelled()
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for f in filenames:
                cancelled()
                if ph.uuid_from_name(f):
                    assets += 1
                elif re.match(r"^[0-9A-F-]{36}_\d+\.", f, re.I):
                    companions += 1
    out["originals_assets"] = assets
    out["originals_companions"] = companions
    return out
