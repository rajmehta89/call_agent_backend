# Pipecat speech trial

Pipecat 1.15.0 is available as an optional STT/TTS runtime on the existing authenticated Twilio Media Streams route.
It uses native Pipecat services and workers, while the existing conversation controller continues to own semantic
decisions, retained answers during connection checks, callback state, transfers, language switching, transcripts,
recording callbacks and Twilio playback marks. This is a speech-runtime integration, not a replacement of the CRM
or a migration to Pipecat Cloud.

## Activate and roll back

Install in the environment that runs the backend:

```powershell
python -m pip install -r requirements-pipecat.txt
$env:CUSTOM_VOICE_RUNTIME = 'pipecat'
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

For a hosted deployment, install `requirements-pipecat.txt` in the build step and set `CUSTOM_VOICE_RUNTIME=pipecat`
on that backend service. Existing speech-provider credentials and voice/model settings are reused. The Twilio
webhook URL and media route stay the same. No Pipecat Cloud account or credentials are required.

The default remains `legacy` for a controlled comparison. Setting `CUSTOM_VOICE_RUNTIME=legacy` and restarting
rolls back to the existing speech implementation. The selected runtime is fixed when each authenticated call starts.
If the optional package is missing, the version is unsupported, or the audio libraries cannot import, the call falls
back to legacy and records the reason. Provider errors after a Pipecat call starts retain the controller's existing
voice fallback and call-failure handling; they are not presented as successful Pipecat operation.

`GET /api/custom-voice/config` requires sign-in and reports runtime selection and package availability without
credentials. Saved calls record `provider_metadata.voice_runtime`, the package version, bounded speech metrics,
and a `voice_runtime` field on each turn timing. Existing provider and call IDs are preserved.

## What is implemented

- Native `DeepgramFluxSTTService` and `DeepgramSTTService`, with the same configured endpoint thresholds,
  recognition models, keyterms and language settings. Raw provider events and word clocks reach the shared
  controller, so Pipecat does not add a second set of conversational intent decisions.
- Persistent Pipecat Deepgram Aura, Deepgram Flux and ElevenLabs speech services. Urdu Flux uses the v2 service,
  which waits for `SpeechMetadata`; its early `Flushed` event is not treated as the end of the audio.
- Explicit 8 kHz mono phone-audio conversion, bounded queues, serialized worker startup, and worker/connection
  disposal on interrupted speech. Old audio cannot be reused on the following turn.
- Existing first-audio deadlines, import warmup before readiness, and queued logging through the application logger.
  Pipecat debug transcript logging is disabled at the ordinary INFO level.
- Optional customer/store lookups share a budget measured from preparation start. For example, a simulated
  one-second knowledge read followed by stalled optional lookups previously allowed four seconds in total;
  the same scenario now stops waiting at 2.5 seconds, preserving the confirmed knowledge. This is a tested timeout
  bound, not a claimed 1.5-second improvement on every real call.
- Separate elapsed timings for contextual handoff checks, meaning preparation and intent checks, including their
  thread-pool wait. This makes the earlier 3.17-second transfer-routing delay diagnosable on subsequent calls.

## Live speech API measurements: 31 October 2026

These tests ran from the local machine with synthetic questions, existing speech accounts, and matching configured
voices/models. No phone call was placed and the diagnostic disables MongoDB before importing application modules.
They exclude CRM retrieval, semantic decisions, model inference, Twilio transport and actual phone playback.
Pipecat requires PCM internally; the legacy path sends phone mulaw directly. ElevenLabs also changes from the
existing streaming HTTP request to Pipecat's persistent WebSocket. Results therefore compare the implementations,
not the framework in isolation.

| Speech stage | Samples per runtime | Legacy median | Pipecat median | Interpretation |
| --- | ---: | ---: | ---: | --- |
| English Deepgram TTS, first audio chunk | 3 | 489 ms | 261 ms | 228 ms lower in this run |
| English Deepgram TTS, estimated first voiced playback | 3 | 509 ms | 287 ms | Leading silence included; 222 ms lower |
| English Flux STT, speech end to turn event | 3 | 495 ms | 420 ms | Small improvement in this sample |
| Urdu Flux TTS, first audio chunk | 3 | 516 ms | 499 ms | Little difference |
| Urdu Flux TTS, estimated first voiced playback | 3 | 572 ms | 579 ms | No demonstrated improvement |
| Urdu multilingual Flux STT, speech end to turn event | 3 | 1,054 ms | 1,054 ms | Same median |
| English ElevenLabs TTS, first audio chunk | 3 | 206 ms | 196 ms | Similar steady response time |
| Arabic ElevenLabs TTS, first audio chunk | 1 | 182 ms | 191 ms | Functional smoke check, not a speed conclusion |
| Arabic Nova STT, speech end to endpoint | 1 | 791 ms | 845 ms | Functional smoke check, not a speed conclusion |

An earlier English Deepgram run measured 594/321 ms for first audio, consistent with an improvement but showing
run-to-run variation. ElevenLabs' first legacy synthesis in its three-sample English run took 1,307 ms, versus
203 ms on the already-open Pipecat voice connection; subsequent legacy requests were 184–206 ms. Prewarming the
voice connection is useful, but that single cold request must not be substituted for a steady-state median.

The first Arabic replay attempt timed out on the legacy path before producing a complete report. A diagnostic
rerun completed on both paths. The replay tool now accepts a provider `UtteranceEnd` after final Nova words,
reports the boundary used, and records failed trials instead of discarding the entire report. Arabic needs more
samples before any performance recommendation.

The voiced-playback estimate adds the audio's leading silence, detected with a common 10 ms RMS threshold,
to first-chunk receipt. It is not a microphone measurement of what a caller hears. Three synthetic samples are
too few for reliable production p95/p99 estimates or a claim that full calls are now faster.

## Reproduce

```powershell
python scripts/benchmark_pipecat_speech.py --live --engine deepgram --language en --repeats 3 --stt flux --output logs/pipecat-en.json
python scripts/benchmark_pipecat_speech.py --live --engine deepgram --language ur --repeats 3 --stt flux --output logs/pipecat-ur.json
python scripts/benchmark_pipecat_speech.py --live --engine elevenlabs --language en --repeats 3 --output logs/pipecat-elevenlabs.json
python scripts/voice_latency_report.py --calls 20 --by-runtime
```

Set `CUSTOM_AUTO_LANGUAGE=true` for the multilingual Flux comparison used above, or explicitly use the desired
configured Flux model. `--live` uses normal speech API usage. Reports and the synthetic reference audio are written
under ignored `logs/`. `--audio-input logs/pipecat-en.ulaw --stt flux` can replay saved 8 kHz mulaw without another
TTS request. Compare the same voices, questions and hosting region, with alternating run order.

## Validation and next measurements

Final full suites with the optional runtime installed: **757 passed** in the primary backend (167 warnings,
141.19 seconds), and **750 passed** in the mirror (164 warnings, 141.12 seconds). The warnings are
framework/date deprecations. Native worker tests do not use real providers; separate live API probes above
exercise actual English/Urdu/Arabic speech services. CI has an optional-dependency job so those native worker tests
also run when the default backend environment has no Pipecat installed.

The code and this report are synchronized to both backend repositories. No production runtime switch, speech
provider settings or credentials were changed during this trial. Source publication alone does not activate
Pipecat: install the optional requirements in the deployed build and select the runtime explicitly as above.

The strongest observed speech-stage gain is English Deepgram. Urdu and steady-state ElevenLabs need optimization
elsewhere. The next useful checks are a real multi-turn phone call in the deployed region, the new handoff timing
breakdown, optional-lookup timeout counts, and concurrent-call queue/memory behaviour. Compare substantive answer
audio, interruptions and transfer correctness before selecting the runtime for all calls.

References: [Pipecat pipelines](https://docs.pipecat.ai/pipecat/learn/pipeline),
[Deepgram services](https://docs.pipecat.ai/api-reference/server/services/stt/deepgram),
[Pipecat metrics](https://docs.pipecat.ai/pipecat/fundamentals/metrics).
