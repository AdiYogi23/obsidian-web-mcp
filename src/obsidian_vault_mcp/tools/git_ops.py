"""Git commit/push tool for the Obsidian vault MCP server."""

import logging
import subprocess

from .. import config
from ..serialization import dumps
from ..vault import resolve_vault_path

logger = logging.getLogger(__name__)

# Literal path tokens that would otherwise smuggle a wildcard/add-all flag
# into `git add` -- rejected outright rather than passed through, because
# there is no implicit "everything" for this tool.
_FORBIDDEN_PATH_TOKENS = {".", "-a", "-A", "--all"}

_GIT_TIMEOUT = 60
_REQUIRED_REMOTE_OWNER = "AdiYogi23"


def _run_git(args: list[str], cwd) -> subprocess.CompletedProcess:
    """Run one git subcommand as an argv list (never shell=True).

    -c core.quotepath=false so Cyrillic paths in stdout/stderr come back
    readable instead of octal-escaped (same convention used throughout this
    session's manual git commands against this vault).
    """
    cmd = ["git", "-c", "core.quotepath=false", *args]
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_GIT_TIMEOUT,
    )


def vault_git_commit(paths: list[str], message: str, push: bool = True) -> str:
    """Stage named vault paths, commit, and (optionally) push to origin main.

    Every guard below returns an error and performs no git operation at all --
    there is no partial-execution path. `git add`/`commit`/`push` run as argv
    lists with `--` before user paths, so a path that happens to look like a
    flag is still treated as a literal pathspec, not parsed as an option.
    """
    if not paths:
        return dumps({
            "error": "paths must not be empty -- there is no implicit \"everything\"",
            "committed": False,
            "pushed": False,
        })

    for p in paths:
        if p in _FORBIDDEN_PATH_TOKENS:
            return dumps({
                "error": f"Refusing path token {p!r} -- wildcard/add-all paths are not allowed; list paths by name",
                "committed": False,
                "pushed": False,
            })

    if not message or not message.strip():
        return dumps({
            "error": "message must not be empty or whitespace-only",
            "committed": False,
            "pushed": False,
        })

    for p in paths:
        try:
            resolve_vault_path(p)
        except ValueError as e:
            return dumps({
                "error": f"Path {p!r} rejected: {e}",
                "committed": False,
                "pushed": False,
            })

    vault_root = config.VAULT_PATH.resolve()
    log: list[dict] = []

    try:
        remote = _run_git(["remote", "get-url", "origin"], cwd=vault_root)
    except subprocess.TimeoutExpired:
        return dumps({"error": "git remote get-url origin timed out", "committed": False, "pushed": False})

    remote_url = remote.stdout.strip()
    if remote.returncode != 0 or _REQUIRED_REMOTE_OWNER not in remote_url:
        return dumps({
            "error": f"origin remote must belong to {_REQUIRED_REMOTE_OWNER}; got {remote_url!r}",
            "committed": False,
            "pushed": False,
        })

    try:
        add_result = _run_git(["add", "--", *paths], cwd=vault_root)
    except subprocess.TimeoutExpired:
        return dumps({"error": "git add timed out", "committed": False, "pushed": False})

    log.append({"cmd": "add", "stdout": add_result.stdout, "stderr": add_result.stderr, "returncode": add_result.returncode})

    if add_result.returncode != 0:
        return dumps({"error": "git add failed", "committed": False, "pushed": False, "log": log})

    try:
        staged = _run_git(["diff", "--cached", "--name-only"], cwd=vault_root)
    except subprocess.TimeoutExpired:
        return dumps({"error": "git diff --cached timed out", "committed": False, "pushed": False, "log": log})

    if not staged.stdout.strip():
        return dumps({
            "error": "Nothing staged after git add -- refusing to create an empty commit",
            "committed": False,
            "pushed": False,
            "log": log,
        })

    try:
        commit_result = _run_git(["commit", "-m", message], cwd=vault_root)
    except subprocess.TimeoutExpired:
        return dumps({"error": "git commit timed out", "committed": False, "pushed": False, "log": log})

    log.append({"cmd": "commit", "stdout": commit_result.stdout, "stderr": commit_result.stderr, "returncode": commit_result.returncode})

    if commit_result.returncode != 0:
        return dumps({"error": "git commit failed", "committed": False, "pushed": False, "log": log})

    pushed = False
    if push:
        try:
            push_result = _run_git(["push", "origin", "main"], cwd=vault_root)
        except subprocess.TimeoutExpired:
            return dumps({"error": "git push timed out", "committed": True, "pushed": False, "log": log})

        log.append({"cmd": "push", "stdout": push_result.stdout, "stderr": push_result.stderr, "returncode": push_result.returncode})

        if push_result.returncode != 0:
            return dumps({"error": "git push failed", "committed": True, "pushed": False, "log": log})
        pushed = True

    head_result = _run_git(["rev-parse", "HEAD"], cwd=vault_root)
    log.append({"cmd": "rev-parse HEAD", "stdout": head_result.stdout, "stderr": head_result.stderr, "returncode": head_result.returncode})

    return dumps({
        "committed": True,
        "pushed": pushed,
        "head": head_result.stdout.strip(),
        "log": log,
    })
