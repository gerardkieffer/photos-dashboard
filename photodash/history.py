"""Tiered time-series store for the Photos library dashboard.

Different facts about a library change on wildly different timescales, and
storing them all at one interval is either useless or wasteful:

    queue depth          moves every few seconds   -> sample fast, keep briefly
    search-index size    moves every few minutes   -> sample slowly, keep for days
    asset / face counts  move over days            -> sample rarely, keep for a year

So every metric belongs to a *tier*, and each tier declares its own sampling
interval, how long the raw samples are kept, and a chain of rollups it is
compacted into afterwards. A rollup row keeps avg/min/max/n, so a downsampled
chart still shows the spikes rather than averaging them away.

Rows live in one table keyed by (metric, res, ts), where res is the bucket
width in seconds and res=0 means a raw sample. `series()` then picks whichever
resolution gives a sensible number of points for the requested window.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

HOUR = 3600
DAY = 86400

# interval  — how often the sampler collects this tier
# raw_keep  — seconds of raw samples kept before compaction (None = keep forever)
# rollups   — [(bucket_seconds, keep_seconds), ...] applied in order
TIERS: dict[str, dict] = {
    # queue depth, WAL size, process liveness: seconds matter
    "live": dict(interval=10, raw_keep=3 * HOUR,
                 rollups=[(60, 48 * HOUR), (900, 30 * DAY)]),
    # search index, log verdicts, write rate: minutes matter
    "medium": dict(interval=60, raw_keep=48 * HOUR,
                   rollups=[(900, 30 * DAY), (6 * HOUR, 400 * DAY)]),
    # library content census: days matter
    "slow": dict(interval=900, raw_keep=30 * DAY,
                 rollups=[(6 * HOUR, 400 * DAY)]),
    # filesystem inventory: expensive, changes slowly, keep everything
    "rare": dict(interval=6 * HOUR, raw_keep=None, rollups=[]),
}

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sample (
    metric TEXT    NOT NULL,
    res    INTEGER NOT NULL,      -- 0 = raw, else bucket width in seconds
    ts     REAL    NOT NULL,      -- unix seconds (bucket start when res > 0)
    value  REAL,                  -- raw value, or the bucket mean
    vmin   REAL,
    vmax   REAL,
    n      INTEGER NOT NULL DEFAULT 1,
    tier   TEXT    NOT NULL,
    PRIMARY KEY (metric, res, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_sample_sweep ON sample(tier, res, ts);

-- Composite structures that are not single numbers: the job mix, the table
-- census, the process list. Kept sparsely; the charts use `sample`.
CREATE TABLE IF NOT EXISTS snapshot (
    name TEXT NOT NULL,
    ts   REAL NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (name, ts)
) WITHOUT ROWID;

-- Notable transitions, so the history is readable and not just plottable.
CREATE TABLE IF NOT EXISTS event (
    ts       REAL NOT NULL,
    kind     TEXT NOT NULL,
    severity TEXT NOT NULL,        -- info | warn | error
    message  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_event_ts ON event(ts DESC);
"""

SNAPSHOT_KEEP = {
    "queue_mix": 30 * DAY,
    "table_census": 400 * DAY,
    "processes": 7 * DAY,
    "analysis_coverage": 400 * DAY,
    "authors": 7 * DAY,
}


class History:
    """Thread-safe wrapper around the time-series SQLite file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(self.path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(SCHEMA)
        self._con.commit()

    # ---- writing ---------------------------------------------------------
    def record(self, tier: str, values: dict[str, float], ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        # bool is a subclass of int, and a True/False metric is a legitimate 0/1
        # series here, so it is deliberately allowed through rather than filtered.
        rows = [(m, 0, ts, float(v), float(v), float(v), 1, tier)
                for m, v in values.items() if isinstance(v, (int, float))]
        if not rows:
            return
        with self._lock:
            self._con.executemany(
                "INSERT OR REPLACE INTO sample(metric,res,ts,value,vmin,vmax,n,tier) "
                "VALUES (?,?,?,?,?,?,?,?)", rows)
            self._con.commit()

    def record_snapshot(self, name: str, payload, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO snapshot(name,ts,payload) VALUES (?,?,?)",
                (name, ts, json.dumps(payload, default=str)))
            self._con.commit()

    def add_event(self, kind: str, severity: str, message: str,
                  ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._con.execute(
                "INSERT INTO event(ts,kind,severity,message) VALUES (?,?,?,?)",
                (ts, kind, severity, message))
            self._con.commit()

    # ---- reading ---------------------------------------------------------
    def series(self, metrics: list[str], window: float,
               max_points: int = 600) -> dict[str, list]:
        """Return {metric: [[ts, value, vmin, vmax], ...]} over the last `window`.

        Picks the coarsest resolution that still yields >= 2 points and the
        finest that stays under `max_points`, so a 30-day view does not try to
        ship 260,000 ten-second samples to the browser.
        """
        since = time.time() - window
        out: dict[str, list] = {}
        with self._lock:
            for metric in metrics:
                avail = [r["res"] for r in self._con.execute(
                    "SELECT DISTINCT res FROM sample WHERE metric=? AND ts>=? "
                    "ORDER BY res", (metric, since))]
                if not avail:
                    out[metric] = []
                    continue
                chosen = avail[0]
                for res in avail:
                    step = res or 1
                    if window / step <= max_points:
                        chosen = res
                        break
                    chosen = res
                rows = self._con.execute(
                    "SELECT ts,value,vmin,vmax FROM sample "
                    "WHERE metric=? AND res=? AND ts>=? ORDER BY ts",
                    (metric, chosen, since)).fetchall()
                if len(rows) > max_points:            # last-resort thinning
                    stride = len(rows) // max_points + 1
                    rows = rows[::stride]
                out[metric] = [[r["ts"], r["value"], r["vmin"], r["vmax"]]
                               for r in rows]
        return out

    def latest_snapshot(self, name: str):
        with self._lock:
            row = self._con.execute(
                "SELECT ts,payload FROM snapshot WHERE name=? ORDER BY ts DESC LIMIT 1",
                (name,)).fetchone()
        return (row["ts"], json.loads(row["payload"])) if row else (None, None)

    def snapshot_history(self, name: str, limit: int = 200):
        with self._lock:
            rows = self._con.execute(
                "SELECT ts,payload FROM snapshot WHERE name=? ORDER BY ts DESC LIMIT ?",
                (name, limit)).fetchall()
        return [{"ts": r["ts"], **json.loads(r["payload"])}
                if isinstance(json.loads(r["payload"]), dict)
                else {"ts": r["ts"], "value": json.loads(r["payload"])}
                for r in rows]

    def events(self, limit: int = 200, since: float | None = None):
        sql = "SELECT ts,kind,severity,message FROM event"
        args: list = []
        if since:
            sql += " WHERE ts >= ?"
            args.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._con.execute(sql, args)]

    def value_at(self, metric: str, before: float):
        """Most recent raw sample at or before `before` — used for rate maths."""
        with self._lock:
            row = self._con.execute(
                "SELECT ts,value FROM sample WHERE metric=? AND res=0 AND ts<=? "
                "ORDER BY ts DESC LIMIT 1", (metric, before)).fetchone()
        return (row["ts"], row["value"]) if row else (None, None)

    def first_ts(self):
        with self._lock:
            row = self._con.execute("SELECT MIN(ts) t FROM sample").fetchone()
        return row["t"] if row and row["t"] else None

    # ---- compaction ------------------------------------------------------
    def compact(self) -> None:
        """Roll raw samples up into coarser buckets and drop what has expired.

        Idempotent and cheap enough to run on every sampler pass; it only
        touches rows that have aged past their tier's raw_keep.
        """
        now = time.time()
        with self._lock:
            for tier, cfg in TIERS.items():
                src_res = 0
                keep = cfg["raw_keep"]
                for bucket, bucket_keep in cfg["rollups"]:
                    if keep is None:
                        break
                    cutoff = now - keep
                    self._con.execute(
                        """INSERT OR REPLACE INTO sample(metric,res,ts,value,vmin,vmax,n,tier)
                           SELECT metric, ?, CAST(ts/? AS INTEGER)*?,
                                  SUM(value*n)/SUM(n), MIN(vmin), MAX(vmax), SUM(n), tier
                             FROM sample
                            WHERE tier=? AND res=? AND ts < ?
                            GROUP BY metric, CAST(ts/? AS INTEGER)""",
                        (bucket, bucket, bucket, tier, src_res, cutoff, bucket))
                    self._con.execute(
                        "DELETE FROM sample WHERE tier=? AND res=? AND ts < ?",
                        (tier, src_res, cutoff))
                    src_res, keep = bucket, bucket_keep
                if keep is not None:
                    self._con.execute(
                        "DELETE FROM sample WHERE tier=? AND res=? AND ts < ?",
                        (tier, src_res, now - keep))
            for name, keep in SNAPSHOT_KEEP.items():
                self._con.execute("DELETE FROM snapshot WHERE name=? AND ts < ?",
                                  (name, now - keep))
            self._con.execute("DELETE FROM event WHERE ts < ?", (now - 400 * DAY,))
            self._con.commit()

    def stats(self) -> dict:
        with self._lock:
            rows = self._con.execute(
                "SELECT res, COUNT(*) c FROM sample GROUP BY res ORDER BY res").fetchall()
            metrics = self._con.execute(
                "SELECT COUNT(DISTINCT metric) c FROM sample").fetchone()["c"]
            events = self._con.execute("SELECT COUNT(*) c FROM event").fetchone()["c"]
        return {
            "file": str(self.path),
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
            "metrics": metrics,
            "events": events,
            "rows_by_res": {str(r["res"]): r["c"] for r in rows},
            "first_ts": self.first_ts(),
        }

    def close(self) -> None:
        with self._lock:
            self._con.close()
