"""Deciding things about a call turn by what the words mean, not by keyword lists.

A Classifier holds a few example sentences per kind ("the caller is asking for a callback": "Can someone call me
back?", "I'd like a quote"...). A turn's kind is the kind of its closest examples in meaning, using the local
embedding model the knowledge search already runs, with the network embedding provider as a fallback. Every text is embedded once and cached, so the several decisions
made about one turn (which model answers, which acknowledgement fits, which store data it needs...) share one vector.
None of the examples hold facts about the business: they describe kinds of things callers say.
"""

import logging
import threading
from collections import OrderedDict
from concurrent.futures import Future
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_CACHE: "OrderedDict[str, object]" = OrderedDict()
_CACHE_SIZE = 512
_CACHE_LOCK = threading.Lock()
_INFLIGHT: Dict[str, Future] = {}


def vectors(texts: Sequence[str]):
    """Unit meaning vectors for texts, from the local model or the network embedding provider."""
    import numpy as np
    from brain_service import brain_service, local_embeddings
    keys = [" ".join((text or "").split()) for text in texts]
    pending, owned = {}, []
    with _CACHE_LOCK:
        for key in dict.fromkeys(keys):
            if key in _CACHE:
                done = Future()
                done.set_result(_CACHE[key])
                pending[key] = done
            else:
                if key not in _INFLIGHT:
                    _INFLIGHT[key] = Future()
                    owned.append(key)
                pending[key] = _INFLIGHT[key]
    if owned:
        normalized = []
        try:
            fresh = local_embeddings(owned)
            if not fresh:
                fresh, _ = brain_service._embeddings(owned)
            if len(fresh) == len(owned) and all(len(vector) for vector in fresh):
                for vector in fresh:
                    values = np.asarray(vector, dtype=float)
                    magnitude = np.linalg.norm(values)
                    normalized.append(values / magnitude if magnitude else None)
        except BaseException as error:
            for key in owned:
                pending[key].set_exception(error)
            raise
        else:
            with _CACHE_LOCK:
                for key, vector in zip(owned, normalized):
                    if vector is not None:
                        _CACHE[key] = vector
                        _CACHE.move_to_end(key)
                while len(_CACHE) > _CACHE_SIZE:
                    _CACHE.popitem(last=False)
            for index, key in enumerate(owned):
                pending[key].set_result(normalized[index] if normalized else None)
        finally:
            with _CACHE_LOCK:
                for key in owned:
                    _INFLIGHT.pop(key, None)
    # Wait outside the cache lock. Independent checks of the same caller words
    # share one provider request, including when an older response is cancelled.
    found = [pending[key].result() for key in keys]
    return None if any(vector is None for vector in found) else np.vstack(found)


class Classifier:
    """Which kind of thing a text is, by its closest examples in meaning."""

    def __init__(self, name: str, examples: Dict[str, List[str]], min_score: float = 0.55, close_call: float = 0.06):
        self.name, self.examples, self.min_score, self.close_call = name, examples, min_score, close_call
        self._labels: List[str] = []
        self._matrix = None
        self._lock = threading.Lock()

    def _ready(self) -> bool:
        if self._matrix is None:
            with self._lock:
                if self._matrix is None:
                    labels = [kind for kind, items in self.examples.items() for _ in items]
                    matrix = vectors([text for items in self.examples.values() for text in items])
                    if matrix is None:
                        return False
                    self._labels, self._matrix = labels, matrix
        return True

    def rank(self, text: str, earlier: str = "", earlier_weight: float = 0.95) -> Optional[List[Tuple[str, float]]]:
        """Every kind with its score (the closest example of that kind), best first. `earlier` (the turn before) is
        also tried, joined to this one, so a follow-up is read in its context. None when the model is unavailable."""
        text = (text or "").strip()
        if not text:
            return None
        try:
            if not self._ready():
                return None
            probes = [text] + ([f"{earlier.strip()} {text}"] if (earlier or "").strip() else [])
            probe_vectors = vectors(probes)
            if probe_vectors is None:
                return None
        except Exception as error:
            logger.warning("%s semantic decision unavailable: %s", self.name, type(error).__name__)
            return None
        scores: Dict[str, float] = {}
        for weight, vector in zip((1.0, earlier_weight), probe_vectors):
            for label, similarity in zip(self._labels, self._matrix @ vector):
                scores[label] = max(scores.get(label, 0.0), float(similarity) * weight)
        return sorted(scores.items(), key=lambda item: item[1], reverse=True)

    def best(self, text: str, earlier: str = "") -> Optional[str]:
        """The single closest kind ("" when nothing is close enough); None when the model is unavailable."""
        ranked = self.rank(text, earlier)
        if ranked is None:
            return None
        kind, score = ranked[0]
        logger.debug("%s %r -> %s (%s)", self.name, text[:60], kind, ", ".join(f"{k} {v:.2f}" for k, v in ranked[:3]))
        return kind if score >= self.min_score else ""

    def close(self, text: str, earlier: str = "") -> Optional[List[str]]:
        """The closest kind, plus the next one when the two are a close call; [] when nothing is close enough."""
        ranked = self.rank(text, earlier)
        if ranked is None:
            return None
        (best, top), (second, runner_up) = ranked[0], ranked[1]
        if top < self.min_score:
            return []
        return [best] + ([second] if top - runner_up < self.close_call and runner_up >= self.min_score else [])


def most_similar(text: str, candidates: Sequence[str]) -> List[Tuple[int, float]]:
    """Candidates by closeness in meaning to `text` (index, similarity), best first; [] when unavailable."""
    if not candidates or not (text or "").strip():
        return []
    matrix = vectors([text, *candidates])
    if matrix is None:
        return []
    similarities = matrix[1:] @ matrix[0]
    return sorted(((index, float(score)) for index, score in enumerate(similarities)), key=lambda item: item[1], reverse=True)
