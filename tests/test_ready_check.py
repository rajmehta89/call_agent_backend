import asyncio
import json

import main
import pytest


def _status(response):
    return (response.status_code, json.loads(response.body)) if hasattr(response, "status_code") else (200, response)


def test_ready_waits_for_the_warm_up_then_reports_ready(monkeypatch):
    monkeypatch.setenv("READY_MAX_SECONDS", "240")
    monkeypatch.setattr(main, "_voice_warm", __import__("threading").Event())
    code, body = _status(asyncio.run(main.ready_check()))
    assert code == 503 and body["ready"] is False  # a new server takes no calls while still warming up
    main._voice_warm.set()
    code, body = _status(asyncio.run(main.ready_check()))
    assert code == 200 and body["ready"] is True and body["warm"] is True


def test_ready_never_blocks_a_deploy_forever(monkeypatch):
    monkeypatch.setenv("READY_MAX_SECONDS", "0")
    monkeypatch.setattr(main, "_voice_warm", __import__("threading").Event())
    code, body = _status(asyncio.run(main.ready_check()))
    assert code == 200 and body["ready"] is True and body["warm"] is False


@pytest.mark.parametrize("revision,expected", [("a" * 40, "a" * 12), ("", None), ("not-a-revision", None)])
def test_readiness_identifies_only_a_valid_deployed_commit(monkeypatch, revision, expected):
    monkeypatch.setenv("RENDER_GIT_COMMIT", revision)
    monkeypatch.setenv("READY_MAX_SECONDS", "240")
    monkeypatch.setattr(main, "_voice_warm", __import__("threading").Event())
    _, cold = _status(asyncio.run(main.ready_check()))
    main._voice_warm.set()
    _, warm = _status(asyncio.run(main.ready_check()))
    assert cold["revision"] == warm["revision"] == expected


def test_readiness_reports_the_selected_speech_install_without_credentials(monkeypatch):
    import voice_runtime
    monkeypatch.setenv("CUSTOM_VOICE_RUNTIME", "pipecat")
    monkeypatch.setattr(voice_runtime.metadata, "version", lambda name: voice_runtime.PIPECAT_VERSION)
    monkeypatch.setenv("READY_MAX_SECONDS", "0")
    _, body = _status(asyncio.run(main.ready_check()))
    assert body["speech_runtime"] == {"selected": "pipecat", "pipecat_version": "1.15.0"}
