from types import SimpleNamespace
from unittest.mock import Mock

import brain_service as brain


def test_embedding_batches_preserve_vectors_with_bounded_memory(monkeypatch):
    model = SimpleNamespace(passage_embed=Mock(return_value=iter([[0.5, 0.25], [0.1, 0.9]])))
    monkeypatch.setattr(brain, "_LOCAL_EMBEDDER", model)
    monkeypatch.setenv("LOCAL_EMBEDDING_BATCH_SIZE", "8")
    assert brain.local_embeddings(["first", "second"]) == [[0.5, 0.25], [0.1, 0.9]]
    model.passage_embed.assert_called_once_with(["first", "second"], batch_size=8)


def test_render_retains_explicit_control_over_loading_local_model(monkeypatch):
    monkeypatch.setattr(brain, "_LOCAL_EMBEDDER", None)
    monkeypatch.setattr(brain, "_LOCAL_EMBEDDER_FAILED", False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://production.onrender.com")
    monkeypatch.delenv("LOCAL_EMBEDDINGS", raising=False)
    assert brain._local_embedder() is None
