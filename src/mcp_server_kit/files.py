"""Collision-safe file staging, e.g. a directory shared by several MCP servers.

Servers that hand files to each other through a shared directory (one writes
`shared:<name>`, another reads it) need identical semantics, or a file staged
by one isn't where the other looks: bare filenames only, never overwrite, and
on a collision append -2, -3, ... before the extension. Creation uses
O_CREAT|O_EXCL, so parallel writers in different processes can't clobber each
other. Every transfer reports the sha256 of the bytes, so the caller can verify
a handoff in-band.

`list_files`, `purge_one` and `purge_older` are the matching housekeeping tools'
bodies; they return the human-readable text a tool hands back. They only ever
touch regular files, never subdirectories.
"""

import hashlib
import os
import time
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


def _age_hours(st: os.stat_result) -> float:
    return (time.time() - st.st_mtime) / 3600


def list_files(root: Path, filter: str = "", *, scheme: str = "shared", label: str = "shared mount",
               recursive: bool = False) -> str:
    """List the regular files under `root`: size, age, sha256 and the `<scheme>:<name>` reference.
    `filter` is a case-insensitive substring of the filename. `recursive` includes
    subdirectories (names are then relative paths)."""
    root = Path(root)
    if not root.exists():
        return f"The {label} ({root}) does not exist."
    files = sorted(p for p in (root.rglob("*") if recursive else root.iterdir()) if p.is_file())
    if filter:
        files = [p for p in files if filter.lower() in p.name.lower()]
    if not files:
        return f"The {label} is empty" + (f" (filter {filter!r})." if filter else ".")
    lines = [f"{len(files)} file(s) in the {label} ({root}):"]
    for p in files:
        rel = p.relative_to(root).as_posix()
        data = p.read_bytes()
        lines.append(f"  • {rel}  ({len(data)} bytes, age {_age_hours(p.stat()):.1f}h, "
                     f"sha256 {sha256(data)})  [{scheme}:{rel}]")
    return "\n".join(lines)


def purge_one(root: Path, filename: str, *, label: str = "shared mount") -> str:
    """Delete one file under `root` by bare filename (see require_bare)."""
    require_bare(filename)
    target = Path(root) / filename
    if not target.is_file():
        return f"No such file in the {label}: {filename}"
    size = target.stat().st_size
    target.unlink()
    return f"Deleted {filename} ({size} bytes) from {root}."


def purge_older(root: Path, max_age_hours: float = 24.0, dry_run: bool = False) -> str:
    """Delete the top-level files under `root` whose mtime is older than `max_age_hours`.
    `dry_run` lists them without deleting."""
    if max_age_hours < 0:
        raise ValueError("max_age_hours must be >= 0")
    root = Path(root)
    if not root.exists():
        return f"{root} does not exist (nothing to purge)."
    cutoff = time.time() - max_age_hours * 3600
    victims = sorted(p for p in root.iterdir() if p.is_file() and p.stat().st_mtime < cutoff)
    if not victims:
        return f"No files older than {max_age_hours}h in {root}."
    verb = "Would delete" if dry_run else "Deleted"
    lines = [f"{verb} {len(victims)} file(s) older than {max_age_hours}h from {root}:"]
    for p in victims:
        st = p.stat()
        lines.append(f"  • {p.name}  ({st.st_size} bytes, age {_age_hours(st):.1f}h)")
        if not dry_run:
            p.unlink(missing_ok=True)
    return "\n".join(lines)
