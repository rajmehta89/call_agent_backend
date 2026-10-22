"""What a caller wants from the store, by meaning rather than by keywords.

Each turn (and the turn before it, for follow-ups such as "has it been posted yet?") is compared by meaning with short
example requests of each kind, using the local embedding model the knowledge search already runs (~7 ms a turn, no
network). The examples describe kinds of request any shop gets; they hold no facts about this business, so they never
need editing when the products, prices or policies change.
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

EXAMPLES: Dict[str, List[str]] = {
    "product": [
        "How much does it cost?", "What's the price of that?", "What would one of those set me back?", "How much are they?",
        "Is it in stock?", "Do you have that available?", "What sizes does it come in?", "Does it come in black?",
        "Which one would you recommend?", "I'm looking for something for my bathroom.", "What are the dimensions?", "How wide is it?",
        "What's the warranty on that one?", "What options do you have?", "Can you list what you have and the prices?",
        "I want to buy one.", "Do you sell those?", "Do you have anything cheaper?", "Tell me more about that product.",
        "What is it made of?", "I need the smaller one.", "Something to wash my hands in.", "Is it any good?",
        "What goes with it?", "Do you have it in a different colour?",
    ],
    "order": [
        "Where is my order?", "Has my parcel shipped yet?", "Has it been posted yet?", "Has it been dispatched?",
        "When will my delivery arrive?", "Is my package on its way?", "I haven't received what I ordered.",
        "The thing I bought from you is broken.", "What I got is leaking.", "I want to send back what I bought.",
        "Did I get my refund for my order?", "Can you check the status of my purchase?", "I ordered something last week and it's not here.",
        "I paid but nothing came.", "Can you track my delivery?", "What did I order?",
    ],
    "policy": [
        "What's your return policy?", "Can I return something if I change my mind?", "Can I exchange it for something else?",
        "How does shipping work?", "How long does delivery usually take?", "Do you give refunds?", "What are your terms and conditions?",
        "Do you deliver to my area?", "How much is postage?", "What warranty do you give on your products in general?",
        "What's your privacy policy?",
    ],
    "none": [
        "Hello?", "Who am I speaking to?", "What time do you close?", "Where is your store?", "What's your phone number?",
        "Thanks, bye.", "Can someone call me back?", "My name is John.", "Yes.", "Okay.", "What does your company do?",
        "Can I speak to a person?", "My email is john at gmail dot com.", "Are you a robot?", "Sorry, I didn't catch that.",
        # About the business itself (the knowledge base answers these, not the store's catalogue).
        "What services do you offer?", "Do you do installation?", "Can I become a trade partner?", "Are you open on weekends?",
        "Do you have a showroom I can visit?", "How long have you been in business?",
        # Off-topic chat any shop's caller might try: nothing in the store answers it.
        "Who won the game last night?", "What's the weather like today?", "Tell me a joke.", "What's in the news?",
        "Who is the president?", "What's the score in the cricket?",
    ],
}
from meaning import Classifier

# Two kinds this close in meaning (0.06) are both fetched, the model uses whichever fits; below 0.55 nothing matches.
STORE = Classifier("store intent", EXAMPLES, min_score=0.55, close_call=0.06)


def rank(text: str, earlier: str = "") -> Optional[List[Tuple[str, float]]]:
    """Each kind of request with how close the caller's words are to it, best first; None when the local model is
    unavailable (the caller then falls back to word matching)."""
    return STORE.rank(text, earlier)


CLEAR_LEAD = 0.03  # how far a kind of store request must lead "none" to count as clearly a store question


def clearly_wanted(text: str, earlier: str = "") -> bool:
    """True only when a store request is the best match and clearly ahead of "none". wanted() fetches on a close call
    as well (better too much data than too little); saying "let me check" needs this firmer answer, so an off-topic
    question ("who won the football?") is answered straight away."""
    ranked = rank(text, earlier)
    if not ranked:
        return False
    best, score = ranked[0]
    if best == "none" or score < STORE.min_score:
        return False
    none_score = next((value for kind, value in ranked if kind == "none"), 0.0)
    return score - none_score >= CLEAR_LEAD


def wanted(text: str, earlier: str = "") -> Optional[List[str]]:
    """The kind(s) of store data this turn needs: one, or two when they are a close call; [] for none; None when the
    meaning model is unavailable."""
    kinds = STORE.close(text, earlier)
    if kinds is None:
        return None
    chosen = [kind for kind in kinds if kind != "none"]
    logger.debug("store intent %r -> %s", text[:60], chosen or "none")
    return chosen
