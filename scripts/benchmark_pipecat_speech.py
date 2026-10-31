"""Compare legacy and Pipecat speech with identical provider settings and synthetic questions.

Requires requirements-pipecat.txt. --live uses the configured speech APIs (normal usage charges), never places
a phone call, never imports the application database client with a URI, and writes only an ignored local report.
STT replay is paced at 8 kHz, including trailing silence; these are speech-stage measurements, not full call latency.
"""

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import statistics
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["MONGO_URI"] = ""  # this diagnostic cannot create indexes, call records, or CRM activities
from env_loader import load_project_env
load_project_env()


async def measure_tts(source):
    started = perf_counter()
    first = None
    audio = bytearray()
    async with contextlib.aclosing(source) as stream:
        async for chunk in stream:
            if chunk and first is None:
                first = round((perf_counter() - started) * 1000)
            audio.extend(chunk)
    if not audio:
        raise RuntimeError("Speech provider returned no audio")
    import numpy as np
    from voice_audio import ulaw_to_pcm
    samples = ulaw_to_pcm(bytes(audio)).astype(np.float64)
    complete_windows = len(samples) // 80  # 10 ms windows, same threshold for both runtimes
    rms = np.sqrt(np.mean(samples[:complete_windows * 80].reshape(-1, 80) ** 2, axis=1))
    voiced = np.flatnonzero(rms > 300)
    result = {"first_audio_ms": first, "all_audio_received_ms": round((perf_counter() - started) * 1000),
              "audio_seconds": round(len(audio) / 8000, 2)}
    if len(voiced):
        result["leading_silence_ms"] = int(voiced[0]) * 10
        result["first_voiced_playback_estimate_ms"] = first + result["leading_silence_ms"]
    return result, bytes(audio)


async def measure_stt(connection, audio):
    started = perf_counter()
    padded = audio + b"\xff" * 24000

    async def send():
        for offset in range(0, len(padded), 160):
            await asyncio.sleep(max(0, offset / 8000 - (perf_counter() - started)))
            await connection.send(padded[offset:offset + 160])

    sender = asyncio.create_task(send())
    latest_final = None
    try:
        async with asyncio.timeout(len(padded) / 8000 + 8):
            async for raw in connection:
                event = json.loads(raw)
                if event.get("type") in {"Error", "FatalError"}:
                    raise RuntimeError("Speech recognition rejected the replay: " + str(event.get("code") or "provider error"))
                if event.get("event") == "EndOfTurn":
                    transcript, words = event.get("transcript"), event.get("words") or []
                elif event.get("type") == "Results":
                    best = (event.get("channel", {}).get("alternatives") or [{}])[0]
                    transcript, words = best.get("transcript"), best.get("words") or []
                    if event.get("is_final") and transcript:
                        latest_final = (transcript, words)
                    if not event.get("speech_final"):
                        continue
                elif event.get("type") == "UtteranceEnd" and latest_final:
                    transcript, words = latest_final
                else:
                    continue
                if not transcript:
                    continue
                elapsed = (perf_counter() - started) * 1000
                word_ends = [word["end"] for word in words if isinstance(word.get("end"), (int, float))]
                result = {"transcript": transcript, "event_received_ms": round(elapsed),
                          "endpoint_event": event.get("event") or event.get("type")}
                if word_ends:
                    result["speech_end_to_event_ms"] = round(max(0, elapsed - max(word_ends) * 1000))
                return result
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
    raise RuntimeError("No completed transcript arrived")


async def benchmark(args):
    from routers import custom_twilio_voice_api as legacy
    from pipecat_voice import PipecatVoiceRuntime
    import websockets

    phrases = {"en": "Could you tell me what bathroom products you sell?",
               "ur": "کیا آپ بتا سکتے ہیں کہ باتھ روم کے لیے کون سی چیزیں دستیاب ہیں؟",
               "ar": "هل يمكنك إخباري بمنتجات الحمام المتوفرة لديكم؟"}
    question = args.text or phrases[args.language]
    engine = legacy._tts_engine_for_language(args.engine, args.language)
    runtime = PipecatVoiceRuntime()
    old_speaker = legacy._DeepgramSpeaker(args.language) if engine == "deepgram" else None
    rows = []
    reference = args.audio_input.read_bytes() if args.audio_input else None
    warm = {}
    try:
        for name in (() if args.audio_input else ("legacy", "pipecat")):
            started = perf_counter()
            if name == "pipecat":
                await runtime.warm(engine, args.language)
            elif old_speaker:
                await old_speaker.warm()
            else:
                await legacy._warm_tts_connection(engine)
            warm[name] = round((perf_counter() - started) * 1000)
        for repeat in range(0 if args.audio_input else args.repeats):
            # Alternate order to reduce systematic provider/time-of-run bias.
            for name in (("legacy", "pipecat") if repeat % 2 == 0 else ("pipecat", "legacy")):
                source = (runtime.tts_stream(question, engine, args.language) if name == "pipecat" else
                          old_speaker.stream(question) if old_speaker else legacy._tts_stream(question, engine, args.language))
                metrics, audio = await measure_tts(source)
                reference = reference or audio
                if not args.audio_input:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.with_suffix(".ulaw").write_bytes(reference)
                row = {"stage": "tts", "runtime": name, "repeat": repeat + 1, **metrics}
                rows.append(row)
                print(json.dumps(row), flush=True)
        if args.stt:
            flux = args.stt == "flux"
            url = legacy._flux_url(args.language) if flux else legacy._deepgram_url(args.language)
            for repeat in range(args.repeats):
                for name in (("legacy", "pipecat") if repeat % 2 == 0 else ("pipecat", "legacy")):
                    started = perf_counter()
                    connection = (await runtime.connect_stt(url, os.environ["DEEPGRAM_API_KEY"]) if name == "pipecat" else
                                  await websockets.connect(url, additional_headers={"Authorization": "Token " + os.environ["DEEPGRAM_API_KEY"]}, open_timeout=12))
                    connect_ms = round((perf_counter() - started) * 1000)
                    try:
                        row = {"stage": "stt", "runtime": name, "repeat": repeat + 1, "connect_ms": connect_ms}
                        try:
                            row.update(await measure_stt(connection, reference))
                        except (RuntimeError, TimeoutError) as error:
                            row["error"] = type(error).__name__
                            row["error_detail"] = str(error) if isinstance(error, RuntimeError) else "No completed transcript arrived"
                        rows.append(row)
                        print(json.dumps(row, ensure_ascii=True), flush=True)
                    finally:
                        await connection.close()
        summaries = []
        for stage, metric in (("tts", "first_audio_ms"), ("tts", "first_voiced_playback_estimate_ms"),
                              ("stt", "speech_end_to_event_ms")):
            for name in ("legacy", "pipecat"):
                values = [row[metric] for row in rows if row["stage"] == stage and row["runtime"] == name and metric in row]
                if values:
                    summaries.append({"stage": stage, "runtime": name, "metric": metric,
                                      "n": len(values), "median_ms": statistics.median(values), "max_ms": max(values)})
        report = {"scope": "Speech APIs only; excludes CRM, semantic routing, LLM, Twilio and phone playback",
                  "language": args.language, "engine": engine, "repeats": args.repeats,
                  "tts_connection_warmup_ms": warm, "stt": args.stt,
                  "summaries": summaries, "results": rows}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"summaries": summaries, "report": str(args.output)}), flush=True)
    finally:
        await runtime.close()
        if old_speaker:
            await old_speaker.close()
        await legacy._reset_elevenlabs_client()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="Use configured speech providers; no phone calls or CRM writes")
    parser.add_argument("--engine", choices=["deepgram", "elevenlabs"], default="deepgram")
    parser.add_argument("--language", choices=["en", "ur", "ar"], default="en")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--stt", choices=["flux", "nova"])
    parser.add_argument("--text")
    parser.add_argument("--audio-input", type=Path, help="Replay existing synthetic 8 kHz mulaw audio; skip TTS")
    parser.add_argument("--output", type=Path, default=Path("logs/pipecat-speech-benchmark.json"))
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to measure configured speech APIs (normal provider usage)")
    if not 1 <= args.repeats <= 10:
        parser.error("Choose between one and ten repeats")
    if args.audio_input and not args.stt:
        parser.error("--audio-input requires --stt")
    asyncio.run(benchmark(args))


if __name__ == "__main__":
    main()
