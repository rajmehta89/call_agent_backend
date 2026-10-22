"""Measure which split between meaning and words ranks products best, on the live catalogue.

    python scripts/tune_store_search.py

Each question has a check for what a right product looks like (e.g. a basin, not a basin mixer). For every split from
0% to 100% meaning, it reports how often the first product is right (top 1), whether a right one is in the first 3
(top 3), and the average rank of the first right product (MRR). Questions the store has no right product for are left
out. Set the winner as STORE_SEARCH_MEANING_PERCENT (default in shopify_service.ShopifyService.MEANING_PERCENT_DEFAULT).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from env_loader import load_project_env  # noqa: E402

load_project_env()

from routers.shopify_api import _apply_saved_shopify_config  # noqa: E402
from shopify_service import shopify_service as shop  # noqa: E402


def has(*words, without=()):
    """A right product's title has all these words and none of the `without` ones."""
    return lambda title: all(word in title for word in words) and not any(word in title for word in without)


def has_any(*words, without=()):
    return lambda title: any(word in title for word in words) and not any(word in title for word in without)


# (question, what a right product is). Plain names, descriptions without the product's name, follow-ups with the
# earlier product, a misheard word, and finishes or specs.
CASES = [
    ("matte black basin", has("basin", without=("mixer", "spout", "waste"))),
    ("I want to buy a basin. I need the smaller one.", has("basin", without=("mixer", "spout", "waste"))),
    ("basin list down all the business and the pricing", has("basin", without=("mixer", "spout", "waste"))),
    ("something to wash my hands in", has_any("basin", without=("mixer", "spout", "waste"))),
    ("black kitchen sink", has("sink", without=("mixer", "waste"))),
    ("freestanding bath", has_any("bathtub", "bath ", without=("mixer", "spout"))),
    ("a bath tap that stands on the floor", has_any("freestanding", "floor", without=("bathtub",))),
    ("tap for the kitchen that pulls out", has_any("pull", without=("sink",))),
    ("lydia mixer in gold", has("lydia")),
    ("solid brass wall mixer with a diverter", has("mixer", "diverter")),
    ("mirror with lights", has_any("led")),
    ("a door for the front of my house", has_any("door")),
    ("toilet suite", has_any("toilet")),
    ("rain shower set", has("shower", without=("arm",))),
    ("something to boil water for tea at the sink", has_any("boiling")),
    ("tiles for my bathroom floor", has_any("tile")),
    ("waterproof flooring", has_any("flooring", "floor")),
    ("900 vanity", has("vanity")),
    ("floor drain", has("drain")),
    ("cooktop for my kitchen", has_any("cooktop")),
    ("hand held shower", has_any("hand shower", "handheld", "hand-held")),
    # Described, not named: the caller says what it does or where it goes, not what the store calls it.
    ("a cabinet to go under my basin", has_any("vanity", "cabinet")),
    ("a bath that isn't attached to the wall", has("freestanding", without=("mixer", "spout"))),
    ("a bath that fits in the corner of the room", has("corner")),
    ("something to hang my towels on", has_any("towel")),
    ("a tap that gives me filtered boiling and chilled water", has_any("boiling", "chilled", "filtered")),
    ("a tap for my bathroom sink", has_any("basin mixer", "basin mi")),
    ("a toilet that cleans itself", has_any("smart")),
    ("something to hold the toilet roll", has_any("paper", "roll holder")),
    ("a handle to open my door", has("handle")),
    ("the drain in the floor of my shower", has("drain")),
    ("big grey tiles for a modern floor", has_any("tile")),
    ("lightweight panels for building walls", has_any("aac", "panel")),
    ("a shower with a big overhead head and a hand one on a rail", has_any("station", "shower set", "rail")),
    ("a mirror that lights up", has_any("led")),
]


def main() -> None:
    _apply_saved_shopify_config()
    shop._build_catalog_words()
    for _ in range(3):  # a dropped connection while loading the catalogue: try again
        shop._build_index()  # also builds the meaning vectors
        if shop._index[0]:
            break
    if not shop._index[0]:
        sys.exit("Could not load the catalogue from Shopify; try again.")
    titles = [(product.get("title") or "").lower() for product in shop._index[0]]
    cases = [(question, right) for question, right in CASES if any(right(title) for title in titles)]
    skipped = [question for question, right in CASES if not any(right(title) for title in titles)]
    print(f"{len(cases)} questions over {len(titles)} products" + (f" (skipped, nothing in the store fits: {', '.join(skipped)})" if skipped else ""))
    results = {}
    for percent in range(0, 101, 10):
        top1 = top3 = mrr = 0.0
        misses = []
        for question, right in cases:
            found = shop._local_search(question, shop._search_terms(question), 10, meaning_share=percent / 100)
            ranks = [rank for rank, product in enumerate(found, 1) if right((product.get("title") or "").lower())]
            first = ranks[0] if ranks else None
            top1 += first == 1
            top3 += bool(first and first <= 3)
            mrr += 1 / first if first else 0
            if first != 1:
                misses.append(f"{question!r} -> {(found[0]['title'] if found else 'nothing')[:40]}")
        results[percent] = (top1 / len(cases), top3 / len(cases), mrr / len(cases), misses)
    print(f"\n{'meaning %':>9} {'words %':>8} {'top 1':>7} {'top 3':>7} {'MRR':>6}")
    for percent, (top1, top3, mrr, _) in results.items():
        print(f"{percent:>8}% {100 - percent:>7}% {top1:>7.0%} {top3:>7.0%} {mrr:>6.2f}")
    best = max(results, key=lambda percent: (round(results[percent][0], 3), round(results[percent][2], 3), -abs(percent - 50)))
    print(f"\nBest: {best}% meaning / {100 - best}% words (top 1 {results[best][0]:.0%}, top 3 {results[best][1]:.0%})")
    for miss in results[best][3]:
        print(f"  not first: {miss}")


if __name__ == "__main__":
    main()
