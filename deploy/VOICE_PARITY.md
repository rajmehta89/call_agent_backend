# Match production voice performance to the measured local baseline

Prepared 26 October 2026. These changes are tested locally and have not been deployed to Render.

## Measured baseline

| Measurement | Result | Scope |
|---|---:|---|
| Recorded local first audio, median of 30 turns | 1.91 seconds | Existing custom Twilio voice logs, after the caller's transcript is ready |
| Last two recorded local first-audio times | 2.44 and 2.77 seconds | Recent actual calls |
| Fresh warmed local first generated sentence | 0.969–1.217 seconds | Three questions, second pass; excludes speech recognition and TTS |
| Warm local semantic embedding | 38–41 milliseconds | Fresh query, model already loaded |
| Equivalent network semantic embedding | 1.077–1.416 seconds | Same machine, runtime and example questions; local model disabled |
| Latest production spoken responses | 4.991 and 6.559 seconds | Retell-reported end-to-end timings |
| Latest production backend/LLM processing | 3.930 and 5.209 seconds | Combined backend processing and model generation |

The first local test pass was slower than the warmed pass. Compare the same questions after startup warm-up in both environments. Network embeddings are the code default on Render; the current production environment override remains unverified because dashboard access was denied.

## Confirmed failures and tested corrections

1. Twilio rejected the latest team-transfer child call with error 13247. Its outbound caller ID was the browser caller's contact number. Forwarding now uses the configured Twilio business number; the caller's contact number remains in the CRM.
2. Retell could generate another AI response while a live transfer was waiting to start. A pending transfer now suppresses subsequent responses and prevents duplicate transfer attempts.
3. Final call saves could overwrite the fallback callback-request flag. The forwarding fields now preserve `human_callback_requested`.
4. Semantic model selection and contact checks could block Retell's event loop. They now run concurrently off that loop; place and end-of-call classification also run off the loop.
5. Timing now starts before classification and the unfinished-sentence wait, so the first-token trace includes those stages.
6. Local embedding inference has explicit thread and batch limits to reduce CPU oversubscription and temporary memory demand.

Validation: **334 backend tests passed**, including a WebSocket regression test for an extra caller utterance during transfer.

## Production settings

`render-voice-parity.env` contains only non-secret settings from the local voice configuration, plus bounded embedding thread/batch sizes. Keep the existing production API credentials, database, public URLs, authentication settings and CORS origins.

The local model uses GPT-4.1-mini, with the configured small fast-turn model and fallback. Preserve the active production speech provider while aligning the backend; Retell speech synthesis and the custom ElevenLabs connection are separate integrations.

Check the Render instance's memory capacity and peak usage before enabling `LOCAL_EMBEDDINGS=true`. The existing code deliberately defaults to network embeddings on Render to protect small instances. Enabling a local model without sufficient capacity can cause restarts. No hosting plan or paid resource has been changed.

The local ElevenLabs API returned HTTP 401 with `quota_exceeded` and **0 credits remaining**. This is a quota failure, not proof of an invalid API key. Do not replace working production speech credentials with that local account. The fresh full-audio benchmark stopped at this provider error; its completed text-only counterpart is saved in `logs/voice_local_text_benchmark_20261026.json`.

## Deployment and verification

1. Verify the Render service's connected repository, instance capacity, current model/embedding settings and active voice provider.
2. Apply the reviewed non-secret profile, retaining production secrets and URLs, and deploy the voice-specific code changes.
3. Confirm startup warm-up completes, the configured model is GPT-4.1-mini, and the chosen embedding path is active.
4. Run the same benchmark on production:

   ```sh
   python scripts/benchmark_voice_latency.py --repeats 2 --output logs/voice_production_benchmark.json
   ```

   This invokes the real model and knowledge pipeline without placing phone calls or writing CRM records. For the custom TTS pipeline with an available quota, add `--audio --engine elevenlabs` or use the explicitly selected Deepgram engine. The benchmark excludes speech recognition and phone transport, so compare its numbers to the same local script, not directly to Retell's end-to-end measurement.

5. Confirm live first-audio timings are in the local range after warm-up, the outbound team leg uses the Twilio business identity, and failed transfers retain their callback request.

Browser security denied access to `dashboard.render.com` earlier in this session. Production configuration, deployment and production performance verification are pending renewed dashboard permission. Local fixes and passing tests do not establish that production has changed.
