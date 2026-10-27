"""p50 / p95 voice latency from the per-turn timings saved on real calls (calls.voice_turn_timings).

Read-only: no call is placed, no provider is asked anything, nothing is written.

    python scripts/voice_latency_report.py              # the latest custom-voice call
    python scripts/voice_latency_report.py --calls 10   # the latest 10 calls together
"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from env_loader import load_project_env

load_project_env()

from mongo_client import mongo_client

# Stages in the order they happen. They overlap (the reply starts before the turn ends, a filler covers the wait), so
# they are reported side by side and never added up.
STAGES = (
    ("turn_detection_ms", "end of speech detected"),
    ("semantic_checks_ms", "turn checks"),
    ("preparation_ms", "prompt ready (checks + facts)"),
    ("model_wait_ms", "LLM first words after prompt"),
    ("first_sentence_ms", "first speakable sentence"),
    ("tts_first_audio_ms", "TTS first audio chunk"),
    ("first_audible_ms", "turn end -> first audio (filler or answer)"),
    ("speech_end_to_first_audible_ms", "caller stops -> first audio"),
)


def percentile(values, share):
    ordered = sorted(values)
    if not ordered:
        return None
    rank = share * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def report(turns):
    print(f"{'stage':44} {'n':>3} {'p50':>7} {'p95':>7} {'max':>7}")
    for key, label in STAGES:
        values = [turn[key] for turn in turns if isinstance(turn.get(key), (int, float))]
        if values:
            print(f"{label:44} {len(values):>3} {percentile(values, .5):>7.0f} {percentile(values, .95):>7.0f} {max(values):>7.0f}")
    routes = {}
    for turn in turns:
        route = turn.get("model_route") or turn.get("action") or "unrecorded"
        routes[route] = routes.get(route, 0) + 1
    print("routes:", ", ".join(f"{name} {count}" for name, count in sorted(routes.items())))
    print("filler played:", sum(1 for turn in turns if turn.get("filler_played") or "bridge_first_audio_ms" in turn),
          "| interrupted:", sum(1 for turn in turns if turn.get("interrupted")), "| turns:", len(turns))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calls", type=int, default=1, help="how many of the latest calls to include")
    args = parser.parse_args()
    if not mongo_client.is_connected():
        sys.exit("MongoDB is not connected")
    calls = list(mongo_client.db.calls.find({"voice_turn_timings.0": {"$exists": True}},
                                            {"voice_turn_timings": 1, "created_at": 1, "duration": 1})
                 .sort("created_at", -1).limit(max(1, args.calls)))
    if not calls:
        sys.exit("no call with saved voice timings")
    for call in calls:
        print(f"call {call['_id']}  {call.get('created_at')}  {call.get('duration') or '?'} s  {len(call['voice_turn_timings'])} turns")
    report([turn for call in calls for turn in call["voice_turn_timings"]])


if __name__ == "__main__":
    main()
