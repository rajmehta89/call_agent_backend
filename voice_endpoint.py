"""Endpoint measurements from provider audio timestamps, separate from local handling."""

import math


def _number(value):
    if not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def flux_endpoint_timing(payload, audio_received_seconds, time_offset=0.0):
    result = {"turn_detection_source": "flux_audio_clock"}
    window = _number(payload.get("audio_window_end"))
    ends = [_number(word.get("end")) for word in payload.get("words") or [] if isinstance(word, dict)]
    ends = [end for end in ends if end is not None]
    if window is not None:
        result["stt_audio_window_end_ms"] = round((window + time_offset) * 1000)
        # Transport, queued provider messages and local reader delay can all
        # contribute here. This field does not attribute that delay to Flux.
        result["stt_delivery_backlog_ms"] = round(max(0.0, audio_received_seconds - time_offset - window) * 1000)
        if ends and window >= max(ends):
            result["stt_provider_detection_ms"] = round((window - max(ends)) * 1000)
            result["stt_last_word_end_ms"] = round((max(ends) + time_offset) * 1000)
    confidence = _number(payload.get("end_of_turn_confidence"))
    if confidence is not None and confidence <= 1:
        result["stt_end_of_turn_confidence"] = confidence
    if isinstance(payload.get("turn_index"), int):
        result["stt_turn_index"] = payload["turn_index"]
    return result


def retell_endpoint_timing(transcript, call_started_clock, received_clock):
    result = {"turn_detection_source": "retell_word_clock_estimate"}
    for item in reversed(transcript if isinstance(transcript, list) else []):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        ends = [_number(word.get("end")) for word in item.get("words") or [] if isinstance(word, dict)]
        ends = [end for end in ends if end is not None]
        if ends:
            lag = received_clock - call_started_clock - max(ends)
            if lag >= 0:
                # Unlike the bounded bridge clock, diagnostics retain the full
                # observed lag. Retell does not give a separate provider EOT clock.
                result["speech_end_to_event_received_ms"] = round(lag * 1000)
            else:
                result["stt_clock_mismatch"] = True
        break
    return result
