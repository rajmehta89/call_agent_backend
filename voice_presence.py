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


def is_connection_check(text: str) -> bool:
    """Only confident standalone presence intent qualifies; ambiguous meaning changes nothing.

    Shared meaning vectors provide translation for Urdu/Hindi and Arabic. There
    are no keyword, phrase-matching or script-based intent fallbacks.
    """
    return CONNECTION_CHECK.close(text) == ["presence"]


def connection_reply(language: str, pending: bool) -> str:
    lines = {
        "en": ("Yes, I can hear you. Please go ahead.", "Yes, I'm here. I'm still working on your question."),
        "ur": ("جی، آپ کی آواز آ رہی ہے۔ بتائیے۔", "جی، میں موجود ہوں۔ آپ کے سوال پر کام کر رہا ہوں۔"),
        "ar": ("نعم، أسمعك. تفضل.", "نعم، أنا هنا. ما زلت أعمل على سؤالك."),
    }
    return lines[normalize_language(language)][int(pending)]
