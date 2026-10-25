from datetime import datetime, timezone
from xml.etree import ElementTree

from human_transcript import lines_from_utterances, summarize_human_segment
from routers.custom_twilio_voice_api import _with_full_call_recording
from routers.inbox_api import _call_messages


def test_full_recording_starts_before_ai_stream_and_has_callback():
    twiml = _with_full_call_recording(
        '<?xml version="1.0"?><Response><Connect><Stream url="wss://example.test/media" /></Connect></Response>',
        "https://example.test",
    )
    root = ElementTree.fromstring(twiml)
    assert [child.tag for child in root] == ["Start", "Connect"]
    recording = root.find("Start/Recording")
    assert recording is not None
    assert recording.attrib["track"] == "both"
    assert recording.attrib["recordingStatusCallback"] == "https://example.test/api/call-forwarding/full-recording-result"


def test_human_channels_form_chronological_labeled_transcript():
    payload = {"results": {"utterances": [
        {"channel": 1, "start": 3.0, "transcript": "How can I help?"},
        {"channel": 0, "start": 1.0, "transcript": "I need a person."},
    ]}}
    lines = lines_from_utterances(payload, datetime(2026, 10, 24, tzinfo=timezone.utc))
    assert [line["role"] for line in lines] == ["customer", "human_agent"]
    assert [line["text"] for line in lines] == ["I need a person.", "How can I help?"]
    assert lines[0]["at"] < lines[1]["at"]


def test_brief_human_segment_is_summarized_without_inventing_a_request():
    lines = [{"role": "human_agent", "text": "Hello?"}, {"role": "human_agent", "text": "What?"},
             {"role": "customer", "text": "Hello?"}]
    summary = summarize_human_segment(lines)
    assert "brief" in summary and "No substantive request" in summary


def test_substantive_human_summary_uses_only_the_human_segment(monkeypatch):
    import ai_services
    seen = []

    class FakeServices:
        def chat_completion(self, messages, **kwargs):
            seen.append(messages)
            return "The caller asked about tiles; the agent agreed to send options."

    monkeypatch.setattr(ai_services, "AIServices", FakeServices)
    summary = summarize_human_segment([
        {"role": "customer", "text": "Could you send me the tile options?"},
        {"role": "human_agent", "text": "Yes, I will send the options tomorrow."},
    ])
    assert summary == "The caller asked about tiles; the agent agreed to send options."
    assert "Caller: Could you send me the tile options?" in seen[0][1]["content"]


def test_inbox_keeps_ai_then_human_conversation_when_timestamps_are_missing():
    call = {
        "messages": [{"role": "customer", "text": "Please connect me.", "at": ""},
                     {"role": "agent", "text": "Connecting you.", "at": ""}],
        "human_transcript": [{"role": "customer", "text": "Hello?", "at": "2026-10-24T09:38:30+00:00"},
                             {"role": "human_agent", "text": "How can I help?", "at": "2026-10-24T09:38:31+00:00"}],
    }
    lines = _call_messages(call)
    assert [line["text"] for line in lines] == ["Please connect me.", "Connecting you.", "Hello?", "How can I help?"]
    assert [line["segment"] for line in lines] == ["ai", "ai", "human", "human"]
