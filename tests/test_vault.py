"""Tests for vault.py -- path resolution, file operations, and safety checks."""

import pytest
from pathlib import Path

from obsidian_vault_mcp.vault import (
    resolve_vault_path,
    read_file,
    write_file_atomic,
    delete_path,
    move_path,
    list_directory,
)


def test_resolve_valid_path(vault_dir):
    """Normal relative path resolves correctly."""
    result = resolve_vault_path("test-note.md")
    assert result.exists()
    assert result.name == "test-note.md"


def test_resolve_dotdot_rejected(vault_dir):
    """Path with .. that escapes vault is rejected."""
    with pytest.raises(ValueError):
        resolve_vault_path("../../etc/passwd")


def test_resolve_dotfile_rejected(vault_dir):
    """Path starting with .obsidian is rejected."""
    with pytest.raises(ValueError, match="hidden"):
        resolve_vault_path(".obsidian/config.json")


def test_resolve_null_byte_rejected(vault_dir):
    """Path with null byte is rejected."""
    with pytest.raises(ValueError, match="null"):
        resolve_vault_path("test\x00note.md")


# --- .claude/skills read-only exception (2026-08-06) ---


def test_resolve_claude_skills_blocked_by_default(vault_dir):
    """Without the flag, .claude/skills is rejected exactly like any dotfile."""
    with pytest.raises(ValueError, match="hidden"):
        resolve_vault_path(".claude/skills/sample-skill/SKILL.md")


def test_resolve_claude_skills_allowed_with_flag(vault_dir):
    """With allow_claude_skills=True, a path under .claude/skills resolves."""
    result = resolve_vault_path(
        ".claude/skills/sample-skill/SKILL.md", allow_claude_skills=True
    )
    assert result.exists()
    assert result.name == "SKILL.md"


def test_resolve_claude_skills_directory_itself_allowed_with_flag(vault_dir):
    """The bare .claude/skills directory (no trailing path) also resolves."""
    result = resolve_vault_path(".claude/skills", allow_claude_skills=True)
    assert result.is_dir()


def test_resolve_claude_settings_rejected_even_with_flag(vault_dir):
    """.claude/settings.local.json stays blocked -- the exception is skills/ only."""
    with pytest.raises(ValueError, match="hidden"):
        resolve_vault_path(".claude/settings.local.json", allow_claude_skills=True)


def test_resolve_claude_bare_rejected_even_with_flag(vault_dir):
    """Bare .claude (not under skills/) stays blocked."""
    with pytest.raises(ValueError, match="hidden"):
        resolve_vault_path(".claude", allow_claude_skills=True)


def test_resolve_git_rejected_even_with_flag(vault_dir):
    """.git stays blocked regardless of the flag -- unrelated to .claude/skills."""
    with pytest.raises(ValueError, match="hidden"):
        resolve_vault_path(".git/config", allow_claude_skills=True)


def test_resolve_claude_skills_traversal_still_rejected_with_flag(vault_dir):
    """A .. segment after the exempted prefix is still caught by the dot check."""
    with pytest.raises(ValueError):
        resolve_vault_path(
            ".claude/skills/../../.git/config", allow_claude_skills=True
        )


def test_resolve_dotdot_rejected_even_with_flag(vault_dir):
    """Plain traversal outside the vault stays rejected regardless of the flag."""
    with pytest.raises(ValueError):
        resolve_vault_path("../../etc/passwd", allow_claude_skills=True)


def test_list_directory_claude_skills_allowed_with_flag(vault_dir):
    """list_directory can enumerate .claude/skills when the flag is set."""
    items = list_directory(".claude/skills", depth=2, allow_claude_skills=True)
    names = [item["name"] for item in items]
    assert "sample-skill" in names
    assert "SKILL.md" in names


def test_read_file_claude_skills_allowed_with_flag(vault_dir):
    """read_file can read a skill file when the flag is set."""
    content, metadata = read_file(
        ".claude/skills/sample-skill/SKILL.md", allow_claude_skills=True
    )
    assert "sample-skill" in content
    assert metadata["size"] > 0


def test_read_file(vault_dir):
    """Read a file, verify content and metadata."""
    content, metadata = read_file("test-note.md")
    assert "test note" in content
    assert "size" in metadata
    assert "modified" in metadata
    assert "created" in metadata
    assert metadata["size"] > 0


def test_read_missing_file(vault_dir):
    """Reading a nonexistent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        read_file("nonexistent.md")


def test_write_atomic_new_file(vault_dir):
    """Write a new file and verify it exists."""
    is_new, size = write_file_atomic("new-file.md", "# Hello\n\nNew content.")
    assert is_new is True
    assert size > 0
    assert (vault_dir / "new-file.md").exists()
    assert (vault_dir / "new-file.md").read_text() == "# Hello\n\nNew content."


def test_write_atomic_overwrite(vault_dir):
    """Overwrite an existing file."""
    is_new, _ = write_file_atomic("test-note.md", "Overwritten content.")
    assert is_new is False
    assert (vault_dir / "test-note.md").read_text() == "Overwritten content."


def test_write_atomic_creates_dirs(vault_dir):
    """Write to a nonexistent directory with create_dirs=True."""
    is_new, _ = write_file_atomic("new-dir/deep/file.md", "Content", create_dirs=True)
    assert is_new is True
    assert (vault_dir / "new-dir" / "deep" / "file.md").exists()


def test_write_respects_size_limit(vault_dir):
    """Content exceeding MAX_CONTENT_SIZE is rejected."""
    from obsidian_vault_mcp.config import MAX_CONTENT_SIZE
    big_content = "x" * (MAX_CONTENT_SIZE + 1)
    with pytest.raises(ValueError, match="size"):
        write_file_atomic("big-file.md", big_content)


def test_delete_moves_to_trash(vault_dir):
    """Delete moves file to .trash/, not hard delete."""
    write_file_atomic("to-delete.md", "Delete me.")
    assert (vault_dir / "to-delete.md").exists()

    deleted = delete_path("to-delete.md")
    assert deleted is True
    assert not (vault_dir / "to-delete.md").exists()
    assert (vault_dir / ".trash" / "to-delete.md").exists()


def test_list_excludes_dotdirs(vault_dir):
    """Listing excludes .obsidian directory."""
    items = list_directory("", depth=1, include_files=True, include_dirs=True, pattern=None)
    names = [item["name"] for item in items]
    assert ".obsidian" not in names
    assert ".trash" not in names
    assert "test-note.md" in names


def test_move_file(vault_dir):
    """Move a file and verify old path is gone, new path exists."""
    write_file_atomic("source.md", "Move me.")
    moved = move_path("source.md", "destination.md")
    assert moved is True
    assert not (vault_dir / "source.md").exists()
    assert (vault_dir / "destination.md").exists()
    assert (vault_dir / "destination.md").read_text() == "Move me."
