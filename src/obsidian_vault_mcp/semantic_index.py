"""Semantic (vector) index over vault .md files.

Mirrors FrontmatterIndex conventions: builds on startup, watches for changes
via watchdog (debounced), and persists to disk for fast restarts.

Design goals (safety first):
- Heavy deps (fastembed, numpy) are imported lazily. If they are missing or
  fail to load, the index sets ``enabled = False`` and the server keeps running
  with the existing keyword search untouched.
- The initial embedding build runs in a BACKGROUND thread so server startup is
  never blocked. Until the build finishes, ``search()`` reports "indexing".
- The persisted index is keyed by model name + per-file mtime, so restarts only
  re-embed files that actually changed.
"""

from __future__ import annotations

import logging
import os
import pickle
import threading
import time
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)


def _split_into_chunks(rel_path: str, body: str) -> list[dict]:
    """Split a markdown body into section-level chunks.

    Frontmatter is stripped by the caller. Sections are delimited by headings
    and blank lines. Each chunk carries its nearest heading for context.
    """
    filename = os.path.basename(rel_path)
    chunks: list[dict] = []
    cur_head = filename
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip()
        if len(text) >= 30:
            chunks.append({
                "path": rel_path,
                "heading": cur_head,
                "text": text,
            })

    for line in body.splitlines():
        if line.startswith("#"):
            flush()
            buf = []
            cur_head = line.lstrip("#").strip() or filename
        elif line.strip() == "":
            if buf:
                flush()
                buf = []
        else:
            buf.append(line)
    if buf:
        flush()
    return chunks


def _strip_frontmatter(raw: str) -> str:
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            return parts[2]
    return raw


class SemanticIndex:
    """Thread-safe vector index for semantic search over the vault."""

    def __init__(self) -> None:
        self.enabled = False          # True once deps load and a build succeeds
        self.ready = False            # True once the first build completes
        self.error: str | None = None
        self._model = None            # fastembed TextEmbedding
        self._np = None               # numpy module
        self._vectors = None          # np.ndarray (N, dim), L2-normalized
        self._meta: list[dict] = []   # parallel list of chunk dicts
        self._file_mtimes: dict[str, float] = {}
        self._lock = threading.Lock()
        self._observer = None
        self._debounce_timer: threading.Timer | None = None
        self._pending_paths: set[str] = set()
        self._cache_path = Path(
            os.environ.get(
                "SEMANTIC_INDEX_PATH",
                str(Path(__file__).resolve().parents[2] / ".semantic_index" / "index.pkl"),
            )
        )

    # ---- lifecycle ----

    def start(self) -> None:
        """Load deps + persisted index, then build/refresh in a background thread."""
        try:
            from fastembed import TextEmbedding  # noqa: F401
            import numpy as np
        except Exception as e:  # deps missing -> stay disabled, never crash
            self.error = f"semantic deps unavailable: {e}"
            logger.warning("Semantic index disabled: %s", self.error)
            return

        self._np = np
        # Build off the main thread so server startup is not blocked.
        threading.Thread(target=self._bootstrap, name="semantic-index-build", daemon=True).start()

    def _bootstrap(self) -> None:
        try:
            from fastembed import TextEmbedding
            t0 = time.monotonic()
            self._model = TextEmbedding(model_name=config.SEMANTIC_MODEL)
            self._load_cache()
            self._refresh_all()
            self.enabled = True
            self.ready = True
            self._save_cache()
            logger.info(
                "Semantic index ready: %d chunks in %.1fs (model=%s)",
                len(self._meta), time.monotonic() - t0, config.SEMANTIC_MODEL,
            )
            self._start_watching()
        except Exception as e:
            self.error = str(e)
            logger.warning("Semantic index build failed: %s", e)

    def stop(self) -> None:
        if self._debounce_timer is not None:
            self._debounce_timer.cancel()
            self._debounce_timer = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:
                pass
            self._observer = None

    # ---- public query ----

    @property
    def chunk_count(self) -> int:
        with self._lock:
            return len(self._meta)

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "chunks": self.chunk_count,
            "model": config.SEMANTIC_MODEL,
            "error": self.error,
        }

    def search(self, query: str, max_results: int = 20, path_prefix: str | None = None) -> list[dict]:
        """Return ranked chunks most similar in meaning to ``query``.

        Each result: {path, heading, score, snippet}. Returns [] if not ready.
        """
        if not self.ready or self._model is None or self._vectors is None:
            return []
        np = self._np
        qv = np.array(list(self._model.embed([query]))[0], dtype=np.float32)
        qv /= (np.linalg.norm(qv) + 1e-9)
        with self._lock:
            if len(self._meta) == 0:
                return []
            sims = self._vectors @ qv
            order = np.argsort(-sims)
            results: list[dict] = []
            for idx in order:
                meta = self._meta[int(idx)]
                if path_prefix and not meta["path"].startswith(path_prefix):
                    continue
                snippet = " ".join(meta["text"].split())[:200]
                results.append({
                    "path": meta["path"],
                    "heading": meta["heading"],
                    "score": round(float(sims[int(idx)]), 4),
                    "snippet": snippet,
                })
                if len(results) >= max_results:
                    break
            return results

    # ---- index construction ----

    def _iter_md_files(self):
        for md_path in config.VAULT_PATH.rglob("*.md"):
            if config.EXCLUDED_DIRS & set(md_path.relative_to(config.VAULT_PATH).parts):
                continue
            yield md_path

    def _embed_file(self, md_path: Path) -> list[dict]:
        rel = str(md_path.relative_to(config.VAULT_PATH))
        try:
            raw = md_path.read_text(encoding="utf-8")
        except Exception:
            return []
        body = _strip_frontmatter(raw)
        chunks = _split_into_chunks(rel, body)
        if not chunks:
            return []
        texts = [c["heading"] + ". " + c["text"][:500] for c in chunks]
        vecs = list(self._model.embed(texts))
        np = self._np
        for c, v in zip(chunks, vecs):
            arr = np.array(v, dtype=np.float32)
            arr /= (np.linalg.norm(arr) + 1e-9)
            c["_vec"] = arr
        return chunks

    def _rebuild_matrix(self, all_chunks: list[dict]) -> None:
        np = self._np
        if all_chunks:
            mat = np.vstack([c.pop("_vec") for c in all_chunks]).astype(np.float32)
        else:
            mat = np.zeros((0, 384), dtype=np.float32)
        with self._lock:
            self._meta = all_chunks
            self._vectors = mat

    def _refresh_all(self) -> None:
        """Embed every file whose mtime changed since the cached build."""
        kept: list[dict] = []        # chunks from unchanged files (reuse cached vecs)
        to_embed: list[Path] = []
        seen_files: set[str] = set()

        # bucket cached chunks by path for reuse
        cached_by_path: dict[str, list[dict]] = {}
        for c in self._meta:
            cached_by_path.setdefault(c["path"], []).append(c)

        for md_path in self._iter_md_files():
            rel = str(md_path.relative_to(config.VAULT_PATH))
            seen_files.add(rel)
            mtime = md_path.stat().st_mtime
            if self._file_mtimes.get(rel) == mtime and rel in cached_by_path:
                kept.extend(cached_by_path[rel])
            else:
                to_embed.append(md_path)
            self._file_mtimes[rel] = mtime

        # drop mtimes for deleted files
        for rel in list(self._file_mtimes.keys()):
            if rel not in seen_files:
                self._file_mtimes.pop(rel, None)

        new_chunks: list[dict] = []
        for md_path in to_embed:
            new_chunks.extend(self._embed_file(md_path))

        # cached chunks already carry "_vec"? No -> _meta vectors live in matrix.
        # Reconstruct vecs for kept chunks from the existing matrix.
        np = self._np
        if kept and self._vectors is not None and len(self._meta) == self._vectors.shape[0]:
            idx_of = {id(c): i for i, c in enumerate(self._meta)}
            for c in kept:
                i = idx_of.get(id(c))
                if i is not None:
                    c["_vec"] = self._vectors[i]
        kept = [c for c in kept if "_vec" in c]

        self._rebuild_matrix(kept + new_chunks)

    def _refresh_one(self, md_path: Path) -> None:
        """Re-embed a single changed/created/deleted file, update matrix."""
        rel = str(md_path.relative_to(config.VAULT_PATH))
        np = self._np
        with self._lock:
            meta = self._meta
            vectors = self._vectors
        # detach existing chunks for this path
        keep_meta: list[dict] = []
        keep_vecs = []
        for i, c in enumerate(meta):
            if c["path"] != rel:
                keep_meta.append(c)
                keep_vecs.append(vectors[i])

        new_chunks: list[dict] = []
        if md_path.exists():
            new_chunks = self._embed_file(md_path)
            self._file_mtimes[rel] = md_path.stat().st_mtime
        else:
            self._file_mtimes.pop(rel, None)

        combined_meta = keep_meta + [{k: v for k, v in c.items() if k != "_vec"} for c in new_chunks]
        vec_list = keep_vecs + [c["_vec"] for c in new_chunks]
        mat = np.vstack(vec_list).astype(np.float32) if vec_list else np.zeros((0, 384), dtype=np.float32)
        with self._lock:
            self._meta = combined_meta
            self._vectors = mat
        self._save_cache()

    # ---- persistence ----

    def _load_cache(self) -> None:
        try:
            if not self._cache_path.exists():
                return
            with open(self._cache_path, "rb") as fh:
                data = pickle.load(fh)
            if data.get("model") != config.SEMANTIC_MODEL:
                return  # model changed -> rebuild from scratch
            self._meta = data["meta"]
            self._vectors = data["vectors"]
            self._file_mtimes = data.get("mtimes", {})
            logger.info("Semantic cache loaded: %d chunks", len(self._meta))
        except Exception as e:
            logger.warning("Could not load semantic cache: %s", e)
            self._meta = []
            self._vectors = None
            self._file_mtimes = {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = {
                    "model": config.SEMANTIC_MODEL,
                    "meta": self._meta,
                    "vectors": self._vectors,
                    "mtimes": self._file_mtimes,
                }
            tmp = self._cache_path.with_suffix(".tmp")
            with open(tmp, "wb") as fh:
                pickle.dump(payload, fh)
            os.replace(tmp, self._cache_path)
        except Exception as e:
            logger.warning("Could not save semantic cache: %s", e)

    # ---- file watching (debounced, mirrors FrontmatterIndex) ----

    def _start_watching(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception:
            return

        index = self

        class _Handler(FileSystemEventHandler):
            def _handle(self, event):
                if event.is_directory:
                    return
                p = Path(event.src_path)
                if p.suffix != ".md":
                    return
                if config.EXCLUDED_DIRS & set(p.parts):
                    return
                index._schedule_debounce(event.src_path)

            def on_created(self, e):
                self._handle(e)

            def on_modified(self, e):
                self._handle(e)

            def on_deleted(self, e):
                self._handle(e)

        self._observer = Observer()
        self._observer.schedule(_Handler(), str(config.VAULT_PATH), recursive=True)
        self._observer.start()

    def _schedule_debounce(self, abs_path: str) -> None:
        with self._lock:
            self._pending_paths.add(abs_path)
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                config.SEMANTIC_INDEX_DEBOUNCE, self._flush_pending
            )
            self._debounce_timer.start()

    def _flush_pending(self) -> None:
        with self._lock:
            paths = self._pending_paths.copy()
            self._pending_paths.clear()
            self._debounce_timer = None
        for abs_path in paths:
            try:
                self._refresh_one(Path(abs_path))
            except Exception as e:
                logger.warning("Semantic refresh failed for %s: %s", abs_path, e)
