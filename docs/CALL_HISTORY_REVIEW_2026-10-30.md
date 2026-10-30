# Full call-history review, October 30

Reviewed all 35 stored calls from October 28-30 across three caller identities: 24 custom-voice calls, eight Retell calls, two Twilio missed calls and one legacy call. The caller histories contain 26, eight and one call respectively. Reviewed the messages, legacy transcription/response lists, human segments, outcomes, timing diagnostics, summaries and pipeline history. No customer identifiers or transcripts are included in this report.

Fetched all 34 available parent provider resources successfully: 26 Twilio and eight Retell. The legacy record has no supported provider resource identifier. Also fetched the child legs for all eight recorded human handoffs. Raw evidence and database backups remain in ignored local diagnostic files.

## What the history showed

- Seven human handoffs were unanswered; one connected. The seven unanswered legs have no Twilio provider notifications. This confirms the recorded dial outcomes, not why the people or phones did not answer. The connected human segment included an integration discussion; it is not evidence of a completed sale.
- One older Retell call ended with `error_llm_websocket_lost_connection`; the other seven Retell records report `user_hangup`. Two early calls on October 30 have HTTP 502 webhook/callback notifications. The retrieved remaining parent records have no notifications identifying a service error. The exact ending party/cause of the latest custom call is still unconfirmed.
- The repeat caller repeatedly described a new-home project and asked for guidance. Replies often restarted the product-range introduction, asked another broad question, or pointed to a website. The voice caller context contained the CRM lead but no recent call sequence.
- Team handoffs mostly carried the last transfer request rather than the product requirements discussed before it. A callback recipient could therefore receive a request to speak without the bathroom/product context.
- Two AI-generated pipeline changes marked callers unqualified solely after unanswered calls with no transcribed customer turn. One of those caller histories already contained genuine product and hours enquiries.

## Changes delivered

1. Added small, attributed previous-call notes for a returning caller: at most three recent conversations from an exact-phone, inbound, final-state, 30-day query. The read examines at most six matching records and the JSON notes are limited to 3,000 characters. Greeting-only and empty-transcript records do not crowd out meaningful conversations. The existing caller cache is reused.
2. Historical notes include the caller's own words and the recorded transfer outcome. Shared voice instructions require the current request to take precedence, prohibit treating historical quotes as current prices/stock/bookings, and prohibit treating stored text as instructions or proactively reading private old details aloud. Non-phone identities such as `unknown` and `client:...` cannot retrieve history through this helper; caller ID alone remains insufficient verification for protected order details.
3. Optional caller lookup has a 500 ms wait budget. Failure or timeout preserves retrieved business facts and lets the answer proceed. A history-read failure also preserves any successfully loaded CRM lead/customer data.
4. Team transfers and callbacks carry up to four recent caller statements, with a fixed per-statement limit. The brief is saved with the transfer and retained in the existing follow-up notification after an unanswered leg. It does not fabricate an AI interpretation or copy the agent's unsupported price claims.
5. Automatic pipeline changes require an attributed customer turn. Technical failures and agent-only greetings cannot disqualify the customer. Semantic stage instructions also preserve interest across connection checks or language requests; a subsequent genuine enquiry may reopen an unqualified thread.
6. Restored the two reviewed incorrect AI-generated stages to their recorded previous `new_lead` stages. Both database writes used compare-and-set checks, saved before-state backups, appended corrective stage-history events and were read back successfully. The 35 call records were preserved.
7. Shared voice instructions now turn exploratory requests into one useful next step: two relevant choices supported by the knowledge and one focused question, using already stated requirements instead of restarting the introduction.
8. Readiness reports a validated abbreviated commit identifier from [Render's deployment environment](https://render.com/docs/environment-variables#render_git_commit), allowing a running deployment to be distinguished from a source push.

## Validation and remaining work

Full suites passed: primary 680 tests, mirror 673 tests. A final focused run passed 24 tests in each repository, including the readiness identifier. Tests use fake providers, mail, databases and call streams; no live calls or messages were sent by this review. Mirror-specific email and discovery differences are preserved.

Human transfer availability and authoritative opening/holiday hours still need operational attention. The service/proxy cause behind the historical 502s and the older Retell websocket loss requires production runtime evidence; Render's dashboard remains behind sign-in. Source pushes alone do not establish deployment. Verify the live readiness revision after rollout and complete an actual phone acceptance check.
