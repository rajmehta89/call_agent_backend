"""Connection checks repair the conversation instead of replacing its pending request."""

from meaning import Classifier
from voice_languages import normalize_language


CONNECTION_CHECK = Classifier("caller connection check", {
    "presence": [
        "Hello?", "Hello, hello?", "Are you there?", "Is anyone still there?",
        "Hello, can you hear me?", "Can you hear my voice?", "Are you still listening?",
        "Hello, are you there? Can you hear me?", "I can't hear anyone. Are you there?",
        "Have I lost you?", "Are we still connected?", "Is this line working?",
        "Hello, hello, hello. Is anybody there?", "Please let me know you can hear me.",
        "I'm still here. Can you hear me on your end?", "You have gone quiet. Are you still there?",
        # "Hello. बोलिए." (Hello, speak) from a caller who heard nothing: the agent kept starting answers until they hung
        # up (2026-10-30). Said to a silent line, it asks whether anyone is there, not for the agent to go on.
        "Hello? Speak.", "Hello, say something.", "Hello? Please speak, I can't hear anything.",
    ],
    "request": [
        "Hello, what are your opening hours?", "Hello, I need a refund.",
        "Hello, hello, can you tell me the price of a basin?", "Are you there? I need to change my order.",
        "Can I speak to someone in your team?", "Is there anyone I can talk to about my order?",
        "Put me through to a person.", "Please call me back tomorrow.",
        "Stop, I meant the bathroom products.", "Wait, please.", "Sorry, hold on.",
        "Please repeat the price.", "I couldn't hear the answer. Say it again.",
        "I meant the kitchen sink, not the bath.", "No, cancel that request.",
        "I can hear you. My question is about delivery.", "What time do you close tomorrow?",
        "Can you check whether my order has arrived?", "What do you sell?",
        "Yes, please.", "Okay, thank you.", "Go ahead.", "Goodbye.",
    ],
}, min_score=0.60, close_call=0.06)


def is_connection_check(text: str, earlier: str = "") -> bool:
    """Require a confident semantic presence intent, using the prior agent turn to resolve weak matches.

    Shared meaning vectors provide translation for Urdu/Hindi and Arabic. There
    are no keyword, phrase-matching or script-based intent fallbacks. A weak
    match is accepted only when the preceding agent turn supports that meaning.
    """
    if CONNECTION_CHECK.close(text) != ["presence"]:
        return False
    if not earlier:
        return True
    ranked = CONNECTION_CHECK.rank(text) or []
    if ranked and ranked[0][0] == "presence" and ranked[0][1] >= 0.78:
        return True
    contextual = " ".join((earlier[-300:], text)).strip()
    return CONNECTION_CHECK.close(contextual) == ["presence"]


_CONNECTION_LINES = {
    "en": {
        False: (
            "Yes, I can hear you. Please go ahead.",
            "I'm here, and I can hear you.",
            "Yes, I'm here. What can I help with?",
            "I can hear you clearly. Go ahead.",
        ),
        True: (
            "Yes, I heard your question. I'll answer that.",
            "I'm here, and I heard what you asked.",
            "Yes, I have your question. Please go ahead.",
            "I can hear you. I have your question.",
        ),
    },
    "ur": {
        False: (
            "جی، آپ کی آواز آ رہی ہے۔ بتائیے۔",
            "جی، میں سن رہا ہوں۔ آپ کہیے۔",
            "میں آپ کو صاف سن سکتا ہوں۔",
            "جی، میں موجود ہوں۔ آپ بتائیں۔",
        ),
        True: (
            "جی، میں موجود ہوں۔ میں نے آپ کا سوال سن لیا ہے۔",
            "جی، میں سن رہا ہوں۔ آپ اپنا سوال جاری رکھیں۔",
            "آپ کی بات سن لی ہے۔ میں جواب دیتا ہوں۔",
            "جی، آپ کا سوال میں نے سن لیا ہے۔",
        ),
    },
    "ar": {
        False: (
            "نعم، أسمعك. تفضل.",
            "أنا هنا وأسمعك بوضوح.",
            "نعم، أنا هنا. كيف أساعدك؟",
            "أسمعك جيدًا. تفضل.",
        ),
        True: (
            "نعم، أنا هنا وقد سمعت سؤالك.",
            "أسمعك، وقد فهمت ما سألت عنه.",
            "نعم، سمعت سؤالك. تفضل.",
            "أنا هنا، ويمكنك متابعة سؤالك.",
        ),
    },
}


def connection_reply_lines() -> tuple:
    """The English lines, for writing them in a language with no hand-written copy (voice_localize)."""
    return (*_CONNECTION_LINES["en"][False], *_CONNECTION_LINES["en"][True])


def connection_reply(language: str, pending: bool, recent=()) -> str:
    """Return a short, natural line without repeating one just heard in this call."""
    lines = _CONNECTION_LINES
    code = normalize_language(language)
    if code in lines:
        options = lines[code][bool(pending)]
    else:
        # A language with no hand-written lines (Punjabi): the model's translations of the English ones.
        from voice_localize import localized
        options = tuple(localized(line, code) for line in lines["en"][bool(pending)])
    recent_items = [recent] if isinstance(recent, str) else (recent or ())
    recent_lines = [str(line).strip() for line in recent_items]
    for line in options:
        if line not in recent_lines:
            return line
    last = next((line for line in reversed(recent_lines) if line in options), "")
    next_index = (options.index(last) + 1) % len(options) if last else 0
    return options[next_index]
