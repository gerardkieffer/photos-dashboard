"""Sampler thread + HTTP API for the Photos library dashboard.

The sampler owns all the reading. HTTP handlers never touch the library: they
serve the last completed sample out of memory and the history out of SQLite, so
a slow `log show` or a spun-down external disk can never make the page hang.

Collection is tiered (see history.TIERS) because the underlying facts move at
different speeds — queue depth every few seconds, the asset census every few
hours. The expensive collectors (the unified log, the filesystem walk) run on
their own threads so a 30-second `log show` does not delay the 10-second queue
sample behind it.
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import collectors as C
from .history import TIERS, History

STATIC = Path(__file__).resolve().parent / "static"


class Sampler(threading.Thread):
    """Collects on a tiered schedule and publishes an in-memory snapshot."""

    def __init__(self, library: Path, history: History, log_minutes: int = 5,
                 log_interval: int = 180, walk_fs: bool = True,
                 snapshotter: C.Snapshotter | None = None):
        super().__init__(daemon=True, name="sampler")
        self.library = Path(library)
        self.history = history
        self.log_minutes = log_minutes
        self.log_interval = log_interval
        self.walk_fs = walk_fs
        self.snapshotter = snapshotter
        self.stop_event = threading.Event()

        self.lock = threading.Lock()
        self.state: dict = {"status": "starting", "library": str(library)}
        self.errors: list[str] = []
        self.started_at = time.time()
        self.pass_counts = {t: 0 for t in TIERS}
        self.last_pass = {t: 0.0 for t in TIERS}
        self.durations: dict[str, float] = {}

        # Previous readings, for the derived throughput maths.
        self._prev_queue: tuple[float, int, int] | None = None   # ts, pending, arrivals_total
        self._prev_flags: dict = {}

    # ---- helpers ---------------------------------------------------------
    def _publish(self, key: str, value) -> None:
        with self.lock:
            self.state[key] = value
            self.state["status"] = "ok"
            self.state["updated_at"] = time.time()

    def _note(self, msg: str) -> None:
        with self.lock:
            self.errors.append(f"{time.strftime('%H:%M:%S')} {msg}")
            del self.errors[:-30]

    def _timed(self, name, fn, *a, **kw):
        t0 = time.time()
        try:
            return fn(*a, **kw)
        finally:
            self.durations[name] = time.time() - t0

    @property
    def dbdir(self) -> Path | None:
        """Where database files are read from: None = the library itself."""
        return self.snapshotter.dir if self.snapshotter else None

    # ---- tiers -----------------------------------------------------------
    def tier_live(self, con) -> None:
        ident = C.collect_identity(self.library)
        queue = self._timed("queue", C.collect_queue, con)
        writes = self._timed("writes", C.collect_write_activity, con)
        now = time.time()

        # Derived throughput. The queue has no completion record, so departures
        # are inferred from the identity
        #     pending_now = pending_prev + arrivals - departures
        # using the count of rows whose ZTIMESTAMP falls inside the interval as
        # `arrivals`. This is the closest thing to a "jobs finished" figure the
        # store supports; it is labelled as derived everywhere it is shown.
        derived = {}
        if self._prev_queue:
            pts, ppend, _ = self._prev_queue
            dt = now - pts
            if dt >= 5:
                arrivals = queue["arrivals_60s"] if dt <= 60 else queue["arrivals_5m"]
                arrivals = arrivals * min(dt, 300) / (60 if dt <= 60 else 300)
                delta = queue["pending"] - ppend
                departures = max(0.0, arrivals - delta)
                derived = {
                    "interval_s": dt,
                    "delta": delta,
                    "arrivals_per_min": arrivals / (dt / 60),
                    "departures_per_min": departures / (dt / 60),
                    "net_drain_per_min": -delta / (dt / 60),
                }
                rate = -delta / (dt / 60)
                if rate > 0.05:
                    derived["eta_hours"] = queue["pending"] / rate / 60
                    derived["trend"] = "draining"
                elif rate < -0.05:
                    derived["trend"] = "growing"
                else:
                    derived["trend"] = "flat"
        self._prev_queue = (now, queue["pending"], queue["arrivals_60s"])

        self._publish("identity", ident)
        self._publish("queue", {**queue, "derived": derived})
        self._publish("writes", writes)

        metrics = {
            "queue.pending": queue["pending"],
            "queue.delayed": queue["delayed"],
            "queue.distinct_identifiers": queue["distinct_identifiers"],
            "queue.arrivals_60s": queue["arrivals_60s"],
            "queue.oldest_age_s": queue["oldest_age_s"],
            "db.bytes": ident.get("db_bytes", 0),
            "db.wal_bytes": ident.get("wal_bytes", 0),
            "writes.transactions_60s": writes["transactions_60s"],
            "writes.transaction_max_pk": writes["transaction_max_pk"],
            "writes.change_max_pk": writes["change_max_pk"],
        }
        if derived:
            metrics["queue.departures_per_min"] = derived["departures_per_min"]
            metrics["queue.arrivals_per_min"] = derived["arrivals_per_min"]
            metrics["queue.net_drain_per_min"] = derived["net_drain_per_min"]
        self.history.record("live", metrics)

    def tier_medium(self, con) -> None:
        mix = self._timed("queue_mix", C.collect_queue_mix, con)
        hot = self._timed("hot_assets", C.collect_queue_hot_assets, con)
        search = self._timed("search", C.collect_search_index,
                             self.library, con, self.dbdir)
        authors = self._timed("authors", C.collect_authors, con)
        changes = self._timed("changes", C.collect_recent_changes, con)
        procs = self._timed("processes", C.collect_processes, self.library)

        # Per-bucket departures, same derivation as the global figure.
        now = time.time()
        for b in mix["buckets"]:
            key = f'{b["jobtype"]}/{b["flags"]}'
            prev = self._prev_flags.get(key)
            if prev:
                pts, pn = prev
                dt = now - pts
                if dt >= 30:
                    b["delta_per_min"] = (b["count"] - pn) / (dt / 60)
            self._prev_flags[key] = (now, b["count"])

        self._publish("queue_mix", mix)
        self._publish("hot_assets", hot)
        self._publish("search", search)
        self._publish("authors", authors)
        self._publish("changes", changes)
        self._publish("processes", procs)

        self.history.record_snapshot("queue_mix", mix)
        self.history.record_snapshot("processes", procs)
        self.history.record_snapshot("authors", authors)

        metrics = {
            "search.bytes": search.get("bytes", 0),
            "search.assets_indexed": search.get("assets_indexed", 0),
            "search.groups_total": search.get("groups_total", 0),
            "search.keywords_indexed": search.get("keywords_indexed", 0),
            "search.keywords_missing": search.get("keywords_missing_count", 0),
            "proc.holders": procs["holder_count"],
            "proc.photos_running": 1 if procs["photos_running"] else 0,
        }
        if search.get("assets_total"):
            metrics["search.coverage_pct"] = (
                100.0 * search.get("assets_indexed", 0) / search["assets_total"])
        self.history.record("medium", {k: v for k, v in metrics.items() if v is not None})

        self._detect_events(procs, search)

    def tier_slow(self, con) -> None:
        assets = self._timed("assets", C.collect_assets, con)
        people = self._timed("people", C.collect_people, con)
        albums = self._timed("albums", C.collect_albums, con)
        keywords = self._timed("keywords", C.collect_keywords, con)
        resources = self._timed("resources", C.collect_resources, con)
        coverage = self._timed("coverage", C.collect_analysis_coverage, con)
        side = self._timed("side_tables", C.collect_analysis_side_tables, con)
        pk = self._timed("pk_health", C.collect_pk_health, con)
        census = self._timed("census", C.collect_table_census, con)
        top_people = C.collect_top_people(con)
        top_keywords = C.collect_top_keywords(con)

        self._publish("assets", assets)
        self._publish("people", {**people, "top": top_people})
        self._publish("albums", albums)
        self._publish("keywords", {**keywords, "top": top_keywords})
        self._publish("resources", resources)
        self._publish("coverage", coverage)
        self._publish("side_tables", side)
        self._publish("pk_health", pk)
        self._publish("census", census)

        self.history.record_snapshot("table_census", {"tables": census})
        self.history.record_snapshot("analysis_coverage", coverage)

        metrics = {f"assets.{k}": v for k, v in assets.items()
                   if isinstance(v, (int, float)) and not k.endswith("_ts")}
        metrics |= {f"people.{k}": v for k, v in people.items()}
        metrics |= {f"albums.{k}": v for k, v in albums.items()
                    if isinstance(v, (int, float))}
        metrics |= {f"keywords.{k}": v for k, v in keywords.items()
                    if isinstance(v, (int, float))}
        metrics |= {f"resources.{k}": v for k, v in resources.items()
                    if isinstance(v, (int, float))}
        metrics |= {f"analysis.{k}": v for k, v in side.items()}
        metrics["integrity.pk_faults"] = len(pk["faults"])
        for w in coverage.get("workers", []):
            metrics[f"coverage.{w['column']}"] = w["done"]
        self.history.record("slow", metrics)

        if pk["faults"]:
            # §3.2: this is a live fault. Photos cannot save at all in this
            # state, and PRAGMA integrity_check will still say 'ok' (§20.2).
            names = ", ".join(f"{f['table']} +{f['behind_by']}" for f in pk["faults"])
            self.history.add_event(
                "integrity", "error",
                f"PRIMARY-KEY COUNTER BEHIND — Photos cannot save: {names}")

    def tier_rare(self, con) -> None:
        if not self.walk_fs:
            return
        fs = self._timed("filesystem", C.collect_filesystem, self.library)
        self._publish("filesystem", fs)
        self.history.record_snapshot("filesystem", fs)
        metrics = {"fs.volume_free_bytes": fs.get("volume_free_bytes", 0),
                   "fs.originals_assets": fs.get("originals_assets", 0),
                   "fs.originals_companions": fs.get("originals_companions", 0)}
        for sub, d in fs.get("directories", {}).items():
            key = sub.replace("/", "_")
            metrics[f"fs.{key}.bytes"] = d["bytes"]
            metrics[f"fs.{key}.files"] = d["files"]
        self.history.record("rare", metrics)

    # ---- events ----------------------------------------------------------
    def _detect_events(self, procs, search) -> None:
        prev = getattr(self, "_prev_event_state", {})
        if prev:
            if procs["photos_running"] != prev.get("photos_running"):
                # §2.5: work started by Photos.app is cancelled when it quits.
                self.history.add_event(
                    "photos", "info",
                    "Photos.app launched" if procs["photos_running"]
                    else "Photos.app quit — any on-demand analysis it started is cancelled")
            if procs["unattended"] and not prev.get("unattended"):
                self.history.add_event(
                    "processes", "warn",
                    "nothing holds this library — no background processing will happen")
            if not procs["unattended"] and prev.get("unattended"):
                self.history.add_event("processes", "info",
                                       "daemons attached to the library again")
            pm = prev.get("keywords_missing")
            km = search.get("keywords_missing_count")
            if pm is not None and km is not None and km != pm:
                self.history.add_event(
                    "search", "info",
                    f"keywords not yet searchable: {pm} -> {km}")
        self._prev_event_state = {
            "photos_running": procs["photos_running"],
            "unattended": procs["unattended"],
            "keywords_missing": search.get("keywords_missing_count"),
        }

    # ---- expensive collectors, on their own threads ----------------------
    def _log_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                data = self._timed("log", C.collect_log, self.log_minutes)
                self._publish("log", data)
                m = {f"log.decision_{k}": v for k, v in data["decisions"].items()}
                m["log.constraint_violations"] = data["constraint_violations"]
                m["log.photokit_ok"] = data["photokit_ok"]
                m["log.photokit_fail"] = data["photokit_fail"]
                m["log.scene_cancelled"] = data["scene_cancelled"]
                m["log.face_cancelled"] = data["face_cancelled"]
                if data.get("can_proceed_pct") is not None:
                    m["log.can_proceed_pct"] = data["can_proceed_pct"]
                self.history.record("medium", m)
                prev = getattr(self, "_prev_throttle", None)
                now_state = data.get("throttle_state")
                if prev and now_state and now_state != prev:
                    self.history.add_event("scheduler", "info",
                                           f"scheduler state: {prev} -> {now_state}")
                self._prev_throttle = now_state
                if data["constraint_violations"]:
                    # §20.2: the log names this cause; the database will not.
                    self.history.add_event(
                        "integrity", "error",
                        f"{data['constraint_violations']} persistent-history constraint "
                        f"violations in the last {self.log_minutes}m — see §3.2")
            except Exception as exc:                          # noqa: BLE001
                self._note(f"log collector: {exc}")
            self.stop_event.wait(self.log_interval)

    # ---- main loop -------------------------------------------------------
    def run(self) -> None:
        threading.Thread(target=self._log_loop, daemon=True, name="log").start()
        handlers = {"live": self.tier_live, "medium": self.tier_medium,
                    "slow": self.tier_slow, "rare": self.tier_rare}
        while not self.stop_event.is_set():
            now = time.time()
            due = [t for t, cfg in TIERS.items()
                   if now - self.last_pass[t] >= cfg["interval"]]
            if due:
                con = None
                try:
                    if self.snapshotter:
                        # Refresh before reading, so every tier in this pass
                        # sees one consistent copy rather than a moving target.
                        self._timed("snapshot", self.snapshotter.refresh)
                    con = C.open_db(self.library, self.dbdir)
                    for tier in ("live", "medium", "slow", "rare"):
                        if tier not in due:
                            continue
                        try:
                            handlers[tier](con)
                            self.pass_counts[tier] += 1
                            self.last_pass[tier] = time.time()
                        except Exception as exc:              # noqa: BLE001
                            self._note(f"{tier}: {exc}")
                            traceback.print_exc()
                            self.last_pass[tier] = time.time()
                except Exception as exc:                      # noqa: BLE001
                    with self.lock:
                        self.state["status"] = "library unavailable"
                        self.state["status_detail"] = str(exc)
                    self._note(f"open: {exc}")
                    # An unmounted external volume is the common case; back off
                    # rather than hammering it.
                    self.stop_event.wait(10)
                finally:
                    if con is not None:
                        con.close()
                try:
                    self.history.compact()
                except Exception as exc:                      # noqa: BLE001
                    self._note(f"compact: {exc}")
            self.stop_event.wait(1)

    def snapshot(self) -> dict:
        with self.lock:
            state = dict(self.state)
            errors = list(self.errors)
        state["sampler"] = {
            "uptime_s": time.time() - self.started_at,
            "passes": dict(self.pass_counts),
            "last_pass": dict(self.last_pass),
            "intervals": {t: c["interval"] for t, c in TIERS.items()},
            "durations": {k: round(v, 3) for k, v in self.durations.items()},
            "errors": errors,
        }
        state["access"] = self.snapshotter.status() if self.snapshotter else {
            "mode": "direct",
            # Precise rather than reassuring: what is and is not guaranteed.
            "note": "Photos.sqlite and its WAL are never written (proved "
                    "byte-for-byte by test_readonly.py). SQLite may update the "
                    "-shm WAL-index, a derived scratch file every WAL reader "
                    "touches. Use --snapshot-mode to avoid even that.",
        }
        state["history"] = self.history.stats()
        return state


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Supervisor:
    """Owns the current sampler, so the library can be switched while running.

    Switching means three things move together: the sampler, the history file
    (one per library — mixing them would draw a cliff from one library's asset
    count to another's), and the snapshot directory. Doing it here keeps that
    invariant in one place, and holding the lock across the swap means an HTTP
    request can never observe a half-switched state.
    """

    def __init__(self, opts: dict):
        self.opts = opts
        self.lock = threading.Lock()
        self.sampler: Sampler | None = None
        self.history: History | None = None
        self.snapshotter: C.Snapshotter | None = None

    def start(self, library: Path) -> None:
        with self.lock:
            self._start_locked(Path(library))

    def _start_locked(self, library: Path) -> None:
        o = self.opts
        self.history = History(o["history_for"](library))
        self.snapshotter = None
        if o["snapshot_mode"]:
            scratch = Path(tempfile.mkdtemp(prefix="photodash-snap-"))
            self.snapshotter = C.Snapshotter(library, scratch,
                                             o["snapshot_interval"])
            self.snapshotter.refresh(force=True)
        self.sampler = Sampler(library, self.history, o["log_minutes"],
                               o["log_interval"], o["walk_fs"], self.snapshotter)
        self.sampler.start()

    def _check_readable(self, library: Path) -> bool:
        """Verify we can read the library — without opening it in snapshot mode.

        Opening a WAL store is exactly what writes its -shm, so in snapshot mode
        the check is done against a throwaway copy instead. Copying is pure
        reading, so the library itself is still never opened for a write.
        """
        if not self.opts["snapshot_mode"]:
            return C.readable(library)
        scratch = Path(tempfile.mkdtemp(prefix="photodash-check-"))
        try:
            probe = C.Snapshotter(library, scratch, interval=0)
            if not probe.refresh(force=True):
                return False
            return C.readable(library, dbdir=probe.dir)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _stop_locked(self) -> None:
        if self.sampler:
            self.sampler.stop_event.set()
            self.sampler.join(timeout=5)
        if self.history:
            self.history.close()
        if self.snapshotter:
            self.snapshotter.cleanup()

    def switch(self, library: Path) -> dict:
        library = Path(library)
        if not self._check_readable(library):
            return {"ok": False, "error": f"cannot read {library}"}
        with self.lock:
            if self.sampler and self.sampler.library == library:
                return {"ok": True, "library": str(library), "unchanged": True}
            self._stop_locked()
            self._start_locked(library)
        return {"ok": True, "library": str(library)}

    def discover(self) -> list[dict]:
        # Shallow probe in snapshot mode: opening every candidate to test it
        # would write each one's -shm, which is the thing snapshot mode exists
        # to avoid.
        return C.discover_libraries(deep=not self.opts["snapshot_mode"])

    def snapshot(self) -> dict:
        with self.lock:
            sampler = self.sampler
        return sampler.snapshot() if sampler else {"status": "starting"}

    def series(self, *a, **kw):
        with self.lock:
            history = self.history
        return history.series(*a, **kw) if history else {}

    def events(self, *a, **kw):
        with self.lock:
            history = self.history
        return history.events(*a, **kw) if history else []

    def snapshot_history(self, *a, **kw):
        with self.lock:
            history = self.history
        return history.snapshot_history(*a, **kw) if history else []

    def shutdown(self) -> None:
        with self.lock:
            self._stop_locked()


class Handler(BaseHTTPRequestHandler):
    server_version = "PhotosDashboard/1.0"
    sup: Supervisor = None           # type: ignore[assignment]

    def log_message(self, fmt, *args):
        pass                          # the access log is noise here

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:                                 # noqa: N802
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/snapshot":
                return self._json(self.sup.snapshot())
            if path == "/api/series":
                metrics = [m for m in qs.get("metrics", [""])[0].split(",") if m]
                window = float(qs.get("window", ["3600"])[0])
                points = int(qs.get("points", ["600"])[0])
                return self._json({
                    "window": window,
                    "series": self.sup.series(metrics, window, points),
                })
            if path == "/api/events":
                return self._json({"events": self.sup.events(
                    limit=int(qs.get("limit", ["200"])[0]))})
            if path == "/api/snapshot-history":
                name = qs.get("name", ["queue_mix"])[0]
                return self._json({"name": name, "rows": self.sup.snapshot_history(
                    name, limit=int(qs.get("limit", ["100"])[0]))})
            if path == "/api/libraries":
                current = self.sup.snapshot().get("identity", {}).get("path")
                return self._json({
                    "libraries": self.sup.discover(),
                    "current": current,
                })
            if path == "/api/health":
                snap = self.sup.snapshot()
                return self._json({"status": snap.get("status"),
                                   "updated_at": snap.get("updated_at")})
            self._json({"error": "not found", "path": path}, 404)
        except Exception as exc:                              # noqa: BLE001
            traceback.print_exc()
            self._json({"error": str(exc)}, 500)

    def do_POST(self) -> None:                                # noqa: N802
        url = urlparse(self.path)
        try:
            if url.path != "/api/select-library":
                return self._json({"error": "not found", "path": url.path}, 404)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            target = body.get("path")
            if not target:
                return self._json({"error": "no path given"}, 400)
            # Only ever switch to a library discovery already found, so a POST
            # cannot be used to point the server at an arbitrary filesystem path.
            known = {e["path"] for e in C.discover_libraries()}
            if target not in known:
                return self._json({"error": "not a discovered library"}, 400)
            result = self.sup.switch(Path(target))
            self._json(result, 200 if result.get("ok") else 400)
        except Exception as exc:                              # noqa: BLE001
            traceback.print_exc()
            self._json({"error": str(exc)}, 500)

    def _static(self, name: str) -> None:
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.exists():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def serve(library: Path, history_for, host: str, port: int,
          log_minutes: int, log_interval: int, walk_fs: bool,
          snapshot_mode: bool = False, snapshot_interval: float = 60.0) -> None:
    sup = Supervisor({
        "history_for": history_for,
        "log_minutes": log_minutes,
        "log_interval": log_interval,
        "walk_fs": walk_fs,
        "snapshot_mode": snapshot_mode,
        "snapshot_interval": snapshot_interval,
    })
    sup.start(library)
    Handler.sup = sup
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"  library   {library}")
    print(f"  history   {history_for(library)}")
    if snapshot_mode:
        s = sup.snapshotter
        how = "APFS clone" if (s and s.cloned) else "copy"
        print(f"  access    snapshot mode ({how}, refreshed every "
              f"{snapshot_interval:.0f}s) — the library is only ever read")
    else:
        print("  access    direct read-only — Photos.sqlite and its WAL are never")
        print("            written; SQLite may update the -shm WAL-index scratch")
        print("            file. Use --snapshot-mode to avoid even that.")
    print(f"  dashboard http://{host if host != '0.0.0.0' else 'localhost'}:{port}/")
    print("  Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
        sup.shutdown()
