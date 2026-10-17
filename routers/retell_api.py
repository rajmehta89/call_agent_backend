"""Retell Custom LLM bridge backed by the existing OpenRouter configuration.

Retell owns telephony, speech recognition, voice synthesis, and turn taking.
This router owns the reasoning layer and returns short, customer-ready responses
from the configured OpenRouter model (gpt-oss-20b by default).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect

from ai_services import AIServices
from brain_service import brain_service
from qa_engine import DynamicQA
from routers.calls_api import log_call
from transfer_service import transfer_service
from voice_provider_settings import provider_enabled


router = APIRouter(prefix="/api/retell", tags=["retell"])


def _configure_retell_logger() -> logging.Logger:
    """Write a durable, per-integration trace without relying on Uvicorn stdout."""
    logger = logging.getLogger("retell.integration")
    if logger.handlers:
        return logger
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "retell-debug.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    return logger


retell_logger = _configure_retell_logger()


def _trace(message: str, *, error: bool = False) -> None:
    """Mirror useful call diagnostics to the console and logs/retell-debug.log."""
    (retell_logger.error if error else retell_logger.info)(message)
    print(message)


def _enabled() -> bool:
    return provider_enabled("retell")


def _max_response_tokens() -> int:
    try:
        return max(40, min(240, int(os.getenv("RETELL_MAX_RESPONSE_TOKENS", "120"))))
    except ValueError:
        return 120


def _serialize_messages(messages: List[Dict[str, str]], message_type: str) -> List[Dict[str, str]]:
    return [
        {"content": item["content"], "timestamp": item["timestamp"], "type": message_type}
        for item in messages
        if item.get("content")
    ]


def _transcript_as_messages(transcript: Any) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    for item in transcript or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        role = str(item.get("role") or "").lower()
        if content and role in {"user", "agent", "assistant"}:
            messages.append({"role": "assistant" if role in {"agent", "assistant"} else "user", "content": content})
    return messages[-10:]


def _last_user_message(messages: List[Dict[str, str]]) -> str:
    for item in reversed(messages):
        if item["role"] == "user":
            return item["content"]
    return ""


def _log_content_enabled() -> bool:
    return os.getenv("RETELL_LOG_CONTENT", "false").strip().lower() in {"1", "true", "yes", "on"}


def _parse_retell_signature(signature: str) -> Optional[tuple[str, str]]:
    match = re.fullmatch(r"v=(\d+),d=([0-9a-fA-F]+)", signature.strip())
    return (match.group(1), match.group(2)) if match else None


def _valid_retell_webhook(raw_body: bytes, signature: str) -> bool:
    """Verify Retell's raw-body HMAC and reject stale replay attempts."""
    api_key = os.getenv("RETELL_API_KEY", "").strip()
    parsed = _parse_retell_signature(signature)
    if not api_key or not parsed:
        return False
    timestamp, supplied_digest = parsed
    try:
        if abs(int(datetime.now().timestamp() * 1000) - int(timestamp)) > 5 * 60 * 1000:
            return False
    except ValueError:
        return False
    expected_digest = hmac.new(
        api_key.encode("utf-8"),
        raw_body + timestamp.encode("utf-8"),
        "sha256",
    ).hexdigest()
    return hmac.compare_digest(expected_digest, supplied_digest)


def _timestamp_from_ms(value: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(value) / 1000).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _duration_seconds(call: Dict[str, Any]) -> float:
    try:
        if call.get("duration_ms") is not None:
            return max(float(call["duration_ms"]) / 1000, 0)
        return max((float(call.get("end_timestamp", 0)) - float(call.get("start_timestamp", 0))) / 1000, 0)
    except (TypeError, ValueError):
        return 0


def _retell_messages(call: Dict[str, Any]) -> tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    callers: List[Dict[str, str]] = []
    agents: List[Dict[str, str]] = []
    started_at = call.get("start_timestamp")
    for item in call.get("transcript_object") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").lower()
        content = str(item.get("content") or "").strip()
        if not content or role not in {"user", "agent", "assistant"}:
            continue
        timestamp = _timestamp_from_ms(float(started_at) + float(item.get("time_sec", 0)) * 1000) if started_at else None
        message = {"content": content, "timestamp": timestamp or datetime.now().isoformat(), "type": "user" if role == "user" else "bot"}
        (callers if role == "user" else agents).append(message)
    return callers, agents


def _retell_human_request(call: Dict[str, Any], event: str) -> bool:
    analysis = call.get("call_analysis") or {}
    custom = analysis.get("custom_analysis_data") or {}
    requested_keys = {"human_agent_requested", "human_follow_up_required", "callback_requested", "needs_human"}
    return event.startswith("transfer_") or any(bool(custom.get(key)) for key in requested_keys)


def _upsert_retell_call(call: Dict[str, Any], event: str) -> None:
    call_id = str(call.get("call_id") or "").strip()
    if not call_id:
        raise ValueError("Webhook payload did not include call.call_id")

    analysis = call.get("call_analysis") or {}
    callers, agents = _retell_messages(call)
    direction = str(call.get("direction") or "inbound").lower()
    phone_number = str(
        (call.get("from_number") if direction == "inbound" else call.get("to_number"))
        or "Web caller"
    )
    handoff = transfer_service.get_call(call_id) or {}
    human_requested = bool(handoff.get("transfer_requested")) or _retell_human_request(call, event)
    transfer_status = handoff.get("transfer_status") or ("requested" if human_requested else None)
    call_successful = analysis.get("call_successful")
    outcome_status = "transfer_requested" if human_requested and transfer_status not in {"completed", "accepted"} else "completed"
    if str(call.get("call_status", "")).lower() == "error":
        outcome_status = "failed"

    latency = call.get("latency") or {}
    e2e = latency.get("e2e") or {}
    data = {
        "direction": direction,
        "status": outcome_status,
        "duration": _duration_seconds(call),
        "transcription": callers,
        "ai_responses": agents,
        "summary": analysis.get("call_summary") or "Retell call completed.",
        "sentiment": analysis.get("user_sentiment", "neutral"),
        "transfer_requested": human_requested,
        "transfer_status": transfer_status,
        "transfer_destination": handoff.get("transfer_destination") or call.get("transfer_destination") or ("Callback queue" if human_requested else None),
        "transfer_error": handoff.get("transfer_error"),
        "transfer_succeeded": transfer_status in {"connected", "completed", "accepted"},
        "accepted_by": handoff.get("accepted_by"),
        "handled_by": handoff.get("handled_by"),
        "call_session_id": call_id,
        "provider": "retell",
        "provider_call_id": call_id,
        "channel_type": call.get("call_type", "phone_call"),
        "agent_id": call.get("agent_id"),
        "agent_name": call.get("agent_name"),
        "agent_version": call.get("agent_version"),
        "agent_tag": call.get("agent_tag"),
        "to_number": call.get("to_number"),
        "provider_call_status": call.get("call_status"),
        "disconnection_reason": call.get("disconnection_reason"),
        "recording_url": call.get("recording_url") or call.get("scrubbed_recording_url"),
        "recording_multi_channel_url": call.get("recording_multi_channel_url") or call.get("scrubbed_recording_multi_channel_url"),
        "public_log_url": call.get("public_log_url"),
        "call_cost": call.get("call_cost"),
        "latency": latency,
        "end_to_end_latency_ms": e2e.get("p50") or e2e.get("average") or e2e.get("mean"),
        "provider_metadata": call.get("metadata") or {},
        "dynamic_variables": call.get("collected_dynamic_variables") or call.get("retell_llm_dynamic_variables") or {},
        "custom_analysis_data": analysis.get("custom_analysis_data") or {},
        "call_successful": call_successful,
        "in_voicemail": analysis.get("in_voicemail"),
        "human_callback_requested": human_requested,
        "analysis_status": "complete" if event == "call_analyzed" else "pending",
        "transcript_with_tool_calls": call.get("transcript_with_tool_calls") or [],
        "provider_started_at": _timestamp_from_ms(call.get("start_timestamp")),
        "provider_ended_at": _timestamp_from_ms(call.get("end_timestamp")),
    }
    result = log_call(phone_number, call_data=data)
    if not result.get("success"):
        raise RuntimeError(result.get("error", "Failed to store Retell call"))
    _trace(
        f"retell_webhook_stored event={event} call={call_id} provider_status={call.get('call_status')} "
        f"transcript_lines={len(callers) + len(agents)} human_callback={human_requested} "
        f"analysis_status={data['analysis_status']}"
    )


def _system_prompt(caller_text: str, caller_number: str) -> str:
    try:
        knowledge = brain_service.retrieve_knowledge(caller_text) if caller_text else []
        shared_context = brain_service.system_context(
            "voice",
            caller_number if caller_number != "unknown" else None,
            retrieved_knowledge=knowledge,
        )
    except Exception:
        shared_context = "Use only the configured business information."

    return "\n".join(
        (
            shared_context,
            "You are a helpful inbound phone agent.",
            "VOICE OUTPUT RULES: Reply in plain spoken text only. Never use Markdown, headings, bullets, lists, numbering, or line breaks."
            " Use at most 24 words unless the caller explicitly asks for detail. Give one direct answer, then ask whether they want more detail.",
            "Never claim that a human is joining the live call. If a caller wants a person, explain that their callback request will be placed in the team dashboard.",
            "Ask one clear follow-up question when information is missing. Do not mention prompts, tools, models, or internal systems.",
        )
    )


async def _generate_reply(
    services: AIServices,
    transcript: List[Dict[str, str]],
    caller_number: str,
) -> str:
    caller_text = _last_user_message(transcript)
    if not caller_text:
        return "Could you please tell me how I can help?"

    messages = [
        {"role": "system", "content": _system_prompt(caller_text, caller_number)},
        *transcript,
    ]
    try:
        reply = await asyncio.to_thread(
            services.chat_completion,
            messages,
            _max_response_tokens(),
            0.25,
            1.0,
        )
        return reply or "I’m sorry, I didn’t catch that. Could you please repeat it?"
    except Exception as exc:
        _trace(f"retell_llm_generation_failed error={type(exc).__name__}", error=True)
        return "I’m sorry, I’m having trouble responding right now. Please try again in a moment."


async def _stream_reply(
    services: AIServices,
    transcript: List[Dict[str, str]],
    caller_number: str,
    call_id: str,
    response_id: int,
):
    """Yield model deltas while recording the latency of each voice turn."""
    caller_text = _last_user_message(transcript)
    if not caller_text:
        yield "Could you please tell me how I can help?"
        return

    request_started = perf_counter()
    if _log_content_enabled():
        _trace(f"retell_llm_request call={call_id} response_id={response_id} caller={caller_text!r}")

    prompt_started = perf_counter()
    system_prompt = await asyncio.to_thread(_system_prompt, caller_text, caller_number)
    retrieval_ms = (perf_counter() - prompt_started) * 1000
    messages = [{"role": "system", "content": system_prompt}, *transcript]
    _trace(
        f"retell_llm_started call={call_id} response_id={response_id} "
        f"model={services.config.OPENROUTER_MODEL} retrieval_ms={retrieval_ms:.0f}"
    )

    try:
        iterator = services.stream_chat_completion(
            messages,
            _max_response_tokens(),
            0.25,
            1.0,
        )
        first_token_logged = False
        while True:
            chunk = await asyncio.to_thread(next, iterator, None)
            if chunk is None:
                break
            if not first_token_logged:
                first_token_logged = True
                _trace(
                    f"retell_llm_first_token call={call_id} response_id={response_id} "
                    f"elapsed_ms={(perf_counter() - request_started) * 1000:.0f}"
                )
            yield chunk
        _trace(
            f"retell_llm_complete call={call_id} response_id={response_id} "
            f"elapsed_ms={(perf_counter() - request_started) * 1000:.0f}"
        )
    except Exception as exc:
        _trace(f"retell_llm_stream_failed call={call_id} response_id={response_id} error={type(exc).__name__}: {exc}", error=True)
        yield "I’m sorry, I’m having trouble responding right now. Please try again in a moment."


@router.get("/config")
async def retell_config_status() -> Dict[str, Any]:
    """Safe readiness data for the dashboard; secrets are never returned."""
    return {
        "custom_llm_enabled": _enabled(),
        "openrouter_configured": bool(os.getenv("OPENROUTER_API_KEY")),
        "model": os.getenv("RETELL_OPENROUTER_MODEL", "openai/gpt-oss-20b"),
        "websocket_path": "/api/retell/llm-websocket/{call_id}",
        "webhook_path": "/api/retell/webhook",
        "webhook_ready": bool(os.getenv("RETELL_API_KEY", "").strip()),
    }


@router.post("/webhook", status_code=204)
async def retell_webhook(request: Request) -> Response:
    """Persist Retell lifecycle, transcript, and post-call analysis events."""
    raw_body = await request.body()
    if not os.getenv("RETELL_API_KEY", "").strip():
        _trace("retell_webhook_unavailable reason=RETELL_API_KEY_missing", error=True)
        return Response(status_code=503, content="RETELL_API_KEY is not configured")
    if not _valid_retell_webhook(raw_body, request.headers.get("x-retell-signature", "")):
        _trace("retell_webhook_rejected reason=invalid_signature", error=True)
        return Response(status_code=401, content="Invalid Retell webhook signature")

    try:
        payload = json.loads(raw_body)
        event = str(payload.get("event") or "")
        call = payload.get("call")
        if not isinstance(call, dict):
            raise ValueError("Webhook payload did not include a call object")
        _trace(f"retell_webhook_received event={event or 'missing'} call={call.get('call_id') or 'missing'}")

        if event == "call_started":
            transfer_service.register_call(
                str(call.get("call_id") or ""),
                str(call.get("from_number") or "Web caller"),
                direction=str(call.get("direction") or "inbound"),
            )
        elif event in {
            "call_ended",
            "call_analyzed",
            "transcript_updated",
            "transfer_started",
            "transfer_bridged",
            "transfer_cancelled",
            "transfer_ended",
        }:
            _upsert_retell_call(call, event)
        else:
            _trace(f"retell_webhook_ignored event={event or 'missing'}")
        return Response(status_code=204)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _trace(f"retell_webhook_invalid_payload error={type(exc).__name__}: {exc}", error=True)
        return Response(status_code=400, content="Invalid Retell webhook payload")
    except Exception as exc:
        _trace(f"retell_webhook_processing_failed error={type(exc).__name__}: {exc}", error=True)
        return Response(status_code=500, content="Retell webhook processing failed")


@router.websocket("/llm-websocket/{call_id}")
async def retell_custom_llm_websocket(websocket: WebSocket, call_id: str) -> None:
    """Implement Retell's Custom LLM WebSocket protocol for one live call."""
    if not _enabled():
        await websocket.close(code=1008, reason="Retell custom LLM is disabled")
        return

    expected_secret = os.getenv("RETELL_CUSTOM_LLM_SECRET", "").strip()
    # Retell does not forward arbitrary auth headers for Custom LLM sockets.
    # Configure the secret in the Custom LLM URL instead:
    # wss://api.example.com/api/retell/llm-websocket?token=... .
    # Retell appends the call_id to the path before opening the socket.
    supplied_secret = websocket.query_params.get("token", "")
    if expected_secret and not hmac.compare_digest(supplied_secret, expected_secret):
        await websocket.close(code=1008, reason="Unauthorized")
        return

    await websocket.accept()
    _trace(f"retell_ws_connected call={call_id}")
    started_at = datetime.now()
    started_clock = perf_counter()
    caller_number = "unknown"
    callback_requested = False
    user_messages: List[Dict[str, str]] = []
    agent_messages: List[Dict[str, str]] = []
    services = AIServices()
    services.config.OPENROUTER_MODEL = os.getenv("RETELL_OPENROUTER_MODEL", "openai/gpt-oss-20b").strip()
    intent_analyzer = DynamicQA(services)

    if not services.is_llm_configured():
        await websocket.send_json(
            {
                "response_type": "response",
                "response_id": 0,
                "content": "I’m sorry, the AI service is not configured right now.",
                "content_complete": True,
                "end_call": True,
            }
        )
        await websocket.close(code=1011, reason="OpenRouter is not configured")
        return

    await websocket.send_json(
        {
            "response_type": "config",
            "config": {"auto_reconnect": True, "call_details": True},
        }
    )
    begin_message = os.getenv("RETELL_BEGIN_MESSAGE", "Hello, how can I help you today?").strip()
    await websocket.send_json(
        {
            "response_type": "response",
            "response_id": 0,
            "content": begin_message,
            "content_complete": True,
        }
    )
    if begin_message:
        agent_messages.append({"content": begin_message, "timestamp": datetime.now().isoformat()})

    # Retell pings every two seconds while a response is being generated. Keep
    # the receive loop free so it can return those pings and accept newer turns.
    send_lock = asyncio.Lock()
    response_tasks: set[asyncio.Task[None]] = set()
    latest_response_id = -1

    async def send_event(payload: Dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def handle_response(event: Dict[str, Any], response_id: int) -> None:
        nonlocal callback_requested, user_messages
        transcript = _transcript_as_messages(event.get("transcript"))
        caller_text = _last_user_message(transcript)
        now = datetime.now().isoformat()

        if caller_text:
            user_messages = [
                {"content": item["content"], "timestamp": now}
                for item in transcript
                if item["role"] == "user"
            ]
            transfer_service.append_message(call_id, "user", caller_text)

        streamed = False
        if event.get("interaction_type") == "reminder_required" and not caller_text:
            reply = "Is there anything else I can help you with?"
        elif caller_text and intent_analyzer.should_transfer_to_agent(caller_text):
            callback_requested = True
            transfer_service.request_transfer(
                call_id,
                "Caller requested a human callback",
                "retell_callback",
                0.95,
                phone_number=caller_number,
            )
            _trace(f"retell_human_callback_requested call={call_id} response_id={response_id}")
            reply = "I have marked your request for a human callback. Our team will contact you using this number as soon as possible."
        else:
            answer_parts: List[str] = []
            async for chunk in _stream_reply(services, transcript, caller_number, call_id, response_id):
                if response_id != latest_response_id:
                    _trace(f"retell_llm_stale_response call={call_id} response_id={response_id}")
                    return
                answer_parts.append(chunk)
                streamed = True
                await send_event(
                    {
                        "response_type": "response",
                        "response_id": response_id,
                        "content": chunk,
                        "content_complete": False,
                    }
                )
            reply = "".join(answer_parts).strip() or "I’m sorry, I didn’t catch that. Could you please repeat it?"

        if response_id != latest_response_id:
            _trace(f"retell_llm_stale_response call={call_id} response_id={response_id}")
            return

        agent_messages.append({"content": reply, "timestamp": datetime.now().isoformat()})
        transfer_service.append_message(call_id, "assistant", reply)
        if _log_content_enabled():
            _trace(f"retell_llm_answer call={call_id} response_id={response_id} answer={reply!r}")
        await send_event(
            {
                "response_type": "response",
                "response_id": response_id,
                "content": "" if streamed else reply,
                "content_complete": True,
            }
        )

    def consume_response_task(task: asyncio.Task[None]) -> None:
        response_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _trace(f"retell_llm_response_task_failed call={call_id} error={type(exc).__name__}: {exc}", error=True)

    try:
        while True:
            event = json.loads(await websocket.receive_text())
            interaction_type = event.get("interaction_type")
            transcript = event.get("transcript") or []
            _trace(
                f"retell_ws_event call={call_id} type={interaction_type or 'missing'} "
                f"response_id={event.get('response_id')} transcript_lines={len(transcript)}"
            )
            if _log_content_enabled() and transcript:
                _trace(
                    f"retell_ws_transcript call={call_id} type={interaction_type or 'missing'} "
                    f"data={json.dumps(transcript[-4:], ensure_ascii=False)}"
                )

            if interaction_type == "ping_pong":
                await send_event({"response_type": "ping_pong", "timestamp": event.get("timestamp")})
                continue

            if interaction_type == "call_details":
                call = event.get("call") or {}
                caller_number = str(call.get("from_number") or "unknown")
                transfer_service.register_call(call_id, caller_number, direction=call.get("direction") or "inbound")
                _trace(f"retell_call_details call={call_id} direction={call.get('direction') or 'inbound'} caller_known={caller_number != 'unknown'}")
                continue

            if interaction_type not in {"response_required", "reminder_required"}:
                if interaction_type not in {"update_only", "call_details"}:
                    _trace(f"retell_ws_ignored_event call={call_id} type={interaction_type or 'missing'}")
                continue

            response_id = event.get("response_id")
            if not isinstance(response_id, int):
                continue
            latest_response_id = response_id
            response_task = asyncio.create_task(handle_response(event, response_id))
            response_tasks.add(response_task)
            response_task.add_done_callback(consume_response_task)
            continue
    except WebSocketDisconnect as exc:
        _trace(f"retell_ws_closed call={call_id} code={exc.code}")
    except Exception as exc:
        _trace(f"retell_ws_error call={call_id} error={type(exc).__name__}: {exc}", error=True)
    finally:
        for response_task in response_tasks:
            response_task.cancel()
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        handoff = transfer_service.get_call(call_id) or {}
        transfer_service.close_call(call_id)
        if caller_number != "unknown" and (user_messages or agent_messages):
            duration = max((perf_counter() - started_clock), 0)
            transfer_status = handoff.get("transfer_status") or ("requested" if callback_requested else None)
            log_call(
                caller_number,
                call_data={
                    "direction": "inbound",
                    "status": "transfer_requested" if callback_requested else "completed",
                    "duration": duration,
                    "transcription": _serialize_messages(user_messages, "user"),
                    "ai_responses": _serialize_messages(agent_messages, "bot"),
                    "summary": (
                        "Human callback requested from the Retell AI call."
                        if callback_requested
                        else "Retell inbound AI call completed."
                    ),
                    "sentiment": "neutral",
                    "transfer_requested": callback_requested,
                    "transfer_status": transfer_status,
                    "transfer_destination": "Callback queue" if callback_requested else None,
                    "transfer_succeeded": False,
                    "call_session_id": call_id,
                    "provider": "retell",
                    "started_at": started_at.isoformat(),
                },
            )
            _trace(f"retell_ws_fallback_call_saved call={call_id} messages={len(user_messages) + len(agent_messages)} callback={callback_requested}")
        else:
            _trace(f"retell_ws_fallback_call_skipped call={call_id} caller_known={caller_number != 'unknown'} messages={len(user_messages) + len(agent_messages)}")
