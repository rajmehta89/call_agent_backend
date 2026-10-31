# Follow-up to the first measured Pipecat phone call

The completed English call at 16:19 IST lasted 62 seconds and recorded Pipecat 1.15.0 on all three timed business
replies. Speech-end estimates to substantive answer audio sent to Twilio were 1,927, 1,971 and 6,775 ms. Their median
was 1,971 ms; the mean was 3,558 ms. Playback at the phone was not measured. The small sample does not establish a
production percentile or a before/after framework comparison.

The final question asked for broad advice after buying a new house. Its slow path contained:

| Stage | Saved elapsed time |
| --- | ---: |
| Turn detection | 1,120 ms |
| Semantic checks | 3,603 ms |
| Contextual handoff checks | 3,609 ms, including 2,891 ms in meaning preparation |
| Optional store wait | Timed out at 2,501 ms |
| Knowledge retrieval work | 1,505 ms |
| Model first words after preparation | 558 ms |
| TTS first audio chunk | 143 ms |

Stages overlap and must not be added together. All three replies generated first audio in 143–190 ms. The call
also recorded nine event-loop lags above 250 ms, with a maximum of 901 ms. Stack samples indicate contention to
investigate; they do not establish which function or library caused the stalls.

## Changes

1. **Smaller semantic batches.** Turn checks use `VOICE_SEMANTIC_BATCH_SIZE=1` by default. Mixed short questions and
   longer conversational probes no longer share a large padded inference batch. `LOCAL_EMBEDDING_BATCH_SIZE`
   continues to control knowledge/catalogue indexing, independently. Semantic vector caching and coalescing remain
   in place. An explicit semantic batch override between 1 and 8 allows further measurements or rollback.
2. **General guidance uses business knowledge.** Semantic examples distinguish planning a project from asking for
   specific catalogue items. The store-skip check reads the complete request and requires a confident non-store
   classification. Previous product/order/policy discussion keeps live lookup available for contextual follow-ups.
   Price, stock, named-product and order checks remain in the existing flow. General advice still receives retrieved
   business facts; this change does not tell the model to invent product details or prices.
3. **CPU evidence on the next call.** `/ready` reports CPU quota, cumulative process/container counters and the
   selected semantic batch size. Saved custom calls contain counter deltas over their call window. The process and
   container may handle concurrent calls/requests: those deltas must not be attributed exclusively to one caller.
   Missing or inaccessible cgroup v2 counters remain unknown. No container paths, credentials or transcript data are
   returned by these diagnostics.

Conversation decisions remain semantic and preserve the existing callback, transfer, interruption and language
state. No intent keyword rules or more aggressive endpoint thresholds were added.

## Local CPU measurement

`python scripts/benchmark_semantic_preparation.py --repeats 3` disables MongoDB and provider embedding fallback,
uses a warm local model, alternates batch-size order and clears query vectors between trials. It measures a synthetic
multi-sentence planning question and its conversational probes, not the complete phone call.

| Semantic batch size | Median preparation time |
| --- | ---: |
| 8 | 342.16 ms |
| 1 | 210.75 ms |

That is 131.41 ms (about 38%) less local preparation time in this run. Both settings produced the same cached vectors
in this test (maximum absolute difference 0). An earlier diagnostic with the longer recorded question measured
approximately 662/335 ms. Local measurements cannot predict savings on the deployed CPU or be subtracted from the
6,775 ms phone-call result. The avoided optional store wait also overlapped with semantics, so it is not a guaranteed
2.5-second full-call reduction.

Private evidence remains under ignored `logs/`: `latest-pipecat-call-2026-10-31-private.json`,
`call-preparation-batch-profile.json`, and `semantic-preparation-after-fix.json`.

## Acceptance measurement

Full suites passed with Pipecat installed: **787 tests** in the primary backend and **780 tests** in the mirror,
with zero failures, errors or skipped tests (141.82/141.30 seconds). Regression coverage includes the recorded broad
question, business-name normalization, product-context follow-ups, retained business facts, prices/stock/orders,
semantic batching, CPU counters and the shared multilingual call controller. Offline replay of the recorded earlier
business questions also selects the general-guidance path without a live catalogue request.

Repeat the general home-planning question, then ask about a named product's price/stock and a short contextual
follow-up. Compare substantive answer audio, semantic preparation, store timeout counts and event-loop lag. Check
the CPU counter deltas before recommending a host upgrade. A response below three seconds for the earlier slow
question is an engineering target; no post-change phone-call saving has yet been measured.
