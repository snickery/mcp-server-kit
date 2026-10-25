"""Collision-safe file staging, e.g. a directory shared by several MCP servers.

Servers that hand files to each other through a shared directory (one writes
`shared:<name>`, another reads it) need identical semantics, or a file staged
by one isn't where the other looks: bare filenames only, never overwrite, and
on a collision append -2, -3, ... before the extension. Creation uses
O_CREAT|O_EXCL, so parallel writers in different processes can't clobber each
other. Every transfer reports the sha256 of the bytes, so the caller can verify
a handoff in-band.
"""

import hashlib
import os
from pathlib import Path

MAX_SUFFIX = 1000


class NoFreeNameError(RuntimeError):
    """Every candidate name up to -MAX_SUFFIX already exists."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_bare(name: str, what: str = "filename") -> str:
    """`name`, if it is a bare filename (no directory part, not . or ..); else ValueError."""
    if not name or name != os.path.basename(name) or name in (".", ".."):
        raise ValueError(f"Invalid {what} {name!r} — must be a bare filename.")
    return name


def exclusive_open(root: Path, filename: str, mode: int = 0o644) -> tuple[Path, int]:
    """Create a new file under `root` and return (path, fd) open for writing.

    Tries `filename`, then `<stem>-2<ext>`, `<stem>-3<ext>`, ... `filename` must already
    be bare (see require_bare). For streaming writers; `atomic_create` writes bytes.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    stem, ext = os.path.splitext(filename)
    for i in range(MAX_SUFFIX + 1):
        candidate = root / (filename if i == 0 else f"{stem}-{i + 1}{ext}")
        try:
            return candidate, os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            continue
    raise NoFreeNameError(f"No free filename for {filename!r} in {root} (tried -{MAX_SUFFIX}).")


def atomic_create(root: Path, filename: str, data: bytes) -> Path:
    """Write `data` to a new file under `root` (see exclusive_open) and return its path.
    A failed write removes the partial file."""
    path, fd = exclusive_open(root, filename)
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path
