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

## Callback and email follow-up

The 12:35 IST custom call showed the email recognizer receiving a non-Latin
transliteration for the local part. It correctly asked once for a clearer
address, but later turns did not remember that retry and asked again. The call
also classified the caller's final "call me later" as a callback request but
continued ordinary model prompting instead of placing it in the callback queue.

Earlier complete email addresses now remain in call context and are attached to
the callback notification and call record. If an address was not understood and
the agent already asked once for a repeat, the next turn proceeds with the
caller's phone number instead of asking for the email again. Explicit callback
intent now creates the human follow-up notification on both voice paths.

Production health must show `2026-10-26-voice-turn-repair-8` after deployment.

Primary references:

- https://developers.deepgram.com/docs/nova-3-language-prompting
- https://developers.deepgram.com/docs/flux/quickstart
- https://docs.retellai.com/api-references/llm-websocket
- https://www.twilio.com/docs/api/errors/31921
- https://www.twilio.com/docs/voice/twiml/stream

## Product-detail follow-ups

The 12:55 IST custom call asked about round basins and their prices. Speech
recognition first misheard the product name, then the meaning-only store
classifier missed the clearer basin availability question. The later price
question was recognized as product-related, but its query had lost the basin
from the earlier turns, so the agent fell back to saying it lacked pricing.

Product-category and shopping-interest cues now recover catalogue lookups when
the meaning classifier misses, while the store's live product vocabulary is
still loading. Follow-up price/detail searches retain the recent caller product
phrase, including when speech recognition garbles the newest product word.

Production health must show 2026-10-26-voice-turn-repair-9 after deployment.

## Wait bridges and turn boundaries on both voice paths

The 13:15 IST call returned live product details, but no bridge. Its stored
post-turn response-to-audio measurements were 1.394–2.682 seconds; TTS added
80–93 ms. Those old measurements did not include the STT end-of-turn delay.

Custom voice no longer serializes answer preparation behind bridge selection
or requires bridge audio to have been warmed first. Both custom and Retell
race the answer against one qualified wait acknowledgement. A ready answer wins,
shopping statements classified as detail can still qualify for a live-lookup
bridge, and slow full knowledge questions may get a status update. Short replies,
contact details and explicit interruptions remain excluded. An undecided bridge
classifier no longer results in an unqualified generic fallback.

Nova ignores old UtteranceEnd events when newer words have arrived and preserves
the newest interim continuation when its watchdog ends a stalled turn. Flux's
handoff check no longer blocks the speculation/watchdog loop. Non-Latin turns
are retained rather than treated as empty speech.

New custom timing records identify the actual STT engine and turn-end trigger,
bridge audio time, and first audible response. When provider word timestamps
are available, they also estimate turn-detection delay from the inbound audio
timeline and the interval from speech end to the first audible response.

Production health must show 2026-10-26-voice-turn-repair-10 after deployment.

Turn-boundary references:
- https://developers.deepgram.com/docs/understand-endpointing-interim-results
- https://developers.deepgram.com/docs/flux/configuration


## Semantic transfer offers and callback state

The 13:49 IST call accepted a live connection offer but was asked to choose now
or later. After a later callback was queued, an acknowledgement reopened that
same choice. Both voice paths now resolve replies against the offer's semantic
meaning and retain completed callback state. Callback requests are queued once;
acknowledgements do not create another handoff. Caller ID is reused when present,
and live transfers do not require name, phone, or email collection.

Handoff, product, model-choice and bridge intent decisions use semantic models
and conversation state. Lexical intent shortcuts and the old keyword fallbacks
have been removed from these paths. Semantic examples are warmed during startup.
Contact parsing and catalogue entity search remain separate from intent routing.
The fast model resolves ambiguous live/callback timing with a bounded request;
it does not generate every non-product answer. Control confirmations are spoken
directly and their routing/audio timings are recorded.

Production health must show `2026-10-26-voice-turn-repair-11` after deployment.
A fresh production call is needed to measure caller-perceived latency after
this release; health and regression checks alone do not establish timing parity.

Validation: all 409 backend tests passed locally, including semantic handoff,
callback state, voice turn timing, and live-store answer regressions.

## Release 12: bridge slow routing and close clear Nova turns sooner

The 14:19 UTC custom call on 26 October had four long speech-end-to-audio
intervals: 4.531, 4.139, 3.507 and 5.297 seconds. Turn detection accounted for
2.350, 4.050, 2.760 and 5.200 seconds; after the turn closed, the first audio
followed in 2.181, 0.089, 0.747 and 0.097 seconds. This shows that most of the
delay happened before the answer path began, so an answer-only bridge could
arrive too late or not at all.

Both custom Twilio and Retell now start one eligible bridge while semantic
handoff routing and answer generation run. The fast model can choose among
approved, fact-free holding lines; a slow or unavailable model falls back to
the approved line. A ready answer still wins, and a routing-time bridge suppresses
the second bridge. A catalogue-lookup phrase is changed to a general status line
until the lookup actually starts.

Custom Nova calls also use the semantic turn-completion model after a short quiet
period. It can end a clearly complete turn before the longer provider watchdog;
email and phone dictation retain the longer wait so pauses do not cut off contact
details. This does not reduce the provider's own end-of-turn delay when Nova is
still listening.

The local browser-call application error was Twilio error 11200: the local ngrok
tunnel had stopped, so Twilio could not fetch its voice webhook. Restarting the
local development services restored the tunnel and local browser app URL. The
production phone routing was not changed by this repair.

Production health must show `2026-10-26-voice-turn-repair-12` after deployment.
A new live call is needed to measure the effect on actual turn detection and
caller-perceived timing; the recorded call above predates these changes.

## Release 13: require clearer intent before a live transfer

The 14:56 UTC custom call on 26 October lasted 20 seconds. The saved transcript
shows the caller asking, “Yeah. So what is only the voice for?” The agent began a
live transfer instead of answering. Semantic handoff scoring ranked `live` at
0.575 and `other` at 0.553; the previous 0.02 margin accepted that weak lead.
The turn was not a follow-up request.

Live transfers now require a 0.04 semantic lead over `other`. The handoff
examples also cover ordinary questions about checking something and what the
voice service does. Explicit requests to speak to the team continue through the
same semantic classifier. Regression coverage includes the saved transcript and
“Can we check?”

Production health must show `2026-10-26-voice-turn-repair-13` after deployment.
A fresh call should confirm the agent answers ordinary questions without
starting a transfer and still transfers on a clear request.
