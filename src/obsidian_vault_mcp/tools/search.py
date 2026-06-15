"""Search tools for the Obsidian vault MCP server."""

import json
import logging
import shutil
import subprocess
from pathlib import Path

import frontmatter

from .. import config
from ..vault import resolve_vault_path
from ..serialization import json_default

logger = logging.getLogger(__name__)


def _search_ripgrep(
    query: str,
    search_path: Path,
    file_pattern: str,
    max_results: int,
    context_lines: int,
) -> list[dict]:
    """Search using ripgrep for performance."""
    cmd = [
        "rg",
        "--json",
        f"--max-count={max_results}",
        f"--glob={file_pattern}",
        "-i",
        f"--context={context_lines}",
        query,
        str(search_path),
    ]

    for excluded in config.EXCLUDED_DIRS:
        cmd.insert(-2, f"--glob=!{excluded}/")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    matches = []
    current_match = None

    for line in result.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if data.get("type") == "match":
            match_data = data["data"]
            file_path = match_data["path"]["text"]
            try:
                rel_path = str(Path(file_path).relative_to(config.VAULT_PATH))
            except ValueError:
                continue

            line_number = match_data["line_number"]
            line_text = match_data["lines"]["text"].rstrip("\n")

            matches.append({
                "path": rel_path,
                "line_number": line_number,
                "match_context": line_text,
            })

            if len(matches) >= max_results:
                break

    return matches


def _search_python(
    query: str,
    search_path: Path,
    file_pattern: str,
    max_results: int,
    context_lines: int,
) -> list[dict]:
    """Fallback Python-based search."""
    import fnmatch

    query_lower = query.lower()
    matches = []

    for file_path in search_path.rglob("*"):
        if not file_path.is_file():
            continue

        if any(part in config.EXCLUDED_DIRS for part in file_path.parts):
            continue

        if not fnmatch.fnmatch(file_path.name, file_pattern):
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue

        lines = content.splitlines()
        for i, line in enumerate(lines):
            if query_lower in line.lower():
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)
                context = "\n".join(lines[start:end])

                try:
                    rel_path = str(file_path.relative_to(config.VAULT_PATH))
                except ValueError:
                    continue

                matches.append({
                    "path": rel_path,
                    "line_number": i + 1,
                    "match_context": context,
                })

                if len(matches) >= max_results:
                    return matches

    return matches


def _get_frontmatter_excerpt(file_path: Path, max_keys: int = 3) -> dict | None:
    """Read frontmatter from a file, returning first N key-value pairs."""
    try:
        content = file_path.read_text(encoding="utf-8")
        post = frontmatter.loads(content)
        if not post.metadata:
            return None
        keys = list(post.metadata.keys())[:max_keys]
        return {k: post.metadata[k] for k in keys}
    except Exception:
        return None


def vault_search(
    query: str,
    path_prefix: str | None = None,
    file_pattern: str = "*.md",
    max_results: int = 20,
    context_lines: int = 2,
) -> str:
    """Search for text across vault files."""
    try:
        if path_prefix:
            search_path = resolve_vault_path(path_prefix)
        else:
            search_path = config.VAULT_PATH

        if not search_path.is_dir():
            return json.dumps({"error": f"Search path is not a directory: {path_prefix}"})

        if shutil.which("rg"):
            matches = _search_ripgrep(query, search_path, file_pattern, max_results, context_lines)
        else:
            matches = _search_python(query, search_path, file_pattern, max_results, context_lines)

        for match in matches:
            file_full_path = config.VAULT_PATH / match["path"]
            match["frontmatter_excerpt"] = _get_frontmatter_excerpt(file_full_path)

        truncated = len(matches) >= max_results

        return json.dumps({
            "results": matches,
            "total_matches": len(matches),
            "truncated": truncated,
        }, default=json_default)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"vault_search error: {e}")
        return json.dumps({"error": str(e)})


def _keyword_ranked_paths(
    query: str, search_path: Path, max_results: int
) -> list[str]:
    """Run the existing keyword search and return ordered, de-duplicated paths."""
    if shutil.which("rg"):
        matches = _search_ripgrep(query, search_path, "*.md", max_results, 0)
    else:
        matches = _search_python(query, search_path, "*.md", max_results, 0)
    ordered: list[str] = []
    seen: set[str] = set()
    for m in matches:
        p = m["path"]
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def vault_semantic_search(
    query: str,
    path_prefix: str | None = None,
    max_results: int = 10,
) -> str:
    """Search the vault by MEANING using the vector index (handles synonyms, BG/EN)."""
    try:
        from ..server import semantic_index
    except Exception:
        return json.dumps({"error": "semantic index not available"})

    if not getattr(semantic_index, "ready", False):
        return json.dumps({
            "results": [],
            "status": semantic_index.status() if semantic_index else None,
            "note": "Semantic index still building or disabled; use vault_search meanwhile.",
        })

    results = semantic_index.search(query, max_results=max_results, path_prefix=path_prefix)
    for r in results:
        r["frontmatter_excerpt"] = _get_frontmatter_excerpt(config.VAULT_PATH / r["path"])
    return json.dumps({"results": results, "total_matches": len(results)}, default=json_default)


def vault_hybrid_search(
    query: str,
    path_prefix: str | None = None,
    max_results: int = 10,
) -> str:
    """Best search: fuse keyword + semantic with Reciprocal Rank Fusion (RRF).

    Falls back to plain keyword search if the semantic index is not ready.
    """
    try:
        search_path = resolve_vault_path(path_prefix) if path_prefix else config.VAULT_PATH
        if not search_path.is_dir():
            return json.dumps({"error": f"Search path is not a directory: {path_prefix}"})

        # Keyword ranking (always available)
        kw_paths = _keyword_ranked_paths(query, search_path, max_results=30)

        # Semantic ranking (if ready)
        sem_paths: list[str] = []
        try:
            from ..server import semantic_index
            if getattr(semantic_index, "ready", False):
                sem_hits = semantic_index.search(query, max_results=30, path_prefix=path_prefix)
                seen: set[str] = set()
                for h in sem_hits:
                    if h["path"] not in seen:
                        seen.add(h["path"])
                        sem_paths.append(h["path"])
        except Exception:
            pass

        if not sem_paths:
            # graceful fallback: keyword only
            mode = "keyword-only (semantic not ready)"
            fused = kw_paths
        else:
            mode = "hybrid (keyword + semantic, RRF)"
            scores: dict[str, float] = {}
            for ranked in (kw_paths, sem_paths):
                for rank, p in enumerate(ranked):
                    scores[p] = scores.get(p, 0.0) + 1.0 / (config.RRF_K + rank + 1)
            fused = [p for p, _ in sorted(scores.items(), key=lambda x: -x[1])]

        results = []
        for p in fused[:max_results]:
            full = config.VAULT_PATH / p
            excerpt = ""
            try:
                txt = full.read_text(encoding="utf-8")
                post = frontmatter.loads(txt)
                excerpt = " ".join(post.content.split())[:200]
            except Exception:
                pass
            results.append({
                "path": p,
                "snippet": excerpt,
                "frontmatter_excerpt": _get_frontmatter_excerpt(full),
            })

        return json.dumps({
            "mode": mode,
            "results": results,
            "total_matches": len(results),
        }, default=json_default)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"vault_hybrid_search error: {e}")
        return json.dumps({"error": str(e)})


def vault_search_frontmatter(
    field: str,
    value: str = "",
    match_type: str = "exact",
    path_prefix: str | None = None,
    max_results: int = 20,
) -> str:
    """Search vault files by frontmatter field values using the in-memory index."""
    from ..server import frontmatter_index

    try:
        results = frontmatter_index.search_by_field(
            field=field,
            value=value,
            match_type=match_type,
            path_prefix=path_prefix,
        )

        formatted = []
        for item in results[:max_results]:
            path = item["path"]
            fm = item["frontmatter"]
            title = fm.get("title", Path(path).stem)
            formatted.append({
                "path": path,
                "frontmatter": fm,
                "title": title,
            })

        truncated = len(results) > max_results

        return json.dumps({
            "results": formatted,
            "total": len(formatted),
            "truncated": truncated,
        }, default=json_default)
    except Exception as e:
        logger.error(f"vault_search_frontmatter error: {e}")
        return json.dumps({"error": str(e)})
