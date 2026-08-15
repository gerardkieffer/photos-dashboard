#!/usr/bin/env python3
"""Check that every documentation pointer the app can show actually resolves.

An error message that cites a heading which no longer exists is worse than one
that cites nothing: it sends the reader looking for something that is not there.
This runs in a second and needs no Photos library, so there is no reason for a
pointer ever to be stale.

It checks three things:

  1. every anchor in photodash/docs.py names a real heading in DOCUMENTATION.md,
     including the parent heading of a "Parent > Child" pointer;
  2. every internals section cited from docs.py exists in
     APPLE-PHOTOS-LIBRARY-INTERNALS.md;
  3. every §N.N cited anywhere in the code, the README or the documentation
     exists in that same document.

    ./test_docs.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from photodash import docs  # noqa: E402

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def headings(text: str) -> set[str]:
    return {re.sub(r"^#+\s*", "", line).strip()
            for line in text.splitlines() if line.startswith("#")}


def has_section(internals: str, number: str) -> bool:
    return bool(re.search(rf"^#+\s*{re.escape(number)}\.?\s", internals, re.M))


def main() -> int:
    doc_path = HERE / docs.DOCUMENTATION
    int_path = HERE / docs.INTERNALS
    problems: list[str] = []

    for p in (doc_path, int_path):
        if not p.exists():
            print(f"{RED}missing document: {p.name}{OFF}")
            return 1

    doc, internals = doc_path.read_text(), int_path.read_text()
    heads = headings(doc)

    for key, (anchor, section, summary) in docs.TOPICS.items():
        parts = [p.strip() for p in anchor.split(">")]
        for part in parts:
            if part not in heads:
                problems.append(f"docs.TOPICS[{key!r}]: no heading {part!r} "
                                f"in {docs.DOCUMENTATION}")
        if not summary.strip():
            problems.append(f"docs.TOPICS[{key!r}]: empty summary")
        for s in re.findall(r"§([\d.]+)", section or ""):
            if not has_section(internals, s.rstrip(".")):
                problems.append(f"docs.TOPICS[{key!r}]: no §{s} in {docs.INTERNALS}")

    sources = sorted(HERE.rglob("*.py")) + sorted((HERE / "photodash/static").glob("*.js"))
    sources += [doc_path, HERE / "README.md"]
    for p in sources:
        if p.name == int_path.name:
            continue
        for s in sorted(set(re.findall(r"§([\d.]+)", p.read_text()))):
            if not has_section(internals, s.rstrip(".")):
                problems.append(f"{p.relative_to(HERE)}: cites §{s}, which does "
                                f"not exist in {docs.INTERNALS}")

    print(f"{len(docs.TOPICS)} pointers, {len(sources)} files checked")
    if problems:
        for p in problems:
            print(f"  {RED}FAIL{OFF} {p}")
        print(f"\n{RED}{len(problems)} broken reference(s).{OFF}")
        return 1
    print(f"{GREEN}All documentation references resolve.{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
