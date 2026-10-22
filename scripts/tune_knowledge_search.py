"""Measure which split between meaning and shared words finds the right knowledge passage, on the live knowledge.

    python scripts/tune_knowledge_search.py

Each caller question has the start of the passage(s) that answer it. For every split from 0% to 100% meaning it reports
how often the right passage comes first (top 1), is among the passages the AI is given (top 4), and how many off-topic
questions wrongly get a passage (they must get none). Set the winner as KNOWLEDGE_MEANING_PERCENT.
The questions are written the way callers talk, mostly without the passage's own words.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from env_loader import load_project_env  # noqa: E402

load_project_env()

import re  # noqa: E402

from brain_service import brain_service  # noqa: E402

# (question, starts of the passages that answer it)
CASES = [
    ("how long till my stuff gets here", ["Faqs: How long does delivery take"]),
    ("can I grab it from your shop myself", ["Faqs: Can I pick up my order"]),
    ("what cards do you take", ["Faqs: How do I pay"]),
    ("is there anything I can't send back", ["Faqs: What can't be returned"]),
    ("my tap came damaged, can I swap it", ["Faqs: How do exchanges work"]),
    ("I'm a plumber, can I work with you guys", ["Faqs: Can I become an installation partner"]),
    ("do you come out to Ballarat", ["Faqs: Do you deliver to or install in my city"]),
    ("where's your warehouse", ["Locations:"]),
    ("when are you open", ["Working Hours"]),
    ("what do you guys sell", ["Business Description", "Company Information"]),
    ("what size tiles have you got", ["Services: Tiles"]),
    ("which basin suits a tiny bathroom", ["Custom Knowledge: Basin buying advice"]),
    ("is a ceramic basin better than stone", ["Custom Knowledge: Basin materials"]),
    ("how do I keep a black shower head clean", ["Custom Knowledge: Shower finishes"]),
    ("what should I check before buying a toilet", ["Custom Knowledge: Toilet suite tips"]),
    ("the courier left it outside and it got stolen", ["Policies: Home DFO is not responsible"]),
    ("how much is that mixer", ["Policies: Product prices"]),
    ("do you have guides to help me choose", ["Services: Buying guides"]),
    ("what's your phone number", ["Working Hours", "Company Information"]),
    ("who are you", ["Company Information", "Business Description"]),
    ("is it safe to pay on your website", ["Website Content", "Faqs: How do I pay"]),
]
# Questions the knowledge has nothing on: they must get no passage.
OFF_TOPIC = ["what's the weather tomorrow", "can you recommend a restaurant in Mumbai", "who won the football last night",
             "how do I fix my car engine", "what's the capital of France", "tell me a joke"]


def ask(question: str, share: float):
    query_tokens = {token for token in re.findall(r"[a-z0-9]{3,}", question.lower())}
    return brain_service._local_retrieve(question, query_tokens, 4, meaning_share=share) or []


def main() -> None:
    brain_service.ensure_local_embeddings()
    starts = [" ".join(str(item.get("text", "")).split())[:60] for item in brain_service._knowledge_documents()]
    cases = [(question, right) for question, right in CASES if any(s.startswith(tuple(right)) for s in starts)]
    print(f"{len(cases)} questions over {len(starts)} passages, {len(OFF_TOPIC)} off-topic questions\n")
    print(f"{'meaning %':>9} {'words %':>8} {'top 1':>7} {'top 4':>7} {'off-topic answered':>19}")
    results = {}
    for percent in range(0, 101, 10):
        share = percent / 100
        top1 = top4 = 0
        misses = []
        for question, right in cases:
            found = [" ".join(hit["text"].split()) for hit in ask(question, share)]
            ranks = [rank for rank, text in enumerate(found, 1) if text.startswith(tuple(right))]
            top1 += bool(ranks and ranks[0] == 1)
            top4 += bool(ranks)
            if not ranks or ranks[0] != 1:
                misses.append(f"{question!r} -> {(found[0][:50] if found else 'nothing')}")
        wrong = sum(1 for question in OFF_TOPIC if ask(question, share))
        results[percent] = (top1, top4, wrong, misses)
        print(f"{percent:>8}% {100 - percent:>7}% {top1:>4}/{len(cases)} {top4:>4}/{len(cases)} {wrong:>12}/{len(OFF_TOPIC)}")
    best = max(results, key=lambda percent: (results[percent][1] - results[percent][2], results[percent][0], percent))
    print(f"\nBest: {best}% meaning / {100 - best}% words")
    for miss in results[best][3]:
        print(f"  not first: {miss}")


if __name__ == "__main__":
    main()
