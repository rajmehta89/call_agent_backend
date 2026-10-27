"""Measure the actual voice pipeline without placing a call or writing CRM data.

Run the same command locally and on the production server. --audio also
measures first audio from the configured TTS provider (normal API usage).
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from env_loader import load_project_env

load_project_env()

from ai_services import warm_model_connection
from brain_service import brain_service, _local_embedder
from meaning import Classifier
from routers.custom_twilio_voice_api import (_voice_services, _voice_sentences, _tts_stream,
                                             _warm_tts_connection, _DeepgramSpeaker,
                                             _deepgram_speaker_stream)
from routers.retell_api import _system_prompt
import turn_meaning


async def benchmark(args):
    services = _voice_services()
    history = [{"role": "assistant", "content": "Hello, how can I help you today?"}]
    questions = args.question or ["What is Home DFO?", "What products do you sell?", "Where is your store?"]
    warm_started = perf_counter()

    def warm():
        _local_embedder()
        brain_service.knowledge_text()
        _system_prompt("hello", "unknown")
        for classifier in vars(turn_meaning).values():
            if isinstance(classifier, Classifier):
                classifier.rank("hello")
        warm_model_connection(services)

    await asyncio.to_thread(warm)
    speaker = _DeepgramSpeaker() if args.audio and args.engine == "deepgram" else None
    if speaker is not None:
        await speaker.warm()
    elif args.audio:
        await _warm_tts_connection(args.engine)
    report = {"started_at": datetime.now(timezone.utc).isoformat(),
              "model": services.config.OPENROUTER_MODEL,
              "local_embeddings": _local_embedder() is not None,
              "warmup_ms": round((perf_counter() - warm_started) * 1000),
              "tts_engine": args.engine if args.audio else None,
              "tts_transport": "persistent_websocket" if speaker else "http" if args.audio else None,
              "scope": "Completed transcript to first generated sentence/audio; excludes speech recognition and phone network",
              "results": []}
    for repeat in range(args.repeats):
        for question in questions:
            started = perf_counter()
            timings = {}
            stream = _voice_sentences(services, question, history, "unknown", "", timings=timings).__aiter__()
            try:
                sentence = await anext(stream)
                first_sentence = perf_counter() - started
                row = {"question": question, "repeat": repeat + 1,
                       "first_sentence_ms": round(first_sentence * 1000),
                       **{key: timings[key] for key in ("requested_model", "preparation_ms", "semantic_checks_ms",
                                                        "first_model_text_ms", "model_wait_ms") if key in timings}}
                if args.audio:
                    speech = (_deepgram_speaker_stream(speaker, sentence) if speaker else
                              _tts_stream(sentence, args.engine))
                    try:
                        audio = await anext(speech)
                        if not audio:
                            raise RuntimeError("TTS returned empty audio")
                        row["first_audio_ms"] = round((perf_counter() - started) * 1000)
                        if speaker is not None:
                            # Live calls finish the utterance and reuse this connection.
                            # Closing at the first byte would discard it between trials.
                            async for _ in speech:
                                pass
                    finally:
                        await speech.aclose()
                report["results"].append(row)
                print(json.dumps(row), flush=True)
            finally:
                await stream.aclose()
    if speaker is not None:
        await speaker.close()
    field = "first_audio_ms" if args.audio else "first_sentence_ms"
    report["median_ms"] = round(statistics.median(row[field] for row in report["results"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"median_ms": report["median_ms"], "metric": field, "report": str(args.output)}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", action="store_true")
    parser.add_argument("--engine", choices=["elevenlabs", "deepgram"], default="elevenlabs")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--question", action="append")
    parser.add_argument("--output", type=Path, default=Path("logs/voice_latency_benchmark.json"))
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("Select between one and ten repeats")
    asyncio.run(benchmark(args))


if __name__ == "__main__":
    main()
