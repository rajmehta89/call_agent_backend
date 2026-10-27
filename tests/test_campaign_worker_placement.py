import asyncio

import pytest

import email_campaign
import main


@pytest.mark.parametrize("env, runs", [
    ({"RENDER": "true"}, False),                                   # production answers calls only
    ({"RENDER": "true", "EMAIL_CAMPAIGN_WORKER": "true"}, True),   # explicit opt-in
    ({}, True),                                                    # the local machine runs the campaign
    ({"EMAIL_CAMPAIGN_WORKER": "off"}, False),
])
def test_campaign_worker_stays_off_the_production_voice_server(monkeypatch, env, runs):
    for name in ("RENDER", "EMAIL_CAMPAIGN_WORKER"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(main, "_environment", "development")

    async def idle():
        await asyncio.sleep(3600)

    monkeypatch.setattr(email_campaign, "email_campaign_worker", idle)

    async def start_and_stop():
        await main.start_email_campaign_worker()
        started = main.app.state.email_campaign_task is not None
        await main.stop_email_campaign_worker()
        return started

    assert asyncio.run(start_and_stop()) is runs
