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


def test_semantic_batch_override_does_not_change_knowledge_indexing_batch_size(monkeypatch):
    model = SimpleNamespace(passage_embed=Mock(side_effect=lambda texts, **kwargs: iter([[0.5, 0.25]] * len(texts))))
    monkeypatch.setattr(brain, "_LOCAL_EMBEDDER", model)
    monkeypatch.setenv("LOCAL_EMBEDDING_BATCH_SIZE", "8")
    monkeypatch.delenv("VOICE_SEMANTIC_BATCH_SIZE", raising=False)
    brain.local_embeddings(["turn check"], batch_size=brain.semantic_embedding_batch_size())
    brain.local_embeddings(["knowledge chunk"])
    assert model.passage_embed.call_args_list[0].kwargs == {"batch_size": 1}
    assert model.passage_embed.call_args_list[1].kwargs == {"batch_size": 8}


def test_invalid_semantic_batch_size_keeps_the_small_default(monkeypatch):
    monkeypatch.setenv("VOICE_SEMANTIC_BATCH_SIZE", "invalid")
    assert brain.semantic_embedding_batch_size() == 1
