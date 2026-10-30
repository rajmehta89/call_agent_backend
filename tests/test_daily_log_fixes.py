"""Regressions from the October 30 calls and logs; all external services are fakes."""

import asyncio
from threading import Event, get_ident
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

import call_forwarding
import call_status
import email_campaign
import meaning
import private_email_service as email
import realtime
import voice_control
from routers import custom_twilio_voice_api as custom, twilio_api
from test_custom_voice_early_start import run_call


@pytest.fixture(scope="module", autouse=True)
def warm_semantics():
    import turn_meaning
    from voice_presence import CONNECTION_CHECK
    for classifier in [*vars(turn_meaning).values(), CONNECTION_CHECK, voice_control.CONTROL_SCOPE, voice_control.CALLER_PAUSE]:
        if isinstance(classifier, meaning.Classifier):
            classifier.rank("hello")


@pytest.mark.parametrize("text", [
    "No callback, thanks. What time do you open tomorrow?",
    "I don't want a call. Why don't you have tomorrow's schedule? Is it a public holiday?",
    "No, don't transfer me. Can you explain your opening hours?",
    "No thanks, I still need to know the delivery cost.",
])
def test_declining_an_offer_does_not_discard_the_rest_of_the_question(text):
    assert voice_control.has_followup_request(text) is True
    # An empty result deliberately sends the entire utterance to the answer model.
    assert call_forwarding.contextual_handoff_intent(text, "Would you like someone to call you back?") in ("", None)


def test_hindi_mixed_turn_keeps_its_meaning_through_shared_translation(monkeypatch):
    text = "नहीं नहीं call नहीं चाहिए कल का schedule क्यों नहीं है? कल का जो public holiday है कोई?"
    original = meaning.english_text
    monkeypatch.setattr(meaning, "english_text", lambda value:
                        "I don't want a call. Why don't you have tomorrow's schedule? Is it a public holiday?"
                        if value == text else original(value))
    assert voice_control.has_followup_request(text) is True
    assert call_forwarding.contextual_handoff_intent(text, "Would you like someone to call you back?") == ""


def test_simple_refusal_still_acknowledges_the_offer():
    from voice_turns import pause_request
    assert not pause_request("No, thanks.")
    assert voice_control.has_followup_request("No, thanks.") is False
    assert call_forwarding.contextual_handoff_intent("No, thanks.", "Would you like me to connect you with the team?") == "declined"


@pytest.mark.parametrize("language", ["ur", "ar"])
def test_pause_reassurance_uses_the_active_call_language(language):
    from voice_turns import pause_reply, pause_request
    assert pause_request("Wait, please.")
    assert pause_reply("Wait, please.", language) != pause_reply("Wait, please.", "en")


@pytest.mark.parametrize("scope", [None, [], ["followup", "control"]])
def test_uncertain_semantics_keep_the_full_turn_for_an_answer(monkeypatch, scope):
    monkeypatch.setattr(voice_control.CONTROL_SCOPE, "close", lambda text: scope)
    assert call_forwarding.contextual_handoff_intent("No, thanks.", "Would you like me to connect you with the team?") == ""


@pytest.mark.parametrize("intent,template", [
    ("declined", call_forwarding.OF_COURSE_LINE),
    ("callback_ack", call_forwarding.WELCOME_LINE),
])
def test_control_replies_keep_urdu_and_final_save_runs_off_the_audio_loop(monkeypatch, intent, template):
    spoken, logged, audio_threads, save_threads = [], [], [], []
    monkeypatch.setattr(custom, "effective_language", lambda *args: "ur")
    monkeypatch.setattr(custom, "requested_language", lambda *args: "")
    monkeypatch.setattr(custom, "spoken_language", lambda *args: "")
    monkeypatch.setattr(custom, "complete_nova_turn", lambda text: True)
    monkeypatch.setattr(call_forwarding, "contextual_handoff_intent", lambda *args: intent)

    class SavedCalls(list):
        def append(self, value):
            save_threads.append(get_ident())
            super().append(value)

    async def tts(text, engine, language="en"):
        audio_threads.append(get_ident())
        spoken.append((text, language))
        yield b"\xff" * 160

    logged = SavedCalls()
    run_call(monkeypatch, [(0.25, "No, thanks.", True, True)], tts=tts,
             logged=logged, model_seconds=0.05, call_seconds=1.1)
    assert (call_forwarding.caller_line(template, "ur"), "ur") in spoken
    assert logged[-1]["voice_turn_timings"][0]["action"] == intent
    assert logged[-1]["provider_metadata"]["stream_end_event"] == "twilio_stop"
    assert save_threads and not set(save_threads).intersection(audio_threads)


@pytest.mark.parametrize("surface", ["status", "transfer", "realtime"])
def test_database_work_does_not_block_another_calls_audio_heartbeat(monkeypatch, surface):
    started, heartbeat = Event(), Event()
    blocked = []

    def upstream(*args, **kwargs):
        started.set()
        blocked.append(not heartbeat.wait(1))
        return {"role": "admin"} if surface == "realtime" else True

    app = FastAPI()
    if surface == "realtime":
        app.include_router(realtime.router)
        monkeypatch.setattr(realtime, "user_from_token", upstream)
        monkeypatch.setattr(realtime, "has_permission", lambda *args: True)

        async def stream(user):
            yield 'data: {"kind":"hello"}\n\n'

        monkeypatch.setattr(realtime, "_stream", stream)
    else:
        app.include_router(twilio_api.router)
        app.dependency_overrides[twilio_api.verify_twilio_request] = lambda: None
        monkeypatch.setattr(twilio_api, "update_call_from_twilio", upstream if surface == "status" else lambda *a, **k: True)
        monkeypatch.setattr(twilio_api.transfer_service, "update_transfer", upstream)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            request = asyncio.create_task(client.get("/api/realtime/events?token=test-session-token")
                if surface == "realtime" else client.post("/api/twilio/" + ("status" if surface == "status" else "transfer-status"),
                data={"CallSid": "CA_TEST", "CallStatus": "completed", "DialCallStatus": "completed"}))
            assert await asyncio.to_thread(started.wait, 2)
            heartbeat.set()
            assert (await request).status_code == 200

    asyncio.run(run())
    assert blocked == [False]


@pytest.mark.parametrize("failure", ["discovery", "digest"])
def test_failed_campaign_phase_does_not_skip_approved_delivery(monkeypatch, failure):
    phases, sleep_count = [], 0

    def phase(name):
        def operation():
            phases.append(name)
            if name == failure:
                raise TimeoutError("simulated provider outage")
        return operation

    async def sleep(seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(email_campaign, "refill_campaign_candidates", phase("discovery"))
    monkeypatch.setattr(email_campaign, "notify_pending_approval_digest", phase("digest"))
    monkeypatch.setattr(email_campaign, "_deliver_approved_email_outbox", phase("delivery"))
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(email_campaign.email_campaign_worker())
    assert phases == ["discovery", "digest", "delivery"]


@pytest.mark.parametrize("already_saved", [False, True])
def test_sent_archive_retry_never_resends_mail_or_duplicates_a_saved_copy(monkeypatch, already_saved):
    smtp, first, retry = MagicMock(), MagicMock(), MagicMock()
    first.append.side_effect = email.imaplib.IMAP4.abort("connection dropped after APPEND")
    retry.select.return_value = ("OK", [b"1"])
    retry.search.return_value = ("OK", [b"12" if already_saved else b""])
    retry.append.return_value = ("OK", [b"13"])
    connections = [MagicMock(), MagicMock()]
    for connection, mailbox in zip(connections, (first, retry)):
        connection.__enter__.return_value = mailbox
    monkeypatch.setenv("PRIVATE_EMAIL_ADDRESS", "sender@example.test")
    monkeypatch.setenv("PRIVATE_EMAIL_PASSWORD", "test-only")
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", lambda *a, **k: smtp)
    monkeypatch.setattr(email.imaplib, "IMAP4_SSL", MagicMock(side_effect=connections))
    result = email.private_email_service.send(["recipient@example.test"], "Archive test", "Test message")
    message = smtp.__enter__.return_value.send_message.call_args.args[0]
    smtp.__enter__.return_value.send_message.assert_called_once()
    assert result["status"] == "sent" and result["saved_to_sent"] is True
    retry.search.assert_called_once_with(None, "HEADER", "Message-ID", message["Message-ID"])
    assert retry.append.call_count == (0 if already_saved else 1)


def test_uncertain_archive_lookup_does_not_blindly_append_another_copy(monkeypatch):
    first, retry = MagicMock(), MagicMock()
    first.__enter__.side_effect = TimeoutError
    mailbox = retry.__enter__.return_value
    mailbox.select.return_value = ("OK", [b"1"])
    mailbox.search.return_value = ("NO", [])
    monkeypatch.setattr(email.imaplib, "IMAP4_SSL", MagicMock(side_effect=[first, retry]))
    monkeypatch.setenv("PRIVATE_EMAIL_PASSWORD", "test-only")
    message = email.EmailMessage()
    message["Message-ID"] = "<test@example.test>"
    assert email.private_email_service._save_sent_copy(message) is False
    mailbox.append.assert_not_called()


def test_call_summary_receives_diagnostics_without_inventing_a_hangup_cause(monkeypatch):
    prompts = []

    def create(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return NS(choices=[NS(message=NS(content='{"stage":"new_lead","confidence":0.9}'))])

    monkeypatch.setattr(call_status, "AIServices", lambda: NS(openai_client=NS(chat=NS(completions=NS(create=create)))))
    call_status.classify({"messages": [{"role": "customer", "text": "What time do you open?"}],
                          "voice_turn_timings": [{"interrupted": True}],
                          "provider_metadata": {"loop_lag_max_ms": 1175}}, "new_lead")
    assert "interrupted reply attempts=1" in prompts[0]
    assert "maximum audio-loop delay ms=1175" in prompts[0]
    assert "does not establish who hung up" in prompts[0]
    assert "No transcribed caller speech does not prove the caller was silent" in prompts[0]
