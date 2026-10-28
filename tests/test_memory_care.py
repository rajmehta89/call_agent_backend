"""Memory stays flat over many calls: caches are bounded, finished calls are forgotten, freed memory is released."""

import brain_service
import memory_care
import transfer_service as transfer_module
from routers import custom_twilio_voice_api as custom


def fresh_cache(monkeypatch):
    monkeypatch.setattr(brain_service, "_TTL_CACHE", {})
    return brain_service._TTL_CACHE


def test_a_new_knowledge_version_replaces_the_old_copy(monkeypatch):
    cache = fresh_cache(monkeypatch)
    brain_service._ttl_cached("brain_documents:1", 300, lambda: ["old documents"], family="brain_documents")
    brain_service._ttl_cached("brain_documents:2", 300, lambda: ["new documents"], family="brain_documents")
    assert "brain_documents:1" not in cache and cache["brain_documents:2"][1] == ["new documents"]


def test_long_unused_entries_are_forgotten(monkeypatch):
    cache = fresh_cache(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(brain_service, "monotonic", lambda: clock[0])
    brain_service._ttl_cached("customer:+61400000001", 30, lambda: "caller one")
    clock[0] += 601  # well past ten lifetimes (and at least 10 minutes)
    brain_service._ttl_cached("customer:+61400000002", 30, lambda: "caller two")
    assert list(cache) == ["customer:+61400000002"]


def test_the_cache_never_grows_past_its_limit(monkeypatch):
    cache = fresh_cache(monkeypatch)
    for number in range(brain_service._TTL_MAX_ENTRIES + 50):
        brain_service._ttl_cached(f"customer:{number}", 30, lambda: "x")
    assert len(cache) == brain_service._TTL_MAX_ENTRIES


def test_a_cached_value_is_still_served_within_its_lifetime(monkeypatch):
    fresh_cache(monkeypatch)
    loads = []
    for _ in range(3):
        brain_service._ttl_cached("setting:voice_agent:1", 60, lambda: loads.append(1) or {"name": "Voice"}, family="setting:voice_agent")
    assert len(loads) == 1


def test_finished_calls_are_forgotten_after_an_hour_but_live_ones_stay(monkeypatch):
    service = transfer_module.TransferService()
    clock = [0.0]
    monkeypatch.setattr(transfer_module, "monotonic", lambda: clock[0])
    service.register_call("finished", "+61400000001")
    service.register_call("live", "+61400000002")
    service.close_call("finished")
    clock[0] += 60
    service.register_call("another", "+61400000003")
    assert service.get_call("finished")  # Retell's post-call webhooks still read it for a while
    clock[0] += service.CLOSED_KEEP_SECONDS
    service.register_call("next", "+61400000004")
    assert service.get_call("finished") is None and service.get_call("live")


def test_the_audio_cache_keeps_the_most_recent_phrases():
    cache = custom._AudioCache(3)
    for phrase in ("one", "two", "three"):
        cache[phrase] = phrase.encode()
    assert cache.get("one") == b"one"  # just said: kept
    cache["four"] = b"four"
    assert list(cache) == ["three", "one", "four"] and cache.get("two") is None


def test_releasing_memory_after_a_call_is_safe_everywhere():
    memory_care.release_after_call()  # collects garbage; on Linux also returns freed pages to the system
    memory_care.limit_malloc_arenas()
    assert memory_care.resident_mb() is None or memory_care.resident_mb() > 0


def test_startup_objects_are_kept_out_of_collections():
    import gc
    try:
        memory_care.freeze_startup_objects()
        assert gc.get_freeze_count() > 0
    finally:
        gc.unfreeze()
