import asyncio

import conversation_memory
from conversation_memory import ConversationMemory, bounded_history, clip_message, estimate_tokens


def test_long_messages_are_clipped():
    clipped = clip_message("word " * 1000)
    assert len(clipped) <= conversation_memory.message_char_limit() + 2
    assert clipped.endswith("…")


def test_history_keeps_the_newest_messages_within_budget(monkeypatch):
    monkeypatch.setenv("BRAIN_HISTORY_TOKEN_BUDGET", "300")
    history = [{"role": "user", "content": f"message {i} " + "x" * 400} for i in range(20)]
    kept = bounded_history(history)
    assert kept[-1]["content"].startswith("message 19")
    assert sum(estimate_tokens(item["content"]) for item in kept) <= 300 + estimate_tokens(kept[0]["content"])


def test_the_greeting_counts_as_conversation():
    memory = ConversationMemory()
    memory.add_assistant("Hello, how can I help?")
    assert memory.history() == [{"role": "assistant", "content": "Hello, how can I help?"}]


def test_old_turns_are_folded_into_the_summary(monkeypatch):
    calls = []

    def fake_summarize(existing, messages):
        calls.append(len(messages))
        return "Priya, Bright Smile Dental, uses HubSpot."

    monkeypatch.setattr(conversation_memory, "summarize", fake_summarize)

    async def run():
        memory = ConversationMemory(keep_recent_messages=4, max_messages=6)
        for turn in range(4):
            memory.add(f"question {turn}", f"answer {turn}")
            await asyncio.sleep(0)
            if memory._compacting:
                await memory._compacting
        return memory

    memory = asyncio.run(run())
    assert calls, "summary should run once history exceeds max_messages"
    assert memory.summary.startswith("Priya")
    assert len(memory.messages) <= 6
    assert memory.messages[-1]["content"] == "answer 3"


def test_a_failed_summary_keeps_the_messages(monkeypatch):
    def broken(existing, messages):
        raise RuntimeError("model down")

    monkeypatch.setattr(conversation_memory, "summarize", broken)

    async def run():
        memory = ConversationMemory(keep_recent_messages=2, max_messages=2)
        memory.add("q1", "a1")
        memory.add("q2", "a2")
        await asyncio.sleep(0)
        if memory._compacting:
            await memory._compacting
        return memory

    memory = asyncio.run(run())
    assert len(memory.messages) == 4 and memory.summary == ""
