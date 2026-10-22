"""Ask real caller questions through the exact prompt calls use, against the live store and each model, and grade
every spoken answer for truth (voice_eval.judge) and speed.

    python scripts/eval_voice_answers.py                      # the main and fallback voice models
    python scripts/eval_voice_answers.py --models openai/gpt-4.1-mini,google/gemini-3.1-flash-lite --only order

Cases are built from the connected store each run (its products, a delivered / cancelled / unshipped order, that order's
email and billing phone), so they stay true as the catalogue changes. Customer details are never printed; the full
results (questions, answers, timings) go to logs/evals/. Costs a few cents of model usage per run.
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from env_loader import load_project_env  # noqa: E402

load_project_env()

import phonenumbers  # noqa: E402

from ai_services import AIServices  # noqa: E402
from brain_service import brain_service  # noqa: E402
from routers.retell_api import _max_response_tokens, _system_prompt, configure_voice_services, voice_temperature, voice_turn_reminder  # noqa: E402
from routers.shopify_api import _apply_saved_shopify_config  # noqa: E402
from shopify_service import shopify_service as shop  # noqa: E402
from voice_eval import Case, judge  # noqa: E402

TEAM = ["team", "confirm", "check", "get back", "follow up", "find out", "look into"]


def caller_id(phone: str) -> str:
    """A billing phone ("0412 345 678") as caller ID shows it (+61412345678)."""
    try:
        return phonenumbers.format_number(phonenumbers.parse(phone, os.getenv("VOICE_DEFAULT_REGION") or "AU"), phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        return phone


def first_product(query: str):
    found = shop.search_products(query, limit=1)
    return found[0] if found else None


def build_cases() -> list:
    """Questions from the live store, with what a true answer must contain."""
    cases = []
    shop._build_catalog_words()
    shop._build_index()  # as the server does at startup

    # ---- products: names, sizes, specs, prices, stock
    mixer = first_product("lydia wall mixer with diverter")
    if mixer:
        warranty = next((spec for spec in mixer.get("specs") or [] if spec.lower().startswith("warranty")), "")
        years = "".join(ch for ch in warranty.split(":", 1)[-1] if ch.isdigit())
        if years:
            cases.append(Case("product warranty", f"What's the warranty on the {mixer['title']}?", "product", must=[years], must_any=["warrant"]))
        colours = next((option["values"] for option in mixer.get("options") or [] if option["name"].lower() in ("color", "colour")), [])
        if colours:
            cases.append(Case("product colours", f"What colours does the {mixer['title']} come in?", "product", must=colours[:2]))
        low = mixer.get("min_price")
        if low and float(low) > 0:
            cases.append(Case("product price", f"How much is the {mixer['title']}?", "product", must=[str(low).split(".")[0]]))
    bath = first_product("freestanding oval bathtub")
    if bath:
        sizes = next((option["values"] for option in bath.get("options") or [] if option["name"].lower() == "size"), [])
        if sizes:
            cases.append(Case("product sizes", f"What sizes does the {bath['title']} come in?", "product", must=sizes[:2]))
        cases.append(Case("detail not listed", f"How wide is the {bath['title']}?", "product", must_any=TEAM + ["not listed", "isn't listed", "don't have", "not sure"],
                          must_not=[f"{size} mm wide" for size in sizes] + [f"{size}mm wide" for size in sizes] + [f"{size} millimetres wide" for size in sizes]))
    tile = next((product for product in shop.search_products("porcelain tile", limit=10) if not float(product.get("max_price") or 0)), None)
    if tile:
        cases.append(Case("$0 price never quoted", f"How much is the {tile['title']}?", "product", must_any=["quote"] + TEAM,
                          must_not=["$0", "0 dollars", "zero dollars", "free of charge", "it's free"]))
    cacey = first_product("cacey short basin mixer")
    if cacey:
        cases.append(Case("copied description not used", f"What's the warranty on the {cacey['title']}?", "product", must_any=TEAM,
                          must_not=["15 year", "15-year", "fifteen"]))
    basin = first_product("matte black basin 360")
    if basin and "360" in basin["title"]:
        # Its title says 360x360x120 and its specifications 355*355*120: either is true to the store's data.
        cases.append(Case("measurements", f"What are the measurements of the {basin['title']}?", "product", must=["120"], must_any=["355", "360"]))
    sold_out = next((product for product in shop.search_products("mixer", limit=10) if product.get("total_inventory") == 0 and float(product.get("max_price") or 0) > 0), None)
    if sold_out:
        cases.append(Case("sold out", f"Is the {sold_out['title']} in stock?", "product", must_any=["sold out", "out of stock", "not in stock", "none in stock", "isn't in stock"]))
    cases.append(Case("described, not named", "Got any black kitchen sinks?", "product", must_any=["sink"]))
    # A real call: prices asked as follow-ups, one with "basins" misheard as "business".
    talked = [{"role": "user", "content": "Yeah. I want to buy a basin."},
              {"role": "assistant", "content": "We have a range of basins in different styles and sizes. Are you looking for a particular style?"}]
    no_price = ["can't provide", "cannot provide", "can't give", "don't have the exact price", "check our website", "on our website", "listed on our website"]
    cases.append(Case("follow-up: pricing", "I need to know the pricing.", "pricing", earlier=talked, must_any=["$", "dollar", "aud"], must_not=no_price))
    cases.append(Case("follow-up: list with prices (misheard)", "Okay. So please list down all the business that you have. And also the pricing for that.", "pricing",
                      earlier=talked, must_any=["$", "dollar", "aud"], must_not=no_price))
    cases.append(Case("follow-up: the smaller one", "I need the smaller one.", "pricing", earlier=talked, must_any=["basin"]))
    cases.append(Case("not sold here", "Do you sell garden hoses?", "product", must_not=["yes, we do", "yes we do", "we sell garden hoses"], check_numbers=False))

    # ---- orders: every state, and who may hear about them
    orders = shop._orders(None, 50)
    def pick(test):
        return next((order for order in orders if test(order) and order["_emails"]), None)
    delivered = pick(lambda order: order["status"].startswith("Delivered"))
    cancelled = pick(lambda order: order["cancelled"])
    unshipped = pick(lambda order: order["status"].startswith("Not shipped") and "refund" not in order["status"])
    refunded = pick(lambda order: "refund" in order["status"] and not order["cancelled"])
    if delivered:
        email = delivered["_emails"][0]
        asked = [{"role": "user", "content": f"Where is my order {delivered['name']}?"}, {"role": "assistant", "content": "I can check that. What's the email address on the order?"}]
        day = delivered["status"].split("Delivered", 1)[1].strip().split(" ")[0]
        cases.append(Case("order delivered (email verified)", f"It's {email}", "order", earlier=asked, must=["deliver"] + ([day] if day.isdigit() else [])))
        cases.append(Case("order withheld (not verified)", f"Where is my order {delivered['name']}?", "order", must_any=["email"],
                          must_not=["delivered", delivered["total"].split(".")[0]]))
        if delivered["_phones"]:
            cases.append(Case("intent: problem with a purchase", "The thing I got from you is leaking, what should I do?", "order",
                              phone=caller_id(delivered["_phones"][0]), must_any=["return", "warranty", "replace", "refund"] + TEAM))
    if cancelled and cancelled["_phones"]:
        cases.append(Case("order cancelled (caller ID)", "Hi, where's my order?", "order", phone=caller_id(cancelled["_phones"][0]), must=["cancel"]))
    if unshipped:
        asked = [{"role": "user", "content": f"Has my order {unshipped['name']} shipped?"}, {"role": "assistant", "content": "Let me check. What's the email on the order?"}]
        cases.append(Case("order not shipped (email verified)", f"It's {unshipped['_emails'][0]}", "order", earlier=asked,
                          must_any=["not shipped", "hasn't shipped", "not been shipped", "hasn't been shipped", "not yet", "yet to ship", "hasn't been sent", "not been sent"],
                          must_not=["delivered", "on its way", "on the way"]))
    if refunded and refunded["_phones"]:
        cases.append(Case("order refunded (caller ID)", "Did I get my money back for my order?", "order", phone=caller_id(refunded["_phones"][0]), must=["refund"]))
    cases.append(Case("order not found", "Where is my order DFO009999?", "order", must_any=["couldn't find", "can't find", "not find", "no order", "double-check", "check the number", "find"]))

    # ---- policies and general questions
    cases.append(Case("refund policy", "What's your refund policy?", "policy", must_any=["return", "refund"]))
    cases.append(Case("delivery time", "How long does delivery usually take?", "policy", must_any=["deliver", "shipping", "days"]))
    cases.append(Case("opening hours (not listed)", "What time do you close today?", "general", must_not=["5 pm", "5pm", "6pm", "6 pm", "9 am", "9am"]))
    cases.append(Case("store address", "Where is your store?", "general", must=["Campbellfield"]))
    cases.append(Case("store phone", "What's your phone number?", "general", must_any=["2221 3831", "0422213831", "04 2221 3831"]))
    return cases


def ask(services: AIServices, model: str, case: Case) -> dict:
    started = time.perf_counter()
    system = _system_prompt(case.question, case.phone, "", case.earlier, True)
    prompt_ms = (time.perf_counter() - started) * 1000
    reminder = voice_turn_reminder()
    messages = [{"role": "system", "content": system}, *case.earlier, {"role": "user", "content": case.question}] + ([{"role": "system", "content": reminder}] if reminder else [])
    asked, first, answer = time.perf_counter(), None, ""
    for delta in services.stream_chat_completion(messages, _max_response_tokens(model), voice_temperature(), 1.0, model=model):
        if first is None and delta.strip():
            first = time.perf_counter()
        answer += delta
    done = time.perf_counter()
    sources = " ".join([system, *(item["content"] for item in case.earlier), case.question])
    verdict = judge(case, answer, sources)
    return {"case": case.name, "kind": case.kind, "model": model, "question": case.question, "answer": answer.strip(), **verdict,
            "prompt_ms": round(prompt_ms), "first_words_ms": round(((first or done) - asked) * 1000), "total_ms": round((done - asked) * 1000)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default=",".join(dict.fromkeys(m for m in (os.getenv("RETELL_OPENROUTER_MODEL", "openai/gpt-4.1-mini"), os.getenv("VOICE_FALLBACK_MODEL")) if m)))
    parser.add_argument("--only", default="", help="only cases whose name or kind contains this")
    parser.add_argument("--repeat", type=int, default=1, help="ask every question this many times (answers vary run to run)")
    args = parser.parse_args()
    _apply_saved_shopify_config()
    cases = [case for case in build_cases() if args.only.lower() in f"{case.name} {case.kind}".lower()]
    print(f"{len(cases)} cases from the live store ({shop.store_domain})\n")
    results = []
    for model in [name.strip() for name in args.models.split(",") if name.strip()]:
        services = configure_voice_services(AIServices(), model)
        services.fallback_model = model  # time this model alone: no fallback to another
        list(services.stream_chat_completion([{"role": "user", "content": "Say ok."}], 5, 0.0, 1.0, model=model))  # warm the connection
        print(f"== {model}")
        for case in [case for case in cases for _ in range(max(1, args.repeat))]:
            try:
                row = ask(services, model, case)
            except Exception as exc:  # one failed request is a failed case, not a failed run
                row = {"case": case.name, "kind": case.kind, "model": model, "question": case.question, "answer": "", "ok": False,
                       "problems": [f"error: {exc}"], "prompt_ms": 0, "first_words_ms": 0, "total_ms": 0}
            results.append(row)
            mark = "PASS" if row["ok"] else "FAIL"
            print(f"  {mark}  {row['case']:<36} words in {row['first_words_ms']:>5} ms, done {row['total_ms']:>5} ms (prompt {row['prompt_ms']} ms)"
                  + ("" if row["ok"] else f"  <- {'; '.join(row['problems'])}"))
        print()

    print("Summary (truth = answers that passed; times in ms, median / slowest)")
    for model in dict.fromkeys(row["model"] for row in results):
        rows = [row for row in results if row["model"] == model]
        passed = sum(1 for row in rows if row["ok"])
        firsts = [row["first_words_ms"] for row in rows if row["first_words_ms"]]
        totals = [row["total_ms"] for row in rows if row["total_ms"]]
        prompts = [row["prompt_ms"] for row in rows if row["prompt_ms"]]
        print(f"  {model:<36} truth {passed}/{len(rows)} ({passed / len(rows):.0%})  |  first words {statistics.median(firsts):.0f} / {max(firsts)}  |  full answer {statistics.median(totals):.0f} / {max(totals)}"
              f"  |  prompt + store lookup {statistics.median(prompts):.0f} / {max(prompts)}" if firsts else f"  {model}: no answers")
    if args.repeat > 1:
        print("\nQuestions that were not right every time (passes per model)")
        for name in dict.fromkeys(row["case"] for row in results):
            per_model = {model: [row["ok"] for row in results if row["case"] == name and row["model"] == model] for model in dict.fromkeys(row["model"] for row in results)}
            if not all(all(oks) for oks in per_model.values()):
                print(f"  {name:<36} " + "  ".join(f"{model.split('/')[-1]} {sum(oks)}/{len(oks)}" for model, oks in per_model.items()))
    out = ROOT / "logs" / "evals" / f"voice-answers-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nFull questions and answers: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
