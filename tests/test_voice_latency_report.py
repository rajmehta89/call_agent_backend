from scripts.voice_latency_report import report, turn_measurements


def test_reply_audio_clock_excludes_wait_phrases_and_preserves_original():
    turn = {"pipeline": "custom_twilio", "turn_detection_ms": 1480, "first_audio_ms": 2339,
            "first_audible_ms": 1101, "speech_end_to_first_audible_ms": 2581}
    measurements = turn_measurements(turn)
    assert measurements["speech_end_to_reply_audio_ms"] == 3819
    assert measurements["speech_end_to_first_audible_ms"] == 2581
    assert "speech_end_to_reply_audio_ms" not in turn


def test_missing_audio_and_external_retell_playback_are_not_invented():
    for turn in ({"pipeline": "custom_twilio", "turn_detection_ms": 400, "interrupted": True},
                 {"pipeline": "retell", "turn_detection_ms": 400, "first_audio_ms": 200},
                 {"pipeline": "custom_twilio", "turn_detection_ms": None, "first_audio_ms": 200}):
        assert "speech_end_to_reply_audio_ms" not in turn_measurements(turn)


def test_report_does_not_treat_legacy_cumulative_checkpoints_as_operation_durations(capsys):
    report([{"pipeline": "custom_twilio", "turn_detection_ms": 400, "first_audio_ms": 1395,
             "prompt_store_ms": 299, "prompt_caller_ms": 299},
            {"pipeline": "custom_twilio", "turn_detection_ms": 1480, "first_audio_ms": 2339,
             "prompt_store_work_ms": 200, "filler_played": True, "interrupted": True}])
    lines = capsys.readouterr().out.splitlines()
    reply = next(line for line in lines if line.startswith("caller stops -> reply audio"))
    assert reply.split()[-4:] == ["2", "2807", "3718", "3819"]
    store = next(line for line in lines if line.startswith("store work"))
    assert store.split()[-4:] == ["1", "200", "200", "200"]
    assert not any(line.startswith("caller context work") for line in lines)
    assert "filler played: 1 | interrupted: 1 | turns: 2" in lines
