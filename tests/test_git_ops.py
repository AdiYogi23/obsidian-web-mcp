"""Tests for the vault_git_commit tool."""

import json
import subprocess

import pytest

from obsidian_vault_mcp.tools.git_ops import vault_git_commit


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def git_vault_dir(vault_dir, tmp_path):
    """vault_dir, git-initialized, with an AdiYogi23-owned bare remote."""
    _git(["init", "-b", "main"], cwd=vault_dir)
    _git(["config", "user.email", "test@example.com"], cwd=vault_dir)
    _git(["config", "user.name", "Test"], cwd=vault_dir)
    _git(["add", "test-note.md"], cwd=vault_dir)
    _git(["commit", "-m", "initial"], cwd=vault_dir)

    bare = tmp_path / "AdiYogi23-bare.git"
    _git(["init", "--bare", "-b", "main", str(bare)], cwd=tmp_path)
    _git(["remote", "add", "origin", str(bare)], cwd=vault_dir)
    _git(["push", "origin", "main"], cwd=vault_dir)

    return vault_dir, bare


@pytest.fixture
def git_vault_dir_wrong_owner(vault_dir):
    """vault_dir, git-initialized, with a non-AdiYogi23 remote (never pushed to)."""
    _git(["init", "-b", "main"], cwd=vault_dir)
    _git(["config", "user.email", "test@example.com"], cwd=vault_dir)
    _git(["config", "user.name", "Test"], cwd=vault_dir)
    _git(["add", "test-note.md"], cwd=vault_dir)
    _git(["commit", "-m", "initial"], cwd=vault_dir)
    _git(["remote", "add", "origin", "https://github.com/jimprosser/brain.git"], cwd=vault_dir)
    return vault_dir


def _head(cwd):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_rejects_empty_paths(git_vault_dir):
    vault, _ = git_vault_dir
    before = _head(vault)
    result = json.loads(vault_git_commit([], "msg", push=False))
    assert result["error"]
    assert result["committed"] is False
    assert _head(vault) == before


@pytest.mark.parametrize("token", [".", "-a", "-A", "--all"])
def test_rejects_add_all_tokens(git_vault_dir, token):
    vault, _ = git_vault_dir
    before = _head(vault)
    result = json.loads(vault_git_commit([token], "msg", push=False))
    assert result["error"]
    assert result["committed"] is False
    assert _head(vault) == before


def test_rejects_path_outside_vault(git_vault_dir):
    vault, _ = git_vault_dir
    before = _head(vault)
    result = json.loads(vault_git_commit(["../outside.md"], "msg", push=False))
    assert result["error"]
    assert result["committed"] is False
    assert _head(vault) == before


def test_rejects_empty_message(git_vault_dir):
    vault, _ = git_vault_dir
    before = _head(vault)
    result = json.loads(vault_git_commit(["test-note.md"], "", push=False))
    assert result["error"]
    assert result["committed"] is False
    assert _head(vault) == before


def test_rejects_whitespace_only_message(git_vault_dir):
    vault, _ = git_vault_dir
    before = _head(vault)
    result = json.loads(vault_git_commit(["test-note.md"], "   ", push=False))
    assert result["error"]
    assert result["committed"] is False
    assert _head(vault) == before


def test_rejects_nothing_staged(git_vault_dir):
    """A path with no actual changes must not produce an empty commit."""
    vault, _ = git_vault_dir
    before = _head(vault)
    result = json.loads(vault_git_commit(["test-note.md"], "msg", push=False))
    assert result["error"]
    assert result["committed"] is False
    assert _head(vault) == before


def test_rejects_wrong_remote_owner(git_vault_dir_wrong_owner):
    vault = git_vault_dir_wrong_owner
    before = _head(vault)
    (vault / "test-note.md").write_text("changed content\n")
    result = json.loads(vault_git_commit(["test-note.md"], "msg", push=False))
    assert result["error"]
    assert "AdiYogi23" in result["error"]
    assert result["committed"] is False
    # nothing should have been staged/committed either
    assert _head(vault) == before
    status = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=vault, capture_output=True, text=True
    ).stdout.strip()
    assert status == ""


def test_commit_without_push(git_vault_dir):
    vault, bare = git_vault_dir
    before = _head(vault)
    (vault / "new-file.md").write_text("hello\n")
    result = json.loads(vault_git_commit(["new-file.md"], "add new-file", push=False))
    assert result["committed"] is True
    assert result["pushed"] is False
    assert result["head"] != before
    assert result["head"] == _head(vault)
    # bare "origin" must NOT have received it
    bare_head = subprocess.run(
        ["git", "rev-parse", "main"], cwd=bare, capture_output=True, text=True
    ).stdout.strip()
    assert bare_head == before


def test_commit_with_push(git_vault_dir):
    vault, bare = git_vault_dir
    before = _head(vault)
    (vault / "another-file.md").write_text("hello again\n")
    result = json.loads(vault_git_commit(["another-file.md"], "add another-file", push=True))
    assert result["committed"] is True
    assert result["pushed"] is True
    assert result["head"] != before
    bare_head = subprocess.run(
        ["git", "rev-parse", "main"], cwd=bare, capture_output=True, text=True
    ).stdout.strip()
    assert bare_head == result["head"]


def test_commit_named_subset_leaves_other_changes_unstaged(git_vault_dir):
    """Only the explicitly named path is committed, even if other files changed too."""
    vault, _ = git_vault_dir
    (vault / "new-file.md").write_text("hello\n")
    (vault / "untouched.md").write_text("should stay untracked\n")
    result = json.loads(vault_git_commit(["new-file.md"], "add new-file only", push=False))
    assert result["committed"] is True
    status = subprocess.run(
        ["git", "status", "--short"], cwd=vault, capture_output=True, text=True
    ).stdout
    assert "untouched.md" in status
    assert "new-file.md" not in status
