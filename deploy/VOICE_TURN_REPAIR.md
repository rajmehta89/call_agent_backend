# Repair custom-call setup, delayed bridges and caller interruptions

The latest inspected Retell call started on 26 October 2026 at 11:24:09 IST.
Its four spoken-response delays were 4.630, 2.094, 4.056 and 5.958 seconds.
The median was 4.343 seconds. Saved stage timings showed semantic preparation
of 9-733 ms, prompt preparation of 1.352-2.553 s and model waits of 0.759-1.032 s.
Only the basin-search reply used a bridge. Repeated "sorry" was treated as a new
product turn, and the provider kept speaking after a second interruption.

## Custom calls: reproduced failure

The two custom attempts at 11:27:32 and 11:27:55 IST ended after four and two
seconds. Twilio reported 31921, meaning the server closed the media connection.
Real Deepgram connection checks reproduced both setup defects:

- Nova-3 multilingual rejected `language_hint=ur` with HTTP 400,
  `INVALID_QUERY_PARAMETER`. It now uses automatic multilingual detection without
  unsupported hints. Explicitly selected Urdu uses Nova's dedicated `language=ur`.
- English Flux rejected the `language` query parameter with HTTP 400. The request
  now selects the language through the model name, without that parameter.

Real handshakes now succeed for Nova automatic detection, Nova Urdu and English
Flux. These checks verify connection setup, not transcription accuracy or a
complete phone conversation.

STT now opens after the Twilio Start token and language parameters are validated,
so it uses the selected call language. Arabic and Urdu use Nova rather than the
English Flux model. The Twilio Stream URL has no query string; language and the
stream token are carried in custom parameters. Setup failures are saved as failed
calls with operational error metadata instead of being recorded as completed.

## Bridge and turn handling

- Retell races the first answer against the wait deadline. Slow replies receive
  one context bridge, with a short generic fallback if bridge selection/model
  generation is slow. A ready answer wins; bridge work cannot hold it up.
- The default wait acknowledgement deadline is 1.2 seconds. Existing
  `VOICE_STATUS_AFTER_MS` overrides remain respected. Retell timings include
  `bridge_ms` separately from the first main-answer text.
- Custom calls can speak a wait acknowledgement even before it is in the audio
  cache, and record the general status phrases first during warm-up. An expired
  deadline no longer silently suppresses the status update.
- Both paths recognize standalone "sorry", "stop" and "hold on" as interruptions.
  They acknowledge briefly and listen rather than starting a product lookup.
  An apology followed by a real question still goes through normal reasoning.
- Custom audio is cleared on interruption even before the playback mark exists.
- Retell receives a per-call interruption sensitivity of 0.85 (the inspected
  agent used 0.5), configurable through `RETELL_INTERRUPTION_SENSITIVITY`.
  An explicit `user_turn` cancels stale generation; ordinary transcript updates
  do not cancel a valid response.
- Accepting an offer with "Yeah, I would love to" retains the earlier product
  and offered details in retrieval. Basic business introductions skip a needless
  live store lookup while retaining the business knowledge.

## Verification

Tests cover the real call's short apology and accepted-offer wording, queued audio
clearing, provider request parameters, custom parameters, bridge/answer races,
slow bridge fallback, cancellation and Retell turn-taking events. Real Deepgram
handshakes also passed. Fresh phone calls remain necessary to verify live audio,
interruption sensitivity and final end-to-end timing after deployment.

Production health must show `2026-10-26-voice-turn-repair-4`.

## Follow-up from the 11:58 IST production call

The latest call showed the acknowledgement bridge playing at about 1.2 seconds
after Retell requested each response, including the simple company overview and
contact follow-up. The spoken answer then began about 1.4-2.6 seconds after
those acknowledgements. The caller-to-acknowledgement interval was longer because
Retell had not yet declared the caller's turn complete; the bridge timer begins
only after `response_required` arrives.

Both Retell and custom Twilio now ask once whether the caller needs anything else
after seven seconds of silence following the completed answer. Retell sets maximum
responsiveness. Custom Flux's silence backstop is 1.8 seconds by default (configurable with
`CUSTOM_FLUX_EOT_TIMEOUT_MS`; increase it for callers who pause longer between
clauses). The short bridge already covers delayed simple answers and contact
details; it improves perceived wait time but does not make the full model answer
finish sooner.

The shared reminder timing is configurable with `VOICE_REMINDER_TRIGGER_MS`;
the Retell reminder count is configurable with `VOICE_REMINDER_MAX_COUNT`.

## Follow-up from the latest custom calls

The latest custom calls exposed generic status phrases on short or simple turns,
including an incomplete "So" and a short "No". The custom path used a generic
status even when the turn classifier had deliberately selected no bridge. The
Retell path also used a generic fallback when the bridge classifier returned no
phrase. Both now preserve that empty decision and answer directly; when a bridge
is appropriate, the model is told to continue with the answer without adding a
second holding phrase.

Production health must show `2026-10-26-voice-turn-repair-7` after deployment.

Primary references:

- https://developers.deepgram.com/docs/nova-3-language-prompting
- https://developers.deepgram.com/docs/flux/quickstart
- https://docs.retellai.com/api-references/llm-websocket
- https://www.twilio.com/docs/api/errors/31921
- https://www.twilio.com/docs/voice/twiml/stream
