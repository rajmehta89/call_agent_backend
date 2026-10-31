# Pipecat speech runtime and measurements

Pipecat 1.15.0 is the default STT/TTS runtime on the existing authenticated Twilio Media Streams route and is included
in the standard backend requirements. The earlier opt-in trial is recorded below.
It uses native Pipecat services and workers, while the existing conversation controller continues to own semantic
decisions, retained answers during connection checks, callback state, transfers, language switching, transcripts,
recording callbacks and Twilio playback marks. This is a speech-runtime integration, not a replacement of the CRM
or a migration to Pipecat Cloud.

## Activate and roll back

Install in the environment that runs the backend:

```powershell
python -m pip install -r requirements.txt
$env:CUSTOM_VOICE_RUNTIME = 'pipecat'
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

The standard Render and Docker builds install Pipecat through `requirements.txt`; `requirements-pipecat.txt` remains
a compatibility entry point. Render's blueprint and the deployment settings template select `CUSTOM_VOICE_RUNTIME=pipecat`.
An existing service with an explicit `CUSTOM_VOICE_RUNTIME=legacy` must change that override to enable it.
Existing speech-provider credentials and voice/model settings are reused. The Twilio
webhook URL and media route stay the same. No Pipecat Cloud account or credentials are required.

The default is now `pipecat` for all supported languages on the custom call stack. Setting `CUSTOM_VOICE_RUNTIME=legacy` and restarting
rolls back to the existing speech implementation. The selected runtime is fixed when each authenticated call starts.
If the package is missing, the version is unsupported, or the audio libraries cannot import, the call falls
back to legacy and records the reason. Provider errors after a Pipecat call starts retain the controller's existing
voice fallback and call-failure handling; they are not presented as successful Pipecat operation.

`GET /api/custom-voice/config` requires sign-in and reports runtime selection and package availability without
credentials. Saved calls record `provider_metadata.voice_runtime`, the package version, bounded speech metrics,
and a `voice_runtime` field on each turn timing. Existing provider and call IDs are preserved.
The public `/ready` response also reports runtime selection and installed Pipecat version, allowing deployment checks
without credentials. That reports the selected installation; saved call metadata confirms which runtime actually ran.

## Coverage across supported call languages

Pipecat is selected once per authenticated call, without a language-specific opt-in. Newly synthesized greetings,
answers, wait messages, connection checks and transfer announcements use that call's runtime. Already cached audio
continues to play directly. Language changes retain the same controller and use separate speech workers per language.
Retell-managed calls continue to use Retell's speech services; this runtime applies to the custom Twilio stack.

| App language | Recognition route | Voice route |
| --- | --- | --- |
| English | Flux; multilingual Flux when automatic language switching is enabled | Configured Deepgram English voice or ElevenLabs |
| Urdu, including Hindi/Hinglish callers | Nova when automatic switching is off; multilingual Flux when enabled, using its Hindi recognition coverage | Configured Deepgram Urdu Flux voice or ElevenLabs |
| Arabic | Nova; multilingual Nova when automatic switching is enabled | ElevenLabs unless a Deepgram Arabic voice is explicitly configured |

These are the app's three supported language settings. This does not add arbitrary new languages or change the
workspace's enabled-language switches, automatic-language setting, provider choice or voice/model configuration.
Flux STT does not officially list Arabic or Urdu: the existing Urdu/Hindi caller route uses Hindi coverage and was
exercised with synthetic Urdu questions. Recognition quality and accents still need real-call evaluation.

ElevenLabs v3/v4 models use native `ElevenLabsDialogueTTSService`; Flash/Turbo v2.5/Multilingual models use native
`ElevenLabsTTSService`. A live v4 probe rejected the ordinary TTS WebSocket with HTTP 400, exposing the protocol gap
before rollout. The dialogue service then completed Urdu and Arabic synthesis. Language hints are set only when the
configured model accepts them; unsupported hints are omitted rather than forcing a different model.

## What is implemented

- Native `DeepgramFluxSTTService` and `DeepgramSTTService`, with the same configured endpoint thresholds,
  recognition models, keyterms and language settings. Raw provider events and word clocks reach the shared
  controller, so Pipecat does not add a second set of conversational intent decisions.
- Persistent Pipecat Deepgram Aura, Deepgram Flux and ElevenLabs TTS/dialogue speech services. Urdu Flux uses the v2 service,
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

## Multilingual follow-up: ElevenLabs v4 Turbo

The dialogue service was exercised with the deployment template's `eleven_v4_turbo` model, three syntheses per
runtime/language, alternating order. The measurements below include leading silence and remain local speech-API
estimates, not real phone-call latency. The legacy HTTP synthesis also completed for this model.

| Language | Legacy first voiced estimate | Pipecat first voiced estimate | Median difference |
| --- | ---: | ---: | ---: |
| English | 335 ms | 221 ms | 114 ms lower |
| Urdu | 289 ms | 274 ms | 15 ms lower; little difference |
| Arabic | 293 ms | 257 ms | 36 ms lower |

Recognition remained variable. The v4-reference Urdu replay measured 988 ms legacy versus 3,394 ms Pipecat medians,
including two slow native trials; an isolated replay measured 3,552/937 ms medians in the opposite direction. This does not
establish a stable recognition saving or a reliable overall speedup. Arabic recognition medians were 1,149/1,106 ms.
English recognition medians were 1,087/886 ms, with a 2,540 ms native outlier. Preserve these observations when
comparing real calls; do not add speech-stage medians to promise total savings.

Earlier follow-up probes with the local default Flash v2.5 model also completed Urdu and Arabic: voiced estimates
were 216/223 ms and 233/203 ms respectively. Flash v2.5 does not accept an explicit Urdu language hint; those requests
used its existing automatic detection. These model runs must not be pooled with v4 samples.

Private reproducible reports: `logs/pipecat-all-languages-{en,ur,ar}-v4.json`, plus the isolated Urdu replay report.
No phone call was placed, and audio/transcripts have not been assessed by listening for pronunciation quality.

## Reproduce

```powershell
python scripts/benchmark_pipecat_speech.py --live --engine deepgram --language en --repeats 3 --stt flux --output logs/pipecat-en.json
python scripts/benchmark_pipecat_speech.py --live --engine deepgram --language ur --repeats 3 --stt flux --output logs/pipecat-ur.json
python scripts/benchmark_pipecat_speech.py --live --engine elevenlabs --language en --repeats 3 --output logs/pipecat-elevenlabs.json
python scripts/voice_latency_report.py --calls 20 --by-runtime
```

Use `--stt auto` to follow each language's existing recognition route instead of choosing Flux/Nova manually.
Reports include the actual TTS model, provider failures and warmup failures. Set `CUSTOM_AUTO_LANGUAGE=true` for the multilingual Flux comparison used above, or explicitly use the desired
configured Flux model. `--live` uses normal speech API usage. Reports and the synthetic reference audio are written
under ignored `logs/`. `--audio-input logs/pipecat-en.ulaw --stt flux` can replay saved 8 kHz mulaw without another
TTS request. Compare the same voices, questions and hosting region, with alternating run order.

## Validation and next measurements

Multilingual follow-up full suites: **771 passed** in the primary backend and **764 passed** in the mirror,
both with zero failures, errors or skipped tests (123.65/123.63 seconds). This includes multilingual shared-controller
tests, actual native service/model selection, language-specific voices, Arabic provider fallback, readiness reporting
and runtime rollback. Dependency resolution for the standard install also passed a dry run in the isolated environment.

Initial trial full suites with the runtime installed: **757 passed** in the primary backend (167 warnings,
141.19 seconds), and **750 passed** in the mirror (164 warnings, 141.12 seconds). The warnings are
framework/date deprecations. Native worker tests do not use real providers; separate live API probes above
exercise actual English/Urdu/Arabic speech services. CI also runs a focused native-worker and multilingual-controller
job separately from the full suite, whose legacy fixtures select their runtime explicitly.

The follow-up makes Pipecat part of the standard build and the default runtime. The initial trial did not change
production runtime settings. A successful deployment of this follow-up enables it unless an explicit legacy override
is present; verify `/ready` and saved call metadata before describing it as active. Provider credentials are unchanged.

The strongest observed speech-stage gain is English Deepgram. Urdu and steady-state ElevenLabs need optimization
elsewhere. The next useful checks are a real multi-turn phone call in the deployed region, the new handoff timing
breakdown, optional-lookup timeout counts, and concurrent-call queue/memory behaviour. Compare substantive answer
audio, interruptions and transfer correctness in real calls after deployment.

References: [Pipecat pipelines](https://docs.pipecat.ai/pipecat/learn/pipeline),
[Deepgram services](https://docs.pipecat.ai/api-reference/server/services/stt/deepgram),
[Pipecat metrics](https://docs.pipecat.ai/pipecat/fundamentals/metrics).
Language/model references: [Deepgram STT coverage](https://developers.deepgram.com/docs/models-languages-overview),
[ElevenLabs TTS and dialogue WebSockets](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/tts-vs-ttd-websockets).
