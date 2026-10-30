# Preserve answers during caller connection checks

Today's call review found a 17-second custom-voice call in which three reply attempts were interrupted before a substantive answer reached the caller. The transcript contains repeated greetings and checks that the agent could hear the caller, interleaved with wait phrases.

Previously, recognized words during a pending answer cancelled that answer. The next turn then combined the check with the original question and started generation again. Repeating a connection check could keep cancelling useful work.

The custom Twilio pipeline now uses a dedicated semantic connection-check classifier, backed by the existing translated meaning vectors. A confident standalone check receives a short localized reassurance without replacing its pending question. Reassurance is limited to once per pending answer, with a cooldown for idle checks. Mixed utterances containing a real request, corrections, explicit pauses, repeat requests and human-transfer requests retain ordinary handling. Ambiguous or unavailable semantics do not invent a connection-check intent.

Recognized caller speech pauses audio while its turn is being resolved. Work on an unheard answer is retained until the completed utterance establishes whether it was a check or a substantive continuation. A real continuation still cancels/replaces the old answer. Both Nova and Flux turn finalization/watchdogs continue while an answer is paused, preventing a wait between the recognition and response tasks. Answer and reassurance audio share a lock so their streams do not overlap.

Connection checks remain in the transcript. A subsequent correction replaces the original pending caller turn by identity instead of deleting whichever message happens to be last. Per-call `provider_metadata.connection_checks` records check, reassurance and preserved-answer counts. Timing records use `action=connection_check` and `pending_answer_preserved`.

Validation covers repeated checks under Nova and Flux, translated classification, mixed substantive requests, explicit interruptions, retaining the original question after a check, an unclear partial resolving into a check, withholding an answer until a continuation finishes, and serialized audio. The existing custom-voice regression suite also checks real barge-in, pauses, recognition noise, watchdogs, language switching and wait-bridge behavior.

This change covers the custom Twilio voice path used by today's reviewed calls. It does not change Retell's independent response controller or live provider settings. Deploy/restart the updated backend and verify an actual phone call; offline replay tests do not establish live network/audio delivery quality.
