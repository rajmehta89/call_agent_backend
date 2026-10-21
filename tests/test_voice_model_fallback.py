"""Voice model fallback: the backup model answers when the main one fails before saying anything."""
from types import SimpleNamespace

import pytest

from ai_services import AIServices


def chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


class FakeCompletions:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.behaviour(kwargs["model"])


def services_with(behaviour):
    services = AIServices()
    services.provider = "openrouter"
    services.config.OPENROUTER_MODEL = "openai/gpt-oss-20b"
    services.fallback_model = "openai/gpt-4.1-mini"
    services.stream_timeout = 6
    fake = FakeCompletions(behaviour)
    services.openai_client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return services, fake


def test_fallback_answers_when_main_model_fails():
    def behaviour(model):
        if model == "openai/gpt-oss-20b":
            raise TimeoutError("host stalled")
        return iter([chunk("Hello "), chunk("there.")])
    services, fake = services_with(behaviour)
    assert "".join(services.stream_chat_completion([{"role": "user", "content": "hi"}], 100)) == "Hello there."
    assert [call["model"] for call in fake.calls] == ["openai/gpt-oss-20b", "openai/gpt-4.1-mini"]
    assert "reasoning" in fake.calls[0]["extra_body"] and "reasoning" not in fake.calls[1]["extra_body"]
    assert fake.calls[0]["timeout"] == 6


def test_no_fallback_once_the_reply_has_started():
    def behaviour(model):
        def stream():
            yield chunk("Half a ")
            raise ConnectionError("dropped mid-reply")
        return stream()
    services, fake = services_with(behaviour)
    with pytest.raises(ConnectionError):
        list(services.stream_chat_completion([{"role": "user", "content": "hi"}], 100))
    assert len(fake.calls) == 1
