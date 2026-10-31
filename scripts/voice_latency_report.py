"""p50 / p95 voice latency from the per-turn timings saved on real calls (calls.voice_turn_timings).

Read-only: no call is placed, no provider is asked anything, nothing is written.

    python scripts/voice_latency_report.py              # the latest custom-voice call
    python scripts/voice_latency_report.py --calls 10   # the latest 10 calls together
"""

import argparse
import math
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from env_loader import load_project_env

# Stages in the order they happen. They overlap (the reply starts before the turn ends, a filler covers the wait), so
# they are reported side by side and never added up.
STAGES = (
    ("turn_detection_ms", "end of speech detected"),
    ("stt_provider_detection_ms", "STT provider endpoint"),
    ("stt_delivery_backlog_ms", "STT delivery/backlog estimate"),
    ("semantic_checks_ms", "turn checks"),
    ("prompt_knowledge_work_ms", "knowledge work"),
    ("prompt_store_work_ms", "store work"),
    ("prompt_store_queue_ms", "store pool queue"),
    ("prompt_caller_work_ms", "caller context work"),
    ("prompt_caller_queue_ms", "caller context pool queue"),
    ("preparation_ms", "prompt ready (checks + facts)"),
    ("model_wait_ms", "LLM first words after prompt"),
    ("first_sentence_ms", "first speakable sentence"),
    ("tts_first_audio_ms", "TTS first audio chunk"),
    ("first_audio_ms", "turn end -> reply audio (no filler)"),
    ("first_audible_ms", "turn end -> first audio (may be filler)"),
    ("speech_end_to_reply_audio_ms", "caller stops -> reply audio (no filler)"),
    ("speech_end_to_first_audible_ms", "caller stops -> first audio (may be filler)"),
)


def measured(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def turn_measurements(turn):
    result = dict(turn)
    # Custom voice records when reply audio is handed to Twilio. Never substitute the filler clock, or infer
    # audio delivery for Retell (whose TTS/playback is outside this backend).
    if (turn.get("pipeline") == "custom_twilio" and measured(turn.get("turn_detection_ms"))
            and measured(turn.get("first_audio_ms"))):
        result["speech_end_to_reply_audio_ms"] = turn["turn_detection_ms"] + turn["first_audio_ms"]
    return result


def percentile(values, share):
    ordered = sorted(values)
    if not ordered:
        return None
    rank = share * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def report(turns):
    turns = [turn_measurements(turn) for turn in turns]
    print(f"{'stage':44} {'n':>3} {'p50':>7} {'p95':>7} {'max':>7}")
    for key, label in STAGES:
        values = [turn[key] for turn in turns if measured(turn.get(key))]
        if values:
            print(f"{label:44} {len(values):>3} {percentile(values, .5):>7.0f} {percentile(values, .95):>7.0f} {max(values):>7.0f}")
    routes = {}
    for turn in turns:
        route = turn.get("model_route") or turn.get("action") or "unrecorded"
        routes[route] = routes.get(route, 0) + 1
    print("routes:", ", ".join(f"{name} {count}" for name, count in sorted(routes.items())))
    print("filler played:", sum(1 for turn in turns if turn.get("filler_played") or "bridge_first_audio_ms" in turn),
          "| interrupted:", sum(1 for turn in turns if turn.get("interrupted")), "| turns:", len(turns))
    print("Milliseconds; overlapping stages are not additive. Audio is sent to the provider, not measured at the phone.")
    print("Per-operation prompt work/queue times require schema 2; older cumulative checkpoints are not work durations.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calls", type=int, default=1, help="how many of the latest calls to include")
    args = parser.parse_args()
    load_project_env()
    uri = os.getenv("MONGO_URI")
    if not uri:
        sys.exit("MONGO_URI is not configured")
    # Importing the application MongoDB singleton creates indexes. This diagnostic must only read.
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
    try:
        with MongoClient(uri, serverSelectionTimeoutMS=5000, socketTimeoutMS=15000) as client:
            calls = list(client[os.getenv("MONGO_DB") or "ai_agent_assist"].calls.find(
                {"voice_turn_timings.0": {"$exists": True}},
                {"voice_turn_timings": 1, "call_date": 1, "created_at": 1, "duration": 1})
                .sort([("call_date", -1), ("_id", -1)]).limit(max(1, args.calls)))
    except PyMongoError as error:
        sys.exit(f"MongoDB read failed ({type(error).__name__})")
    if not calls:
        sys.exit("no call with saved voice timings")
    for call in calls:
        print(f"call {call['_id']}  {call.get('call_date') or call.get('created_at')} UTC  {call.get('duration') or '?'} s  {len(call['voice_turn_timings'])} turns")
    report([turn for call in calls for turn in call["voice_turn_timings"]])


if __name__ == "__main__":
    main()
