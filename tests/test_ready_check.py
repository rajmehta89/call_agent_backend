import asyncio
import json

import main


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
