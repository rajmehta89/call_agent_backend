# First-reply and conversation latency: Retell and custom Deepgram

## Latest production baseline

The call beginning 26 October 2026 at 10:59:50 IST used Retell agent version 25.
Production health now confirms `2026-10-26-voice-latency-2` is live.

| Reply | Spoken response latency | Backend/LLM |
|---|---:|---:|
| First question about Home DFO | 5.237 s | 3.902 s |
| Matte black basin | 3.012 s | 1.407 s |
| Basin details and size | 4.896 s | 3.077 s |
| Offer accepted | 3.768 s | 2.572 s |
| Caller name | 3.102 s | 1.938 s |
| Closing | 3.203 s | 1.926 s |

Median spoken response latency was 3.486 seconds, compared with 6.087 seconds
in the previous measured call. These calls contain different questions and
are observations, not a controlled before/after benchmark. Network RTT remained
17 ms. The first greeting started about 0.98 seconds into the call; the longer
first *answer* remains the optimization target.

The first question's transcript ended `is for—`. This code's default unfinished
sentence safeguard waits 0.7 seconds; previously that wait was added after
semantic preparation. The production setting and internal stage split were not
available in the provider trace.

## Changes in release 3

- Both pipelines prepare stable partial transcripts in the background, after
  250 ms without a change. One worker retains only the newest pending text.
  Prefetch never sends speech, updates CRM records or initiates transfers.
- Both pipelines count preparation time toward the unfinished-sentence pause,
  retaining only the remaining wait. Continued speech still cancels stale replies.
- Question knowledge retrieval overlaps independent model, contact and place
  checks. Prepared facts are reused only when the selected reply needs them.
- Custom ElevenLabs and Deepgram calls also batch semantic inputs and move
  model selection, contact checks, place checks and bridge selection off the
  audio event loop. Unknown coverage still uses the careful model, invalid
  contact details still require verification, and the selected language is retained.
- Greeting-time warm-up prepares the previous agent utterance's semantic inputs.
- Completed replies retain operational `voice_turn_timings` on the call record.
  Retell stores semantic preparation, prompt preparation and model-wait timing;
  custom calls store first sentence and first audio timing plus the speech engine.
  These backend timings exclude speech recognition and phone transport. Provider
  end-to-end audio timing remains a separate metric.

## Local verification

The backend suite passed, including interruption, Nova/Flux early-start,
transfer, contact validation, multilingual prompt and concurrent preparation
checks. A real custom Deepgram test used the same persistent WebSocket speaker
as live custom calls. Four trials measured first audio at 4.316, 2.728, 2.189 and
2.043 seconds, median 2.458 seconds. The warmed second pass was 2.043-2.189
seconds. These measurements exclude recognition and phone transport and do not
establish production performance.

The earlier HTTP speech benchmark used a separate request path and showed higher
first-audio latency. The benchmark script now uses and drains the persistent
Deepgram speaker, preserving its connection between trials as live calls do.

## Deployment

Push to the original backend repository and the CALL_AGENT mirror. Confirm
`"release":"2026-10-26-voice-first-reply-3"` at `/health`, then inspect a fresh
call's provider latency and saved `voice_turn_timings`. Do not infer deployment
from the Git push or agent version alone. No voice-provider routing, phone
webhooks, model selection settings or paid hosting resources are changed by
this release.
