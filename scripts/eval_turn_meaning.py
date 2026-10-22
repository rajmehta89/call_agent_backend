"""Old keyword rules against the meaning versions, on caller phrasings neither was built from.

    python scripts/eval_turn_meaning.py

For each decision made about a call turn it prints how many cases each version gets right, and every case where they
differ. Policies are read from the connected store (that part is skipped if no store is connected).
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from env_loader import load_project_env  # noqa: E402

load_project_env()
os.environ.setdefault("VOICE_FAST_MODEL", "meta-llama/llama-3.1-8b-instruct")

import voice_turns  # noqa: E402
from shopify_service import _dimension_asked, _keyword_dimension_asked  # noqa: E402

FAST, MAIN = "fast", "main"

# (what the caller said, our reply before it, which model should answer)
ROUTING = [
    ("Okay.", "We have basins from $40 to $145.", FAST),
    ("Mmm okay", "It comes in 1500 and 1700 mm sizes.", FAST),
    ("Yes.", "Would you like me to have the team call you?", MAIN),
    ("Sure, go ahead and book it", "Shall I book a callback for you?", MAIN),
    ("Thanks so much, bye.", "Is there anything else I can help with?", FAST),
    ("lovely, cheers mate", "The team will call you tomorrow.", FAST),
    ("nah that's all good thanks", "Anything else today?", FAST),
    ("It's Raj Mehta.", "May I have your name?", FAST),
    ("eight eight six six six three one four nine one", "What's the best number to reach you on?", FAST),
    ("raj at gmail dot com", "What email should we send the quote to?", FAST),
    ("tomorrow arvo works", "What time suits you for the callback?", FAST),
    ("my postcode is 3061", "What's the delivery postcode?", FAST),
    ("DFO001110", "What's your order number?", MAIN),
    ("buyer at example dot com", "What's the email on the order?", MAIN),
    ("I'm doing up a small bathroom", "How can I help you today?", MAIN),
    ("how much is the lydia mixer", "", MAIN),
    ("can you tell me the price", "We have a few options.", MAIN),
    ("so what kind of basins do you have", "", MAIN),
    ("what do you mean", "It's a wall-hung model.", MAIN),
    ("hang on, is that the black one", "That one is $129.", MAIN),
    ("yeah but does it come in white", "It's available in black.", MAIN),
    ("cool, and what about delivery to Perth", "That's in stock.", MAIN),
]

# (what the caller said, which acknowledgement fits; "" = none, answer straight away)
BRIDGES = [
    ("I'm looking for a vanity that fits a tight ensuite", "search"),
    ("we want something modern for the main bathroom", "search"),
    ("honestly I have no idea which one would work for me", "problem"),
    ("I'm trying to figure out whether the 900 vanity will fit", "problem"),
    ("the tap you sent is dripping non stop", ""),
    ("the delivery guy dropped it and now it's chipped", ""),
    ("could someone ring me tomorrow about a bulk order", "request"),
    ("I'd love a quote for three bathrooms", "request"),
    ("sorry what was that you said", "clarify"),
    ("can you explain that again in simpler terms", "clarify"),
    ("we've just moved in and the bathroom is really dated", "statement"),
    ("my builder recommended you guys", "statement"),
    ("yes that's right", ""),
    ("what are your opening hours on Saturday", ""),
    ("my name is Priya Sharma", ""),
]

# (what the caller said, our question before it, is it their phone number?)
PHONE = [
    ("eight eight six six six three one four nine one", "What's the best number to reach you on?", True),
    ("you can reach me on 0412345678", "", True),
    ("ring me on oh four one two three four five six seven eight", "", True),
    ("it's 0412 345 678", "Could you confirm your mobile?", True),
    ("my order number is 1114 2233", "What's your order number?", False),
    ("the basin is 400 by 400 by 120", "", False),
    ("I paid 1299 dollars on 21 10 2026", "", False),
    ("the order number is DFO 00111 0", "", False),
]

# (what the caller said, are they asking whether something covers a place?)
PLACES = [
    ("can you get it out to my place in Geelong", True),
    ("do you guys ship over to Tasmania", True),
    ("is there a showroom in Adelaide", True),
    ("what's the freight like to Cairns", True),
    ("can your installers come to Bendigo", True),
    ("I'm calling from Sydney", False),
    ("it's an Indian number", False),
    ("I live in Melbourne and need a basin", False),
    ("my name is Sydney", False),
    ("how much is delivery", False),
]

# (what the caller said, which single measurement they ask for; "" = none)
DIMENSIONS = [
    ("how far across is the bath", "width"),
    ("what does it measure front to back", "depth"),
    ("how tall is the mixer", "height"),
    ("what's the length of the oval tub", "length"),
    ("how big is it", ""),
    ("what sizes does it come in", ""),
    ("is the high back rest one comfortable", ""),
    ("how much room will it take up across my wall", "width"),
]

# (what the caller asked, words that the right policy's title contains)
POLICIES = [
    ("can I send it back if I don't like it", "refund"),
    ("do I get my money back if it's faulty", "refund"),
    ("how long until it arrives", "ship"),
    ("do you charge for postage", "ship"),
    ("what do you do with my personal data", "privacy"),
    ("what are the rules for buying from your website", "terms"),
]


def score(name, cases, old, new):
    old_right = new_right = 0
    differs = []
    for case in cases:
        *inputs, want = case
        before, after = old(*inputs), new(*inputs)
        old_right += before == want
        new_right += after == want
        if before != after or after != want:
            differs.append(f"    {'ok ' if after == want else 'XX '} {inputs[0][:52]!r:<56} keywords: {before!r:<12} meaning: {after!r:<12} want: {want!r}")
    print(f"{name:<34} keywords {old_right}/{len(cases)}   meaning {new_right}/{len(cases)}")
    for line in differs:
        print(line)
    return old_right, new_right, len(cases)


def main() -> None:
    totals = []
    route = lambda fn: (lambda text, agent: FAST if fn(text, agent) else MAIN)
    totals.append(score("Which model answers", ROUTING, route(voice_turns._keyword_choose_voice_model), route(voice_turns.choose_voice_model)))
    totals.append(score("Which acknowledgement", BRIDGES, voice_turns._keyword_bridge_kind, voice_turns.bridge_kind))
    keyword_phone = lambda text, agent: bool(voice_turns._PHONE_CONTEXT.search(text) or voice_turns._PHONE_CONTEXT.search(agent))
    totals.append(score("Is it their phone number", PHONE, keyword_phone, voice_turns.about_phone))
    totals.append(score("Asking about a place", PLACES, lambda text: bool(voice_turns._PLACE_QUESTION.search(text)), voice_turns.is_place_question))
    totals.append(score("Which measurement", DIMENSIONS, _keyword_dimension_asked, _dimension_asked))
    try:
        from routers.shopify_api import _apply_saved_shopify_config
        from shopify_service import shopify_service
        import meaning
        _apply_saved_shopify_config()
        policies = shopify_service.policies() if shopify_service.configured else []
    except Exception as exc:
        policies = []
        print(f"(policies skipped: {exc})")
    if policies:
        def keyword_policy(question):
            wanted = [p for p in policies if any(word in f"{p['type']} {p['title']}".lower() for word in re.findall(r"[a-z]+", question.lower()))] or policies
            return next((key for key in ("refund", "ship", "privacy", "terms") if key in wanted[0]["title"].lower()), wanted[0]["title"])
        def meaning_policy(question):
            from brain_service import brain_service
            first = brain_service._policy_context(question).split("\n")[1]  # what the AI is given first
            title = first.split("]")[0].strip("[").lower()
            return next((key for key in ("refund", "ship", "privacy", "terms") if key in title), title)
        totals.append(score("Which policy", POLICIES, keyword_policy, meaning_policy))
    old = sum(t[0] for t in totals); new = sum(t[1] for t in totals); n = sum(t[2] for t in totals)
    print(f"\nAll decisions: keywords {old}/{n} ({old / n:.0%})   meaning {new}/{n} ({new / n:.0%})")


if __name__ == "__main__":
    main()
