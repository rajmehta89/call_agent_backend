# Latest call and local changes: 31 October 2026

## What the calls show

The latest stored call started at **14:30:54 IST**, lasted **16 seconds**, and contains one completed business reply.
The caller asked what the business offers. The agent described the supported home-product categories and asked what
the caller wanted to know. The saved reply is in Urdu, matching the call's selected language. This is a transcript
and telemetry review, not a listening evaluation of pronunciation or phone playback.

| Latest reply measurement | Time |
| --- | ---: |
| Saved speech-end/turn-detection estimate | 400 ms |
| Prompt preparation, overlapping semantic checks and facts | 405 ms |
| Model first text after prompt | 451 ms |
| Turn handler to first speakable sentence | 1,186 ms |
| Voice generation to first audio chunk | 209 ms |
| Estimated speech end to first answer audio sent | **1,795 ms** |

The checkpoints overlap and are not additive. Audio timing records when this backend sends audio to Twilio, not
when it plays through the caller's phone. The stored endpoint component estimates use different source snapshots;
they should not be summed to manufacture a more precise speech-end clock.

The preceding two longer calls show why the latest short call is insufficient to declare latency solved:

| Call start, IST | Turn | First audio, including any wait phrase | Actual reply audio, excluding wait phrase |
| --- | --- | ---: | ---: |
| 14:29:15 | Business introduction | 1,534 ms | 1,534 ms |
| 14:29:15 | Advice for a newly purchased home | 2,581 ms | **3,819 ms** |
| 14:29:15 | Transfer announcement | 3,754 ms | 3,754 ms |
| 14:07:24 | Business introduction | 1,845 ms | 1,845 ms |
| 14:07:24 | Advice for a newly purchased home | 3,815 ms | 3,815 ms |
| 14:07:24 | Room/product follow-up | 3,471 ms | 3,471 ms |

All values above include the saved turn-detection estimate. The longer product turns spent 1.7–2.2 seconds preparing
the prompt; model first-text waiting was about 0.42–0.52 seconds. The transfer's routing/announcement preparation
took 3,167 ms before voice generation. These are the remaining areas to investigate, rather than assuming that a
different voice or language model will fix them.

Read-only Twilio requests confirm both newest calls completed and have no provider notification errors. The preceding
call's human leg was **no-answer**, with zero connected seconds. The CRM already records `transfer_status=no-answer`
and `transfer_succeeded=false`; it was an attempted transfer, not a successful connection. These records do not
establish why ringing stopped or who ended the call.

## Review of the unfinished changes

The pre-existing Shopify change split catalogue descriptions and price ranges into two full paginated reads.
Live read-only checks of the actual query shapes returned:

| Query | Requested cost | Actual cost | Products in first page | Result |
| --- | ---: | ---: | ---: | --- |
| Existing combined catalogue | 90 | 79 | 250 | Success |
| Proposed descriptions only | 79 | 68 | 250 | Success |
| Proposed separate prices | 20 | 20 | 100 | Success |

The original query already succeeds on this store. Splitting it requires additional price pages and adds startup
requests without evidence of a query-limit failure. The final code keeps the combined read. Shopify documents a
[1,000-point single-query ceiling and query-cost response metadata](https://shopify.dev/docs/apps/build/apis/graphql-admin/rate-limits).
The observed costs are measurements from this store, not guarantees for every store or future API version.

An ignored local patch preserves the intermediate split implementation in
`logs/shopify-split-query-before-simplification-2026-10-31.patch`.

The pre-existing `turn_meaning.py` status is line-ending-only: Git reports LF in the index and CRLF in the working
copy, with no content diff. It was left intact.

## Corrections made locally

- **Catalogue freshness:** a cold product lookup can use a recently built catalogue price immediately and refresh it
  in the background, as intended. An expired catalogue price now requires a live read. Setting both live-cache age
  limits to zero also disables the catalogue-price shortcut. Failed rebuilds keep the original snapshot timestamp;
  their retry timer no longer makes old prices look freshly fetched.
- **Prompt measurements:** existing `prompt_*_ms` fields remain cumulative readiness checkpoints for compatibility.
  Schema 2 adds separate knowledge/store/caller work times and store/caller thread-pool queue times. Missing or timed
  out operations stay unknown, rather than inheriting elapsed time from another operation.
- **Control replies:** a transfer or callback announcement preserves the first-audio clock of an earlier routing
  wait phrase, while retaining a separate clock for the announcement itself.
- **Latency report:** reports first audio and substantive reply/control audio separately, derives the custom-voice
  speech-end-to-reply estimate only when both required measurements exist, and exposes the new operation timings.
  It reads MongoDB directly instead of importing the application's index-creating database singleton.

No keyword intent rules were introduced. Conversation decisions still use semantics and conversation state.

## Validation and deployment

Final full backend suite: **736 passed**, 166 warnings, in 139.02 seconds with `APP_ENV=test`.
The synchronized mirror suite also passed: **729 passed**, 163 warnings, in 141.19 seconds.
The warnings are deprecations in existing framework/date APIs. Regression coverage includes paginated catalogue
builds, failed rebuilds preserving snapshot age, expired-price live reads, per-operation prompt timings, unavailable
caller history, separation of filler/answer clocks, and a real call-handler simulation of the transfer bridge clock.

Command: `python -m pytest -o addopts='' -q --disable-warnings --junitxml=logs/latest-call-review-2026-10-31-tests.xml`.
The report was also run read-only against the three latest measured calls with
`python scripts/voice_latency_report.py --calls 3`; its derived values match the table above.

During the review, the hosted readiness endpoint reported ready and warm at mirror revision `3aa131ed826f`.
The corrections, regression tests and this report are synchronized to the primary and mirror source histories.
Source publication does not establish activation: check the hosted readiness revision against the new mirror
commit after deployment, then make the acceptance call. The frontend has no changes in this review.

After deployment, a useful acceptance call should include a broad business question, a room/product follow-up,
a price question, and a human transfer. Compare the substantive-answer clock and new prompt work/queue fields.
The current evidence does not establish a live speed improvement from these local changes.

Raw call evidence remains under ignored `logs/`; this report contains no phone numbers, credentials or raw transcripts.
