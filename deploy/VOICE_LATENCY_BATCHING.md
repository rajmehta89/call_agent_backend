# Reduce production response preparation time

The latest measured production call on 26 October, 10:35 IST, took 6.136 and
6.037 seconds to speak its replies. Retell measured backend/LLM processing at
5.111 and 5.064 seconds, speech generation at 0.407 and 0.410 seconds, and median
WebSocket round-trip time at 17 ms. Most of the wait preceded the first backend
text chunk. Server logs are still needed to split this into preparation and
model first-token time.

## Changes

- Batch the original caller text, generic turn text, masked place question,
  previous agent utterance and previous question into one semantic embedding
  request. Existing classifiers consume the same inputs and retain their rules.
- Concurrent checks share embedding requests already in progress. Failed
  requests release their entries so the next turn can retry.
- Run contact checks, model selection and place checks concurrently. Preserve
  the careful model and coverage warning for an unconfirmed location.
- Reuse the customer-context lookup already started for the prompt.
- Trace semantic preparation separately and include model-wait time in the
  first-text trace. These traces distinguish preparation from model latency.

## Measured scope

A real provider benchmark on the same local machine with local embeddings
disabled used the same four distinct semantic inputs from the latest call:

| Method | First run | Second run |
|---|---:|---:|
| Separate network requests | 5.376 s | 4.386 s |
| Batched network request | 1.551 s | 1.601 s |

These measurements cover semantic inputs only, with an empty semantic cache
for each run. They do not measure a complete call or predict the exact gain
on Render. The raw local report is `logs/semantic_batch_benchmark_20261026.json`.

Production parity also requires checking the model settings, embedding mode,
instance capacity and active voice path. See `VOICE_PARITY.md`. The previous
1.91-second local first-audio baseline excludes recognition and phone transport.
The latest production call used Retell; the recorded local baseline used the
custom Twilio pipeline.

## Deployment check

The health response for this release contains
`"release":"2026-10-26-voice-latency-2"`. Confirm that marker, then inspect a
fresh call's semantic-preparation, retrieval and model-wait traces. Pushing the
commit alone does not prove that the Render service deployed it.
