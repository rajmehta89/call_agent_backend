"""Read a control reply's complete meaning before replacing its business question."""

from meaning import Classifier
from typing import Optional
from turn_meaning import PAUSE_TURN


HUMAN_REQUEST = Classifier("human request versus help getting started", {
    "person": ["Can I speak to someone in your team?", "Please connect me to a human.",
               "Can you arrange a call with your teammate?", "Could someone from sales help me on the phone?",
               "I want an agent to join this call.", "Please have someone call me back.",
               "Can you link me with your team?", "I need to talk with your staff about my order."],
    "business": ["I just bought a new home and need help getting started.",
                 "I bought a house and need a starting point for the bathroom.",
                 "I have just purchased a home, so I need a startup.",
                 "What do I need first for my new house?", "Can you help me choose products?",
                 "I need advice on where to start.", "How can your company help me?",
                 "I want to keep talking with you about these products.", "What do your staff do?"],
}, min_score=0.62, close_call=0.05)


def clearly_requests_person(text: str, earlier: str = "") -> bool:
    return HUMAN_REQUEST.close(text, earlier) == ["person"]


CALLER_PAUSE = Classifier("caller pause versus refusal", {
    **PAUSE_TURN.examples,
    "other": [*PAUSE_TURN.examples["other"],
              "No, thanks.", "No, I don't want a callback.", "Don't transfer me.",
              "No thanks, I still need to know the delivery cost.",
              "Okay, thanks.", "No need, I have everything I need."],
}, min_score=PAUSE_TURN.min_score, close_call=PAUSE_TURN.close_call)


CONTROL_SCOPE = Classifier("handoff reply scope", {
    "followup": [
        "No callback, thanks. What time do you open tomorrow?",
        "I don't want a call. Why don't you have tomorrow's schedule? Is it a public holiday?",
        "No, don't transfer me. Can you explain your opening hours?",
        "No thanks, I still need to know the delivery cost.",
        "No, I don't need a callback. Tell me the price instead.",
        "Okay, thanks. How long will the delivery take?",
        "Yes, that is already arranged. What sizes are available?",
        "No thanks. I have another question about my order.",
    ],
    "control": [
        "No, thank you.", "No, I don't want a callback.", "Don't transfer me.",
        "No need, I have everything I need.", "Please don't do that.",
        "Yes, please.", "Okay, thanks.", "Go ahead.", "That would help.",
        "Call me tomorrow instead.", "Transfer me now, please.", "Later, please.",
    ],
}, min_score=0.60, close_call=0.05)


def has_followup_request(text: str) -> Optional[bool]:
    """None leaves an uncertain full turn with the answer model instead of discarding it."""
    kinds = CONTROL_SCOPE.close(text)
    return True if kinds == ["followup"] else False if kinds == ["control"] else None
