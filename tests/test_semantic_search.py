"""Tests for semantic + hybrid search and embed-signature cache invalidation.

Covers the feat/semantic-v2 hardening (embed_signature cache key) plus the
public search surface: SemanticIndex build/persist/load, meaning-based search,
the vault_semantic_search ready-gate, vault_hybrid_search RRF + graceful
keyword-only fallback, and a regression guard that a `date:` frontmatter value
in a result does not raise during serialization.
"""
import json
import pickle
from pathlib import Path

import pytest

# Heavy, optional deps: skip the whole module if they are unavailable.
np = pytest.importorskip("numpy")
pytest.importorskip("fastembed")

from obsidian_vault_mcp import config
from obsidian_vault_mcp.semantic_index import SemanticIndex, _embed_signature
from obsidian_vault_mcp.tools import search as search_tools


# --- shared model (load once; embedding model init is the slow part) ---
_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding
        _MODEL = TextEmbedding(model_name=config.SEMANTIC_MODEL)
    return _MODEL


@pytest.fixture
def sem_vault(tmp_path, monkeypatch):
    """Tiny vault with semantically distinct notes + one dated note."""
    vault = tmp_path / "sem-vault"
    vault.mkdir()
    (vault / "running.md").write_text(
        "---\ntype: note\ntitle: Running\n---\n\n# Running and jogging\n\n"
        "Cardio training, sprint intervals, marathon endurance, fast pace.\n",
        encoding="utf-8",
    )
    (vault / "cooking.md").write_text(
        "---\ntype: note\ntitle: Cooking\n---\n\n# Cooking pasta\n\n"
        "Boil water, add salt, simmer tomato sauce, season with fresh basil.\n",
        encoding="utf-8",
    )
    (vault / "dated.md").write_text(
        "---\ndate: 2026-06-15\ntype: log\n---\n\n# Daily log\n\n"
        "Sensor calibration drift was measured on the treadmill today.\n",
        encoding="utf-8",
    )
    # config.VAULT_PATH is read as a module attribute; setattr auto-restores.
    monkeypatch.setattr(config, "VAULT_PATH", Path(str(vault)))
    return vault


@pytest.fixture(autouse=True)
def _reset_semantic_handle():
    """Never leak a wired index into other test modules."""
    yield
    search_tools.set_semantic_index(None)


def _build(cache_path: Path) -> SemanticIndex:
    idx = SemanticIndex()
    idx._cache_path = Path(str(cache_path))  # never touch the production pkl
    idx._np = np
    idx._model = _model()
    idx._load_cache()
    idx._refresh_all()
    idx.ready = True
    return idx


def test_build_persist_and_reload(sem_vault, tmp_path):
    p = tmp_path / "idx.pkl"
    idx = _build(p)
    assert idx.chunk_count > 0

    idx._save_cache()
    assert p.exists()
    data = pickle.loads(p.read_bytes())
    assert data["embed_signature"] == _embed_signature()
    assert "fastembed=" in data["embed_signature"]

    # Fresh instance loads cached vectors without re-embedding.
    idx2 = SemanticIndex()
    idx2._cache_path = p
    idx2._np = np
    idx2._load_cache()
    assert idx2.chunk_count == idx.chunk_count


def test_search_by_meaning(sem_vault, tmp_path):
    idx = _build(tmp_path / "idx.pkl")
    hits = idx.search("endurance run and sprint workout", max_results=3)
    assert hits, "expected semantic hits"
    paths = [h["path"] for h in hits]
    # running.md must outrank the unrelated cooking note.
    assert "running.md" in paths
    assert paths.index("running.md") < (paths.index("cooking.md") if "cooking.md" in paths else 99)


def test_semantic_tool_ready_gate_and_graceful(sem_vault, tmp_path):
    # (a) no index wired -> explicit error, never crash
    search_tools.set_semantic_index(None)
    out = json.loads(search_tools.vault_semantic_search("anything"))
    assert "error" in out

    # (b) index present but not ready -> graceful empty + note
    not_ready = SemanticIndex()
    search_tools.set_semantic_index(not_ready)
    out = json.loads(search_tools.vault_semantic_search("anything"))
    assert out["results"] == []
    assert "note" in out

    # (c) ready index -> real results
    idx = _build(tmp_path / "idx.pkl")
    search_tools.set_semantic_index(idx)
    out = json.loads(search_tools.vault_semantic_search("marathon pace training"))
    assert out["total_matches"] >= 1
    assert any(r["path"] == "running.md" for r in out["results"])


def test_hybrid_rrf_and_keyword_fallback(sem_vault, tmp_path):
    # Not ready -> graceful keyword-only fallback (still returns keyword hits).
    search_tools.set_semantic_index(SemanticIndex())
    out = json.loads(search_tools.vault_hybrid_search("calibration"))
    assert out["mode"].startswith("keyword-only")
    assert any(r["path"] == "dated.md" for r in out["results"])

    # Ready -> hybrid RRF fusion of keyword + semantic.
    idx = _build(tmp_path / "idx.pkl")
    search_tools.set_semantic_index(idx)
    out = json.loads(search_tools.vault_hybrid_search("calibration drift"))
    assert out["mode"].startswith("hybrid")
    assert out["total_matches"] >= 1


def test_embed_signature_invalidation(sem_vault, tmp_path):
    p = tmp_path / "idx.pkl"
    idx = _build(p)
    idx._save_cache()

    # Correct signature -> loads.
    ok = SemanticIndex()
    ok._cache_path = p
    ok._np = np
    ok._load_cache()
    assert ok.chunk_count > 0

    # Tamper the signature (simulate a fastembed behaviour change) -> must NOT load.
    data = pickle.loads(p.read_bytes())
    data["embed_signature"] = "some-model|fastembed=0.0.0-old-CLS"
    p.write_bytes(pickle.dumps(data))

    stale = SemanticIndex()
    stale._cache_path = p
    stale._np = np
    stale._load_cache()
    assert stale.chunk_count == 0  # rejected -> would rebuild from scratch

    # Legacy cache without embed_signature at all -> also rejected.
    data.pop("embed_signature", None)
    p.write_bytes(pickle.dumps(data))
    legacy = SemanticIndex()
    legacy._cache_path = p
    legacy._np = np
    legacy._load_cache()
    assert legacy.chunk_count == 0


def test_date_frontmatter_does_not_raise(sem_vault, tmp_path):
    """Regression: a `date:` value in result frontmatter must serialize cleanly."""
    idx = _build(tmp_path / "idx.pkl")
    search_tools.set_semantic_index(idx)
    raw = search_tools.vault_semantic_search("treadmill calibration drift")
    out = json.loads(raw)  # would raise if dumps() choked on datetime.date
    assert any(r["path"] == "dated.md" for r in out["results"])
    hit = next(r for r in out["results"] if r["path"] == "dated.md")
    assert "date" in hit["frontmatter_excerpt"]
