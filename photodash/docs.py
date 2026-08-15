"""Where to send someone when something goes wrong.

Every error, warning and banner the dashboard can show points at a specific
place in the documentation rather than leaving the reader to search. Keeping
those pointers in one table means the anchors cannot drift apart from the
messages that cite them: if a heading in DOCUMENTATION.md is renamed, it is
renamed here too, and every message that referenced it follows.

Two documents are cited:

  DOCUMENTATION.md                    how to set up, run and troubleshoot this app
  APPLE-PHOTOS-LIBRARY-INTERNALS.md   the measured technical reference the
                                      collectors are built from, whose section
                                      numbers the code comments also use

`cli()` renders a pointer for the terminal, `web()` for the browser.
"""

from __future__ import annotations

DOCUMENTATION = "DOCUMENTATION.md"
INTERNALS = "APPLE-PHOTOS-LIBRARY-INTERNALS.md"

# key -> (documentation anchor text, internals section, one-line summary)
TOPICS: dict[str, tuple[str, str | None, str]] = {
    "blocked": (
        "Troubleshooting > A library is listed as BLOCKED", "§17",
        "macOS blocks Photos libraries in protected locations unless the app "
        "running has Full Disk Access.",
    ),
    "no-library": (
        "Troubleshooting > No library was found", None,
        "Nothing that looks like a readable Photos library was discovered.",
    ),
    "choosing": (
        "Choosing which library to watch", None,
        "How the library is picked, and how to override it.",
    ),
    "unavailable": (
        "Troubleshooting > The library became unavailable", None,
        "The store could not be opened — most often an external volume that "
        "went away or spun down.",
    ),
    "pk-fault": (
        "Troubleshooting > Save integrity reports a fault", "§3.2 and §20.2",
        "A primary-key counter is behind its table, which stops Photos saving "
        "anything anywhere in the library.",
    ),
    "unattended": (
        "Troubleshooting > Nothing is progressing", "§2.5",
        "No daemon holds the library, so no background work will happen.",
    ),
    "throttled": (
        "Troubleshooting > Analysis never seems to run", "§2.5",
        "The scheduler refuses heavy analysis while the Mac is in use.",
    ),
    "log": (
        "Troubleshooting > Scheduler shows \"no decisions logged\"", "§20",
        "Reading the unified log can be slow, empty or unavailable.",
    ),
    "readonly": (
        "How the dashboard protects your library", None,
        "The three read-only guards, what they do and do not cover, and how to "
        "verify them yourself.",
    ),
    "snapshot": (
        "Snapshot mode", None,
        "Read a copy so that nothing inside the library bundle is written.",
    ),
    "history": (
        "Stored history", None,
        "Where samples are kept, and how retention works.",
    ),
    "derived": (
        "How progress is measured", None,
        "Why the completion rate is derived rather than counted.",
    ),
    "jobcodes": (
        "Reference > Job types and flags", None,
        "What is and is not established about ZJOBTYPE and ZJOBFLAGS.",
    ),
    "psi-excess": (
        "Reference > The search index", "§22",
        "The index can hold more rows than the library has assets.",
    ),
    "diagnostics": (
        "Troubleshooting > Collector errors", None,
        "What the collector diagnostics panel is telling you.",
    ),
}


def cli(key: str) -> str:
    """A pointer suitable for stderr, e.g. for an error the user just hit."""
    anchor, section, _ = TOPICS[key]
    ref = f"  See {DOCUMENTATION}: {anchor}"
    if section:
        ref += f"\n  Background: {INTERNALS} {section}"
    return ref


def web(key: str) -> dict:
    """The same pointer, as data the front end can render."""
    anchor, section, summary = TOPICS[key]
    return {"doc": DOCUMENTATION, "anchor": anchor,
            "internals": f"{INTERNALS} {section}" if section else None,
            "summary": summary}


def all_topics() -> dict[str, dict]:
    return {k: web(k) for k in TOPICS}
