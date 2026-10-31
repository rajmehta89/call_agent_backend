import asyncio
import json

import pytest

from scripts.benchmark_pipecat_speech import measure_stt


class Replay:
    def __init__(self, events):
        self.events = events

    async def send(self, audio):
        pass

    def __aiter__(self):
        async def events():
            for event in self.events:
                yield json.dumps(event)
        return events()


def test_replay_accepts_nova_utterance_end_without_claiming_speech_final():
    events = [{"type": "Results", "is_final": True, "speech_final": False,
               "channel": {"alternatives": [{"transcript": "A question.", "words": [{"end": 1.2}]}]}},
              {"type": "UtteranceEnd", "last_word_end": 1.2}]
    result = asyncio.run(measure_stt(Replay(events), b"\xff" * 160))
    assert result["transcript"] == "A question."
    assert result["endpoint_event"] == "UtteranceEnd"


def test_replay_does_not_turn_interim_words_into_a_completed_turn():
    events = [{"type": "Results", "is_final": False, "speech_final": False,
               "channel": {"alternatives": [{"transcript": "Partial words"}]}},
              {"type": "UtteranceEnd"}]
    with pytest.raises(RuntimeError, match="No completed transcript"):
        asyncio.run(measure_stt(Replay(events), b"\xff" * 160))


def test_replay_reports_provider_rejection_immediately():
    with pytest.raises(RuntimeError, match="INVALID_QUERY"):
        asyncio.run(measure_stt(Replay([{"type": "Error", "code": "INVALID_QUERY"}]), b"\xff" * 160))


def test_replay_keeps_the_flux_turn_boundary():
    events = [{"type": "TurnInfo", "event": "Update", "transcript": "Partial"},
              {"type": "TurnInfo", "event": "EndOfTurn", "transcript": "Complete question.", "words": [{"end": 1.2}]}]
    result = asyncio.run(measure_stt(Replay(events), b"\xff" * 160))
    assert result["transcript"] == "Complete question." and result["endpoint_event"] == "EndOfTurn"
