# Voice conversation decisions

Use semantic intent detection together with conversation state for caller requests,
transfer offers, callbacks, product requests, model selection, and wait bridges.
Do not add keyword lists, phrase matching, or regular expression intent rules,
including fallback rules when a semantic service fails. Continue safely without
guessing an action when meaning is uncertain. Keep completed callback state so
acknowledgements cannot restart the handoff flow.

Format validation, contact parsing, and catalogue entity retrieval may use exact
matching; these must not decide conversational intent.
