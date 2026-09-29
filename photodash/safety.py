"""Validate writable destinations before creating dashboard state."""

from pathlib import Path


def outside_library(path: Path, library: Path | None = None) -> Path:
    """Resolve aliases and reject output anywhere within a Photos bundle."""
    path = Path(path).expanduser().absolute()
    resolved = path.resolve()
    for candidate in (path, resolved):
        if any(p.name.lower().endswith('.photoslibrary')
               for p in (candidate, *candidate.parents)):
            raise ValueError('Dashboard output must be outside all Photos libraries')
    if library is not None and resolved.is_relative_to(Path(library).resolve()):
        raise ValueError('Dashboard output must be outside the selected library')
    return resolved
