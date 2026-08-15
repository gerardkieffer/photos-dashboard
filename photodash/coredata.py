"""Core Data helpers for reading a Photos library, and nothing else.

Photos.sqlite is a Core Data store, which dictates every naming convention in
it. Three of those conventions will silently produce wrong answers if you
assume instead of resolving, and this module exists so that the rest of the
package cannot get them wrong:

  * entity numbers (`Z_ENT`) are **not stable** — not even across a Repair
    Library on the same machine, with an unchanged schema version. Measured on
    one library, Keyword moved 47 -> 52 and Person 54 -> 59. So they are looked
    up at read time, on the connection that uses them, every time.

  * many-to-many join tables are *named* after those same unstable numbers
    (`Z_1KEYWORDS` has columns `Z_1ASSETATTRIBUTES` and `Z_52KEYWORDS`), so the
    resolvers below find the table by pattern and pick columns by the word they
    contain rather than by the number.

  * text occurs in both Unicode normalisation forms in real libraries. macOS
    filesystems hand out NFD; most metadata writers emit NFC. Comparing raw
    bytes produced roughly 1,200 phantom "missing" keywords in one measurement.
    Normalise for comparison, keep the raw value.

See APPLE-PHOTOS-LIBRARY-INTERNALS.md sections 2.2, 2.3 and 16.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

# Seconds between the Unix epoch and Core Data's, which starts 2001-01-01 UTC.
APPLE_EPOCH = 978307200

# ZLATITUDE / ZLONGITUDE use this as "no location" instead of NULL. Filtering on
# IS NOT NULL alone therefore reports thousands of assets as located that are not.
NO_LOCATION = -180.0

# §4.1: originals are stored as <UUID>.<ext>, and companion files carry a _N
# suffix before the extension (<UUID>_3.mov is a Live Photo's video component).
# Anchoring on the UUID followed immediately by '.' keeps companions from being
# counted as separate assets.
UUID_RE = re.compile(
    r"^([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})\.", re.I
)

# The project root: this file's grandparent. Used for the default history
# location, so the tool keeps its state beside itself and never inside a library.
PROJECT_DIR = Path(__file__).resolve().parent.parent


def nfc(s):
    """Normalise text to NFC for comparison. Store and display the raw value."""
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s


def register_nfc(con: sqlite3.Connection) -> None:
    """Make nfc() callable inside SQL on this connection."""
    con.create_function("nfc", 1, nfc, deterministic=True)


def entity_numbers(con: sqlite3.Connection) -> dict[str, int]:
    """Z_PRIMARYKEY name -> Z_ENT, resolved now. Never cache these across runs."""
    return {r["Z_NAME"]: r["Z_ENT"]
            for r in con.execute("SELECT Z_ENT, Z_NAME FROM Z_PRIMARYKEY")}


def resolve_keyword_join(con: sqlite3.Connection) -> tuple[str, str, str]:
    """Find the Keyword join table and its columns for *this* store.

    Returns (table, attributes_column, keyword_column). Typically
    ('Z_1KEYWORDS', 'Z_1ASSETATTRIBUTES', 'Z_52KEYWORDS') — but the 52 moves
    between macOS releases and after a repair, so it is matched, not assumed.

    Note the ordering convention: the column for entity E's side is named
    Z_<E><INVERSE-RELATIONSHIP>, so the number in a column name does not
    necessarily match the entity that column points at. Match on the word.
    """
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        r"AND name LIKE 'Z\_%KEYWORDS' ESCAPE '\'"
    ).fetchall()
    if not rows:
        raise LookupError("no keyword join table found in this database")
    table = rows[0]["name"]
    cols = [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]
    kw = next((c for c in cols if "KEYWORD" in c.upper()), None)
    at = next((c for c in cols if c != kw), None)
    if not kw or not at:
        raise LookupError(f"could not resolve columns of {table}: {cols}")
    return table, at, kw


def resolve_album_join(con: sqlite3.Connection) -> tuple[str, str, str]:
    """Find the album-membership join table. Returns (table, album_col, asset_col)."""
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        r"AND name LIKE 'Z\_%ASSETS' ESCAPE '\'"
    ).fetchall()
    if not rows:
        raise LookupError("no album membership join table found")
    want = f"Z_{entity_numbers(con).get('Album')}ASSETS"
    table = next((r["name"] for r in rows if r["name"] == want), rows[0]["name"])
    cols = [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]
    alb = next((c for c in cols if "ALBUM" in c.upper()), None)
    ast = next((c for c in cols if "ASSET" in c.upper() and c != alb), None)
    if not alb or not ast:
        raise LookupError(f"could not resolve columns of {table}: {cols}")
    return table, alb, ast


def uuid_from_name(name: str) -> str | None:
    """The asset UUID a filename in originals/ belongs to, or None."""
    m = UUID_RE.match(name)
    return m.group(1).upper() if m else None


def eprint(*a) -> None:
    print(*a, file=sys.stderr, flush=True)
