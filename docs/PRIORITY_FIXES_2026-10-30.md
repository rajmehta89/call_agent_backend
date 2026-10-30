# CRM priority fixes — 30 October 2026

The ten issues from the CRM review are addressed in this change:

1. Alternate platform endpoints enforce operation permissions and existing lead/call ownership scopes. Customer writes allow only defined fields; exports require an export permission.
2. Public manual call logging is disabled. Provider records are written through internal lifecycle handlers.
3. Missing Twilio verification credentials and Retell socket secrets reject traffic. Production startup and provider activation validate required settings.
4. Shopify configuration accepts canonical Shopify hosts and requires fresh credentials when changing stores.
5. Website ingestion validates every resolved address and redirect, pins the connection to a validated public address, and limits redirects, response types, bytes and socket waits.
6. Synchronous Shopify/provider operations, Retell webhook processing and PDF ingestion run outside the voice event loop.
7. Retell lifecycle rank and conditional writes prevent late transcripts or end events from replacing completed analysis.
8. Knowledge documents and source metadata are replaced in a single MongoDB transaction, after extraction and embedding succeed.
9. Conditional stage updates preserve human edits made while AI classification is running.
10. Durable thread summaries retain complete history. Inbox, Tasks, Leads and Calls use stable continuation cursors, full-query filters and explicit totals; their frontend pages expose Load more controls.

## Deployment requirements

- Deploy the matching frontend and backend changes together. This change does not set live environment variables or change provider configuration.
- Keep `TWILIO_VALIDATE_SIGNATURES=true` and set `TWILIO_AUTH_TOKEN` in production.
- When Retell is enabled, set a random `RETELL_CUSTOM_LLM_SECRET`. Configure the same value as the `token` query parameter of the Retell custom LLM WebSocket URL. Missing or mismatched values now reject the connection. The provider also requires its existing API key and agent configuration.
- Knowledge replacement requires MongoDB Atlas or a replica set with transaction support. Standalone MongoDB is deliberately rejected rather than deleting working knowledge before a failed replacement.
- Restart the backend to activate the changes. The Inbox summary cache backfills complete history automatically at startup or on its first read. It preserves assignment, stages and notes. Subsequent call changes refresh the affected caller in a background worker.
- Existing administrator roles retain access. Grant `export_data` explicitly to custom roles that should export records, along with the relevant resource permission.

## Verification

Run `APP_ENV=test python -m pytest --disable-warnings` (set the environment variable separately in PowerShell). Tests substitute database and provider settings; they cover permissions, unsafe destinations, event ordering, transaction rollback, human-edit races, event-loop responsiveness and stable pagination, including naive MongoDB timestamps.

The matching frontend production build and 14 isolated browser checks pass on desktop and mobile. Browser fixtures contain 250 synthetic records and intercept all API traffic. Real phone calls, live MongoDB transactions and production provider settings still need deployment acceptance checks. No live customer data was changed by verification.

This completes the ten prioritized fixes, not every additional observation in the broader audit. Dependency upgrades and the unavailable fresh npm advisory audit are separate follow-up work.
