from concurrent.futures import ThreadPoolExecutor
from threading import Event

import numpy as np
import pytest

import brain_service as brain_module
import meaning
import voice_turns


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(meaning, "_CACHE", meaning.OrderedDict())
    monkeypatch.setattr(meaning, "_INFLIGHT", {})
    monkeypatch.setattr(brain_module, "local_embeddings", lambda texts, **kwargs: [])


def test_concurrent_checks_share_a_network_embedding(monkeypatch):
    entered, release, joined = Event(), Event(), Event()
    calls = []

    def embed(texts):
        calls.append(texts)
        entered.set()
        assert release.wait(3)
        return [[3.0, 4.0] for _ in texts], "openrouter"

    class ObservedFuture(meaning.Future):
        def result(self, *args, **kwargs):
            if not self.done():
                joined.set()
            return super().result(*args, **kwargs)

    monkeypatch.setattr(meaning, "Future", ObservedFuture)
    monkeypatch.setattr(brain_module.brain_service, "_embeddings", embed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(meaning.vectors, ["same caller words"])
        assert entered.wait(3)
        second = pool.submit(meaning.vectors, ["same caller words"])
        try:
            assert joined.wait(3)
        finally:
            release.set()
        np.testing.assert_allclose(first.result(), [[0.6, 0.8]])
        np.testing.assert_allclose(second.result(), [[0.6, 0.8]])
    assert calls == [["same caller words"]]
    assert not meaning._INFLIGHT


@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_failed_embeddings_can_retry_without_stale_inflight_work(monkeypatch, failure):
    def fail(texts):
        if failure == "exception":
            raise RuntimeError("provider unavailable")
        return [], None

    monkeypatch.setattr(brain_module.brain_service, "_embeddings", fail)
    if failure == "exception":
        with pytest.raises(RuntimeError):
            meaning.vectors(["retry these words"])
    else:
        assert meaning.vectors(["retry these words"]) is None
    assert not meaning._INFLIGHT
    monkeypatch.setattr(brain_module.brain_service, "_embeddings", lambda texts: ([[1.0, 0.0]], "openrouter"))
    np.testing.assert_allclose(meaning.vectors(["retry these words"]), [[1.0, 0.0]])


def test_turn_inputs_use_one_batch_and_classifiers_reuse_it(monkeypatch):
    calls = []

    def embed(texts):
        calls.append(texts)
        return [[1.0, 0.0] for _ in texts], "openrouter"

    monkeypatch.setattr(brain_module.brain_service, "_embeddings", embed)
    monkeypatch.setattr(voice_turns, "kind_of_turn_text", lambda text: "Can you deliver a product to Geelong?")
    caller, agent = "Can you deliver a basin to Geelong?", "Hello. What do you need?"
    voice_turns.prepare_turn_meaning(caller, agent)
    assert len(calls) == 1
    assert caller in calls[0]
    assert "Can you deliver a basin to that town?" in calls[0]
    assert "Can you deliver a product to Geelong?" in calls[0]
    assert agent in calls[0]
    assert voice_turns.agent_question(agent) in calls[0]
    meaning.vectors(calls[0])
    assert len(calls) == 1


def test_duplicate_inputs_keep_the_requested_order(monkeypatch):
    monkeypatch.setattr(brain_module.brain_service, "_embeddings",
                        lambda texts: ([[1.0, 0.0] if text == "one" else [0.0, 1.0] for text in texts], "openrouter"))
    np.testing.assert_allclose(meaning.vectors(["one", "two", "one"]), [[1, 0], [0, 1], [1, 0]])


def test_semantic_vectors_use_the_separate_small_batch_setting(monkeypatch):
    captured = []
    def local(texts, *, batch_size):
        captured.append((texts, batch_size))
        return [[3.0, 4.0] for text in texts]
    monkeypatch.setattr(brain_module, "local_embeddings", local)
    monkeypatch.setenv("LOCAL_EMBEDDING_BATCH_SIZE", "8")
    monkeypatch.delenv("VOICE_SEMANTIC_BATCH_SIZE", raising=False)
    np.testing.assert_allclose(meaning.vectors(["caller", "context"]), [[0.6, 0.8], [0.6, 0.8]])
    assert captured == [(["caller", "context"], 1)]


def test_semantic_provider_failure_returns_unavailable_without_guessing(monkeypatch):
    classifier = meaning.Classifier("unavailable", {"live": ["connect now"], "callback": ["call later"]})
    def unavailable(*args):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(meaning, "vectors", unavailable)
    assert classifier.rank("Please put me through") is None
