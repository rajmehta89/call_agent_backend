import asyncio
from threading import Event
from types import SimpleNamespace

from routers import retell_api as retell


def test_independent_checks_overlap_and_unknown_place_still_uses_careful_model(monkeypatch):
    place_started = Event()
    seen = {}

    def choose(*args):
        assert place_started.wait(3), "Place check was held until after model selection"
        return "fast-model"

    def place(*args):
        place_started.set()
        return "The business has not confirmed delivery to Darwin."

    def complete(messages, *args, **kwargs):
        seen.update(messages=messages, model=kwargs.get("model"))
        yield "The team can confirm delivery for your area."

    monkeypatch.setattr(retell, "choose_voice_model", choose)
    monkeypatch.setattr(retell, "place_note", place)
    monkeypatch.setattr(retell, "contact_check", lambda *args: ("", False))
    monkeypatch.setattr(retell, "sounds_unfinished", lambda text: False)
    monkeypatch.setattr(retell, "bridges_enabled", lambda: False)
    monkeypatch.setattr(retell, "_system_prompt", lambda *args: "Business facts")
    monkeypatch.setattr(retell, "_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(retell.brain_service, "knowledge_text", lambda: "")
    monkeypatch.setenv("VOICE_CAREFUL_MODEL", "careful-model")
    services = SimpleNamespace(fallback_model="fallback", config=SimpleNamespace(OPENROUTER_MODEL="main"),
                               stream_chat_completion=complete)

    async def run():
        return "".join([chunk async for chunk in retell._stream_reply(
            services, [{"role": "user", "content": "Do you deliver to Darwin?"}], "unknown", "test-call", 1)])

    assert "confirm" in asyncio.run(run())
    assert seen["model"] == "careful-model"
    assert any("not confirmed delivery" in message["content"] for message in seen["messages"])
