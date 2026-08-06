"""Core filesystem operations for the Obsidian vault."""

import fnmatch
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config


# Narrow, read-only exception (2026-08-06): skills are canon and the planning
# layer needs to read them, but nothing else under a dot-prefixed path should
# open up. Only the leading `.claude/skills` components are ever exempted --
# see resolve_vault_path's allow_claude_skills parameter.
_CLAUDE_SKILLS_PREFIX = (".claude", "skills")


def resolve_vault_path(relative_path: str, allow_claude_skills: bool = False) -> Path:
    """Resolve a relative path against the vault root, with safety checks.

    Raises ValueError if the path escapes the vault, contains null bytes,
    or touches dotfile/dot-directory components.

    allow_claude_skills: when True, a path whose leading components are
    exactly `.claude/skills` (any separator; pathlib splits both) is exempt
    from the dot-prefix check for THOSE two components only. Every other
    dot-prefixed component -- including one nested inside a skill folder
    (`.claude/skills/x/.git/...`), a `..` traversal segment, or `.claude`
    used for anything other than `skills/` -- is still rejected below.
    Callers pass this only from the three read-only tools (vault_read,
    vault_batch_read via read_file, vault_list via list_directory); it must
    never reach a write path (write_file_atomic, move_path, delete_path take
    no such parameter at all).
    """
    if "\x00" in relative_path:
        raise ValueError("Path contains null bytes")

    # Check for dot-prefixed components (blocks .obsidian, .trash, dotfiles)
    parts = Path(relative_path).parts

    skip = 0
    if allow_claude_skills and parts[: len(_CLAUDE_SKILLS_PREFIX)] == _CLAUDE_SKILLS_PREFIX:
        skip = len(_CLAUDE_SKILLS_PREFIX)

    for part in parts[skip:]:
        if part.startswith("."):
            raise ValueError(
                f"Path component '{part}' starts with '.'; dotfiles and hidden directories are not allowed"
            )

    resolved = (config.VAULT_PATH / relative_path).resolve()
    vault_root = config.VAULT_PATH.resolve()

    if not str(resolved).startswith(str(vault_root) + os.sep) and resolved != vault_root:
        raise ValueError("Path resolves outside the vault root")

    return resolved


def _iso_timestamp(ts: float) -> str:
    """Convert a Unix timestamp to an ISO 8601 string in UTC."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def read_file(relative_path: str, allow_claude_skills: bool = False) -> tuple[str, dict]:
    """Read a file and return (content, metadata).

    Metadata keys: size (int), modified (ISO str), created (ISO str).
    allow_claude_skills: see resolve_vault_path.
    """
    path = resolve_vault_path(relative_path, allow_claude_skills=allow_claude_skills)

    if not path.is_file():
        raise FileNotFoundError(f"Not a file: {relative_path}")

    stat = path.stat()
    content = path.read_text(encoding="utf-8")

    metadata = {
        "size": stat.st_size,
        "modified": _iso_timestamp(stat.st_mtime),
        "created": _iso_timestamp(stat.st_birthtime if hasattr(stat, "st_birthtime") else stat.st_ctime),
    }

    return content, metadata


def write_file_atomic(
    relative_path: str, content: str, create_dirs: bool = True
) -> tuple[bool, int]:
    """Write content to a file atomically.

    Returns (is_new_file, bytes_written). Writes to a tempfile in the same
    directory then replaces the target, so readers never see a partial write.
    """
    encoded = content.encode("utf-8")
    if len(encoded) > config.MAX_CONTENT_SIZE:
        raise ValueError(
            f"Content size {len(encoded)} bytes exceeds limit of {config.MAX_CONTENT_SIZE} bytes"
        )

    path = resolve_vault_path(relative_path)
    is_new = not path.exists()

    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file in the same directory, then atomic-replace.
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up the temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return is_new, len(encoded)


def move_path(
    source: str, destination: str, create_dirs: bool = True
) -> bool:
    """Move a file or directory from source to destination.

    Both paths are relative to the vault root. Raises if the destination
    already exists.
    """
    src = resolve_vault_path(source)
    dst = resolve_vault_path(destination)

    if not src.exists():
        raise FileNotFoundError(f"Source does not exist: {source}")

    if dst.exists():
        raise FileExistsError(f"Destination already exists: {destination}")

    if create_dirs:
        dst.parent.mkdir(parents=True, exist_ok=True)

    shutil.move(str(src), str(dst))
    return True


def delete_path(relative_path: str) -> bool:
    """Soft-delete by moving the path into .trash/ at the vault root.

    Refuses to delete non-empty directories.
    """
    path = resolve_vault_path(relative_path)

    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {relative_path}")

    if path.is_dir() and any(path.iterdir()):
        raise ValueError(f"Refusing to delete non-empty directory: {relative_path}")

    trash_dir = config.VAULT_PATH.resolve() / ".trash"
    trash_dir.mkdir(exist_ok=True)

    dest = trash_dir / path.name

    # Avoid collisions in .trash by appending a timestamp
    if dest.exists():
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
        dest = trash_dir / f"{path.stem}_{ts}{path.suffix}"

    shutil.move(str(path), str(dest))
    return True


def list_directory(
    relative_path: str,
    depth: int = 1,
    include_files: bool = True,
    include_dirs: bool = True,
    pattern: str | None = None,
    allow_claude_skills: bool = False,
) -> list[dict]:
    """List directory contents recursively up to *depth* levels.

    Returns a list of dicts with keys: name, path (relative to vault),
    type ("file" or "dir"), size, modified.
    allow_claude_skills: see resolve_vault_path. Only applies to the root
    of this listing -- recursion below it walks the filesystem directly and
    never re-enters resolve_vault_path, so it is unaffected either way.
    """
    depth = min(depth, config.MAX_LIST_DEPTH)

    root = resolve_vault_path(relative_path, allow_claude_skills=allow_claude_skills)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {relative_path}")

    vault_root = config.VAULT_PATH.resolve()
    results: list[dict] = []

    def _walk(dir_path: Path, current_depth: int) -> None:
        if current_depth > depth:
            return

        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            return

        for entry in entries:
            # Skip excluded directories at every level
            if entry.name in config.EXCLUDED_DIRS:
                continue

            is_dir = entry.is_dir()

            if is_dir and not include_dirs:
                # Still recurse even if we're not listing dirs
                _walk(entry, current_depth + 1)
                continue

            if not is_dir and not include_files:
                continue

            # Apply glob pattern filter
            if pattern and not fnmatch.fnmatch(entry.name, pattern):
                if is_dir:
                    _walk(entry, current_depth + 1)
                continue

            try:
                stat = entry.stat()
            except OSError:
                continue

            rel = str(entry.relative_to(vault_root))

            results.append({
                "name": entry.name,
                "path": rel,
                "type": "dir" if is_dir else "file",
                "size": stat.st_size,
                "modified": _iso_timestamp(stat.st_mtime),
            })

            if is_dir:
                _walk(entry, current_depth + 1)

    _walk(root, 1)
    return results
