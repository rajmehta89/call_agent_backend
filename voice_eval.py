"""Truth checks for spoken answers: does a reply say only what the knowledge and live store data support?

Used by scripts/eval_voice_answers.py (real questions, real models, timed) and covered by tests/test_voice_eval.py.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Small spoken numbers, so "fifteen years" is checked like "15 years".
_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
          "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
          "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
# Numbers that carry no fact ("one or two", "a couple of things", "24/7" is still checked).
_HARMLESS = {"0", "1", "2", "3"}


# Groups of digits split by spaces, dashes or brackets (not dots: "131.40-159.30" is a price range, not a phone number).
_PHONE_RUN = re.compile(r"\(?\+?\d{1,4}\)?(?:[\s-]*\(?\d{1,4}\)?){2,}")


def numbers_in(text: str) -> List[str]:
    """Every number in a text, normalised: "$1,299.00" -> "1299", "15-year" -> "15", "fifteen" -> "15". A phone-style run
    ("(04) 2221 3831", "0422 213 831") counts as its whole digit string, so the same number grouped differently matches."""
    found: List[str] = []
    for match in _PHONE_RUN.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) >= 8:
            found.append(digits.lstrip("0"))
    text = _PHONE_RUN.sub(lambda match: " " if len(re.sub(r"\D", "", match.group(0))) >= 8 else match.group(0), text or "")
    for match in re.finditer(r"\d[\d,]*(?:\.\d+)?", text or ""):
        value = match.group(0).replace(",", "")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        found.append(value.lstrip("0") or "0")
    for word in re.findall(r"[a-z]+", (text or "").lower()):
        if word in _WORDS:
            found.append(str(_WORDS[word]))
    return found


def ungrounded_numbers(answer: str, sources: str) -> List[str]:
    """Numbers in the answer that appear nowhere in what the model was given: invented prices, sizes, years or dates."""
    allowed = set(numbers_in(sources))
    # A price may be said rounded ("about $159" for 159.21): allow the whole-number part of any source number.
    allowed |= {value.split(".")[0] for value in allowed}
    return sorted({value for value in numbers_in(answer) if value not in allowed and value not in _HARMLESS})


@dataclass
class Case:
    """One caller question and what a true answer must (and must not) contain."""
    name: str
    question: str
    kind: str
    phone: str = "unknown"
    earlier: List[Dict[str, str]] = field(default_factory=list)
    must: List[str] = field(default_factory=list)            # every one of these (case-insensitive)
    must_any: List[str] = field(default_factory=list)        # at least one of these
    must_not: List[str] = field(default_factory=list)        # none of these
    check_numbers: bool = True                               # every number must come from the sources


def judge(case: Case, answer: str, sources: str) -> Dict[str, object]:
    """Pass/fail for one answer, with the reasons."""
    text = (answer or "").lower()
    problems: List[str] = []
    missing = [item for item in case.must if item.lower() not in text]
    if missing:
        problems.append("missing " + ", ".join(repr(item) for item in missing))
    if case.must_any and not any(item.lower() in text for item in case.must_any):
        problems.append("none of " + ", ".join(repr(item) for item in case.must_any))
    said = [item for item in case.must_not if item.lower() in text]
    if said:
        problems.append("said " + ", ".join(repr(item) for item in said))
    invented: Optional[List[str]] = ungrounded_numbers(answer, sources) if case.check_numbers else []
    if invented:
        problems.append("numbers not in the data: " + ", ".join(invented))
    if not answer.strip():
        problems.append("no answer")
    return {"ok": not problems, "problems": problems}
