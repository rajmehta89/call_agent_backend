# Voice endpoint and latency review, October 30

This review covers both the custom Twilio voice stack and the Retell custom-LLM bridge. Conversation decisions continue to use semantic meaning and conversation state.

## Latest production evidence

There are now 37 stored calls, two more than the full-history review. Read-only Twilio GETs for both new calls returned `completed`, with no provider notification errors. The latest call started at approximately 10:58:47 IST, lasted 102 seconds, and used custom Flux recognition with Deepgram voice generation. Its media handler received `twilio_stop` without a recorded stream exception. These records do not identify which party ended the call.

The running service reported ready and warm at revision `c2aec681ffad`, the previously pushed mirror revision. The following measurements therefore describe that deployment, before this review's new changes:

| Latest call business turn | Existing aggregate end-detection lag | Handler start to answer audio | Speech end to substantive answer |
| --- | ---: | ---: | ---: |
| 1 | 1,240 ms | 2,533 ms | 3,773 ms |
| 2 | 1,380 ms | 5,793 ms | 7,173 ms |
| 3 | 3,140 ms | 3,875 ms | 7,015 ms |

Voice generation took 88-90 ms on these turns. Semantic preparation and model waiting were much larger contributors. A short wait bridge on turns 1 and 3 was audible sooner than the answer; it must not be counted as the substantive answer. The preceding 33-second call had one answer at approximately 1,967 ms after speech ended.

The latest call also unnecessarily offered a now-or-later human handoff when the caller was seeking a starting point for a newly purchased home. That is business guidance, without a clear request for a person. Existing records contain aggregate audio-clock lag rather than Flux's original endpoint window, so they cannot retrospectively separate provider detection from message delivery and local processing.

## Changes

1. **Custom voice reader:** Flux's eager-end event schedules speculative semantic work without awaiting it in the STT reader. A slow connection-check translation can no longer hold up receipt of the final end event on this path. Resumed speech cancels the pending early-start task and discards a stale draft. Draft audio still waits for the final turn decision. This follows [Flux's eager end-of-turn lifecycle](https://developers.deepgram.com/docs/flux/voice-agent-eager-eot).
2. **Both voice paths:** A separate translation prefetch starts after 250 ms of stable foreign-language words. It coalesces pending updates into the latest transcript and is independent of the heavier semantic worker's adaptive delay. Generic semantic probes translate the full original turn before replacing catalogue/business entities, avoiding a second translation of rewritten foreign text.
3. **Both voice paths:** Console/file logging uses queue listeners so sink flush and rotation run outside the voice event loop. Call context is captured before crossing threads. The Retell-specific trace file also uses a queue. This addresses a confirmed synchronous log-flush stall; it does not explain every observed SSL/CPU stall.
4. **Retell:** Final fallback call persistence runs in a worker thread rather than blocking the websocket loop.
5. **Shared semantic routing:** Ambiguous handoff timing requires a clear semantic request for a person. Requests for help starting a home project continue to normal business guidance. Existing offer acceptance and explicit live/callback handling remain covered by regression tests. No keyword or phrase intent rules were added.

## New measurements

Custom Flux records the provider's source-audio window, last-word end, endpoint confidence and turn index when supplied. `stt_provider_detection_ms` measures the source-audio interval between the last word and the endpoint window. `stt_delivery_backlog_ms` separately estimates the remaining received-audio backlog, including transport, queued messages and local reader delay. It is not attributed solely to Flux. The event receipt timestamp and delay before local turn handling are recorded separately. Missing source timestamps remain unknown. Field definitions follow the [Flux streaming reference](https://developers.deepgram.com/reference/speech-to-text/listen-flux?explorer=true).

Retell records the full estimated interval from the latest user-word timestamp to receipt of its response request, plus receipt time and local handling delay. The old 1.5-second cap is retained only for the wait-bridge clock; it no longer hides larger diagnostic delays. Retell does not provide a separate provider endpoint clock through this bridge, so this measurement is labelled an estimate and includes clock-alignment limitations. It is not a measurement of Retell's TTS or actual phone playback.

No live endpoint thresholds, voice/model providers or production routing settings were changed. [Flux's end threshold and silence timeout](https://developers.deepgram.com/docs/flux/configuration) affect interruption/latency tradeoffs; the new measurements should guide any later tuning.

## Validation

Regression coverage includes a blocked eager semantic check while the final Flux event arrives, provider-versus-backlog clocks, reconnect offsets, missing timestamps, unclipped Retell timing saved through its real websocket handler, threaded persistence, a blocked logging sink with a responsive event loop, translation coalescing/reuse, and semantic home-guidance routing. Providers, calls, storage and mail are faked in these tests.

Final full suites passed: **697 tests in the primary backend and 690 in the mirror**. A focused routing/endpoint run also passed 90 tests. The changed source, tests and this report match across both backend copies after normalizing line endings; the mirror's existing email/discovery differences are preserved. The frontend remains unchanged and its local revision matches the remote.

A fresh production call after deployment is still required to quantify the speed improvement; pre-change call measurements cannot establish the new latency. Source push and live deployment revision must be verified separately.
