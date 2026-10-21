"""Per-turn helpers shared by the Retell and custom voice pipelines.

- choose_voice_model: send turns that need no business facts (a name, a number, "yes", "okay") to a small fast
  model (VOICE_FAST_MODEL), and every question to the main model, so product answers keep its accuracy.
- NumberSpeaker: whatever the model writes, the caller's phone number is read back with exactly the digits the
  caller said, one digit at a time ("zero four one two, three four five, six seven eight").
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

_QUESTION_START = re.compile(
    r"^\s*(?:(?:and|so|but|okay|ok|um|uh|also|then|sorry|hey|hi|yeah|yes|well)[\s,.]+)*"
    r"(?:what|what's|whats|how|why|when|where|which|who|whose|do|does|did|can|could|would|will|is|are|was|were|have|has|"
    r"should|tell|explain|any|may|price|cost)\b",
    re.I,
)


def fast_voice_model() -> str:
    """VOICE_FAST_MODEL: the small model for turns without a question (empty = always the main model)."""
    return os.getenv("VOICE_FAST_MODEL", "").strip()


def needs_facts(caller_text: str) -> bool:
    """True when the caller asked something (a question needs the main model and the business knowledge)."""
    text = caller_text.strip()
    if not text or "?" in text:
        return True
    return any(_QUESTION_START.match(part) for part in re.split(r"(?<=[.!])\s+", text) if part.strip())


# Words that make up a pure acknowledgement or goodbye ("Okay, cool, thanks", "No, that's all. Bye.").
_ACK_WORDS = {"yes", "yeah", "yep", "yup", "no", "nope", "nah", "ok", "okay", "cool", "sure", "right", "alright", "fine", "good",
              "great", "nice", "perfect", "awesome", "thanks", "thank", "you", "cheers", "bye", "goodbye", "see", "ya", "that's",
              "thats", "it's", "its", "all", "nothing", "else", "correct", "sounds", "sorry", "hmm", "mm", "uh", "um", "oh",
              "so", "and", "that", "is", "it", "for", "now", "much", "very", "got", "i", "hello", "hi", "there", "lovely"}
# The agent just asked for one of the caller's details (name, number, email, a time): the reply is that detail.
_ASKED_FOR_DETAILS = re.compile(r"\b(?:name|number|phone|mobile|email|e-mail|spell|digit|day|time|suburb|postcode|address|order)\b", re.I)


def is_acknowledgement(caller_text: str) -> bool:
    words = re.findall(r"[a-z']+", caller_text.lower())
    return bool(words) and len(words) <= 8 and all(word in _ACK_WORDS for word in words)


def choose_voice_model(caller_text: str, agent_text: str = "") -> Optional[str]:
    """The fast model for this turn, or None for the main model. Only turns that need no knowledge go fast: an
    acknowledgement or goodbye, or the detail the agent just asked for. Anything else the caller tells us ("I'm doing
    up a small bathroom") gets the main model and the knowledge, like a question does."""
    fast = fast_voice_model()
    if not fast or needs_facts(caller_text):
        return None
    if _ASKED_FOR_DETAILS.search(agent_text or ""):
        return fast  # the name, number or email the agent just asked for
    if is_acknowledgement(caller_text):
        # "Yeah." / "No." answering the agent's own question needs real understanding of what was asked (the small
        # model replied "It sounds like you're looking for something,"): the main model. Goodbyes and thanks stay fast.
        if (agent_text or "").strip().endswith("?") and not _CLOSING.search(caller_text):
            return None
        return fast
    return None


_CLOSING = re.compile(r"\b(?:bye|goodbye|thanks|thank|cheers|that's all|thats all|nothing else)\b", re.I)


# ---- phone numbers -------------------------------------------------------------------------------------------

_DIGIT_WORDS = {"zero": "0", "oh": "0", "o": "0", "nought": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_REPEATS = {"double": 2, "triple": 3}
_SPOKEN = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TOKEN = r"(?<![A-Za-z])(?:\d+|zero|oh|nought|one|two|three|four|five|six|seven|eight|nine|double|triple)(?![A-Za-z])"
_SEPARATOR = r"[\s,.\-()]*"
# A run of digit tokens ("0412 345 678", "(0) (4) (1)...", "zero four one two, three ...").
_NUMBER_RUN = re.compile(rf"\(?{_TOKEN}\)?(?:{_SEPARATOR}\(?{_TOKEN}\)?)+", re.I)
_MIN_DIGITS = 8
# The same, with a leading "+" (a country code written as "+91"), so the "+" is replaced along with the number.
_PLUS_NUMBER_RUN = re.compile(r"(?:\+\s*)?" + _NUMBER_RUN.pattern, re.I)


def known_numbers(knowledge_text: str) -> frozenset:
    """Phone-length numbers written in the knowledge (the business's own lines): read out as written."""
    return frozenset(digits for digits in (_run_digits(m.group(0)) for m in _NUMBER_RUN.finditer(knowledge_text or ""))
                     if len(digits) >= _MIN_DIGITS)


def _run_digits(run: str) -> str:
    digits, repeat = [], 1
    for token in re.findall(r"\d+|[a-z]+", run.lower()):
        if token.isdigit():
            digits.append(token[0] * repeat + token[1:])
            repeat = 1
        elif token in _REPEATS:
            repeat = _REPEATS[token]
        elif token in _DIGIT_WORDS:
            digits.append(_DIGIT_WORDS[token] * repeat)
            repeat = 1
    return "".join(digits)


def spoken_number(text: str, min_digits: int = _MIN_DIGITS) -> str:
    """The longest phone-like digit string in what a caller said (digits or words, 'double eight'), or ''."""
    best = ""
    for match in _NUMBER_RUN.finditer(text):
        digits = _run_digits(match.group(0))
        if len(digits) >= min_digits and len(digits) > len(best):
            best = digits
    return best


def caller_number_digits(messages: List[Dict[str, str]]) -> str:
    """The phone number the caller most recently said in this conversation ('' if none)."""
    for item in reversed(messages):
        if item.get("role") == "user":
            digits = said_number(str(item.get("content") or ""))
            if digits:
                return digits
    return ""


def _close(left: str, right: str) -> bool:
    """Same number with at most two digits dropped, added or changed (the model's usual mistakes)."""
    if abs(len(left) - len(right)) > 2:
        return False
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1] <= 2


def say_digits(digits: str) -> str:
    """'0412345678' -> 'zero four one two, three four five, six seven eight' (Australian 4-3-3 grouping)."""
    if len(digits) == 10:
        groups = [digits[:4], digits[4:7], digits[7:]]
    else:
        groups = [digits[i:i + 3] for i in range(0, len(digits), 3)]
    return ", ".join(" ".join(_SPOKEN[int(d)] for d in group) for group in groups)


class NumberSpeaker:
    """Streaming filter for spoken replies: a number the model reads back that is (nearly) the caller's number is
    replaced by the caller's exact digits, said one by one. Other numbers (prices, the business's own) are left as
    written. Text that might be the start of a number is held back until the number is complete."""

    def __init__(self, caller_digits: str, known_numbers: frozenset = frozenset()) -> None:
        self.caller_digits = caller_digits
        self.known_numbers = known_numbers  # numbers written in the knowledge (the business's own): never replaced
        self.pending = ""

    def _rewrite(self, text: str) -> str:
        def replace(match: re.Match) -> str:
            digits = _run_digits(match.group(0))
            known = any(digits in number or number in digits for number in self.known_numbers if len(number) >= _MIN_DIGITS)
            # A phone-length number that is not the business's own can only be the caller's number being read back:
            # always say the caller's exact digits (a model once read "8866631491" back as "8866663149").
            if (len(digits) >= _MIN_DIGITS and not known) or (len(digits) >= _MIN_DIGITS - 2 and _close(digits, self.caller_digits)):
                return say_digits(self.caller_digits)
            return match.group(0)
        return _PLUS_NUMBER_RUN.sub(replace, text)

    def feed(self, chunk: str) -> str:
        if not self.caller_digits:
            return chunk
        self.pending += chunk
        # Hold back from the last digit token onwards while the number may still be going.
        # (A "+" goes with the number after it: "+91 ...".)
        tail = re.search(rf"(?:\+\s*)?\(?{_TOKEN}\)?(?:{_SEPARATOR}\(?{_TOKEN}\)?)*{_SEPARATOR}$|\+\s*$", self.pending, re.I)
        if tail:
            ready, self.pending = self.pending[:tail.start()], self.pending[tail.start():]
        else:
            ready, self.pending = self.pending, ""
        return self._rewrite(ready) if ready else ""

    def flush(self) -> str:
        out, self.pending = self.pending, ""
        return self._rewrite(out) if out and self.caller_digits else out


# ---- checking the caller's details -----------------------------------------------------------------------------

_PHONE_CONTEXT = re.compile(r"\b(?:number|phone|mobile|cell|call me|reach me|contact|digits)\b", re.I)
_EMAIL_CONTEXT = re.compile(r"@|\b(?:e-?mail|gmail|yahoo|hotmail|outlook|icloud|bigpond|dot com|dot au)\b", re.I)
_EMAIL = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}$")


# Phone numbers are checked with Google's libphonenumber (the `phonenumbers` package), which knows the number formats
# of every country, against the caller's own country: taken from their caller ID when the call has one, otherwise
# the business's country (VOICE_DEFAULT_REGION, e.g. AU). A number said with its country code is checked as such.

def home_region() -> str:
    """The business's own country ("" if not known): VOICE_DEFAULT_REGION, else the country of the business's phone
    number (TWILIO_PHONE_NUMBER). Callers can be anywhere; this is only the most likely country for a local caller."""
    configured = os.getenv("VOICE_DEFAULT_REGION", "").strip().upper()
    if configured:
        return configured
    return caller_region((os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_CALLER_ID") or "").strip())


def regions_where_valid(digits: str) -> List[str]:
    """Every country in which `digits`, said without a country code, is a valid phone number (often several)."""
    import phonenumbers
    found = []
    for region in sorted(phonenumbers.SUPPORTED_REGIONS):
        parsed = _parse(digits, region)
        if parsed and phonenumbers.is_valid_number(parsed):
            found.append(region)
    return found


def _region_name(region: str) -> str:
    try:
        from phonenumbers import geocoder
        return geocoder._region_display_name(region, "en") or region
    except Exception:
        return region


# Countries a caller may name ("it's an Indian number", "I'm calling from the UK"): their region codes.
_DEMONYMS = {"indian": "IN", "australian": "AU", "american": "US", "usa": "US", "canadian": "CA", "british": "GB",
             "uk": "GB", "england": "GB", "english": "GB", "kiwi": "NZ", "new zealand": "NZ", "pakistani": "PK",
             "bangladeshi": "BD", "sri lankan": "LK", "nepali": "NP", "filipino": "PH", "chinese": "CN", "irish": "IE",
             "singaporean": "SG", "malaysian": "MY", "indonesian": "ID", "german": "DE", "french": "FR", "dubai": "AE",
             "emirati": "AE", "uae": "AE", "south african": "ZA", "vietnamese": "VN", "thai": "TH", "japanese": "JP"}


def mentioned_region(texts: List[str]) -> str:
    """The country the caller named for their number or themselves, from the latest of `texts` that names one."""
    import phonenumbers
    names = {_region_name(region).lower(): region for region in phonenumbers.SUPPORTED_REGIONS}
    names.update(_DEMONYMS)
    for text in reversed([t for t in texts if t]):
        lowered = f" {text.lower()} "
        for name in sorted(names, key=len, reverse=True):
            if len(name) > 2 and re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lowered) or (len(name) == 2 and re.search(rf"(?<![a-z]){name}(?![a-z])", lowered) and name in _DEMONYMS):
                return names[name]
    return ""


def _parse(number: str, region: Optional[str] = None):
    try:
        import phonenumbers
        return phonenumbers.parse(number, region)
    except Exception:
        return None


def caller_region(caller_id: str) -> str:
    """The country (e.g. "AU", "IN") of a caller ID such as "+61412345678"; "" for a browser call or a hidden number."""
    import phonenumbers
    parsed = _parse(caller_id or "") if str(caller_id or "").startswith("+") else None
    return phonenumbers.region_code_for_number(parsed) or "" if parsed and phonenumbers.is_valid_number(parsed) else ""


def _country(parsed) -> str:
    """The country's name ("Australia"); for some mobile numbers the library has no name, so it comes from the region."""
    try:
        import phonenumbers
        from phonenumbers import geocoder
        return geocoder.country_name_for_number(parsed, "en") or geocoder._region_display_name(phonenumbers.region_code_for_number(parsed), "en")
    except Exception:
        return ""


def describe_caller_id(caller_id: str) -> str:
    """"+61 412 345 678 (Australia)" for the prompt; the number as given when it is not a phone number."""
    import phonenumbers
    parsed = _parse(caller_id or "") if str(caller_id or "").startswith("+") else None
    if not parsed or not phonenumbers.is_valid_number(parsed):
        return caller_id
    country = _country(parsed)
    formatted = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    return f"{formatted} ({country})" if country else formatted


def phone_info(digits: str, region: str = "") -> Optional[Dict[str, str]]:
    """The number's country and formats if `digits` is a valid phone number, read as a local number of `region`
    or, failing that, as an international number with its country code ("91 88666 31491"). None if not valid."""
    import phonenumbers
    for candidate, hint_region in ((digits, region or home_region()), ("+" + digits, None)):
        parsed = _parse(candidate, hint_region)
        if parsed and phonenumbers.is_valid_number(parsed):
            return {"region": phonenumbers.region_code_for_number(parsed) or "", "country": _country(parsed),
                    "e164": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
                    "international": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)}
    return None


def valid_phone(digits: str, region: str = "") -> bool:
    return phone_info(digits, region) is not None


def _format_hint(region: str) -> tuple[str, int]:
    """("Australia numbers look like 0412 345 678", 10): an example of a local mobile number and its digit count."""
    import phonenumbers
    region = region or home_region()
    example = phonenumbers.example_number_for_type(region, phonenumbers.PhoneNumberType.MOBILE)
    if not example:
        return "", 0
    national = phonenumbers.format_number(example, phonenumbers.PhoneNumberFormat.NATIONAL)
    country = _country(example) or region
    return f"{country} phone numbers look like {national}", len(re.sub(r"\D", "", national))


def spoken_email(text: str) -> str:
    """The email address in what a caller said, rebuilt from speech ('raj at gmail dot com', 'r a j at the rate
    gmail dot com', 'raj_m@gmail.com'), or '' when none was said."""
    if not _EMAIL_CONTEXT.search(text):
        return ""
    spoken = f" {text.lower()} "
    # Digits said as words: "rajm two six seven" -> "rajm 2 6 7" ("oh" is left alone: too often a real word).
    spoken = re.sub(r"\b(zero|one|two|three|four|five|six|seven|eight|nine)\b", lambda m: _DIGIT_WORDS[m.group(1)], spoken)
    spoken = re.sub(r"(?<![a-z0-9'’])([a-z0-9])\s+(?=[a-z0-9]\b)", r"\1", spoken)  # spelled out: "r a j" -> "raj"
    # "at the rate" (Indian English for @), also as speech recognition hears it before a mail provider ("at direct gmail").
    spoken = re.sub(r"\s+at\s+(?:the\s+rate(?:\s+of)?|direct|the\s+red|rate)\s+(?=[a-z0-9-]+\s*(?:\.|dot\b))", "@", spoken)
    spoken = re.sub(r"\s+at\s+", "@", spoken)
    spoken = re.sub(r"\s*\b(?:dot|period)\b\s*", ".", spoken)
    spoken = re.sub(r"\s*\bunderscore\b\s*", "_", spoken)
    spoken = re.sub(r"\s*\b(?:dash|hyphen)\b\s*", "-", spoken)
    # The name part: everything said after "my email is" (callers say it in pieces: "raj m 267747"), else the last word.
    intro = re.search(r"\be-?mail(?:\s+address)?\s+(?:is|it's|its)?\s*([a-z0-9._%+\- ]+?)\s*@([a-z0-9.\-]*)", spoken)
    if intro:
        return f"{intro.group(1).replace(' ', '')}@{intro.group(2)}".strip(".")
    match = re.search(r"([a-z0-9._%+\-]+)@([a-z0-9.\-]*)", spoken)
    return f"{match.group(1)}@{match.group(2)}".strip(".") if match else ""


def say_email(email: str) -> str:
    """'raj.m@gmail.com' -> 'raj dot m at gmail dot com' (how to read it back)."""
    return email.replace(".", " dot ").replace("@", " at ").replace("_", " underscore ").replace("-", " dash ")


def said_number(text: str, min_digits: int = _MIN_DIGITS) -> str:
    """The phone number in an utterance, joining groups the caller broke up ("eight eight, right? six six six three
    one four nine one" is one number). Single stray digit words ("one question") are not joined in."""
    groups = [_run_digits(match.group(0)) for match in _NUMBER_RUN.finditer(text)]
    joined = "".join(group for group in groups if len(group) >= 2)
    longest = spoken_number(text, min_digits=1)
    best = joined if len(joined) >= len(longest) else longest
    return best if len(best) >= min_digits else ""


def earlier_caller_texts(messages: List[Dict[str, str]], current: str) -> List[str]:
    """What the caller said before this turn (the conversation may or may not already end with this turn)."""
    texts = [str(item.get("content") or "") for item in messages if item.get("role") == "user"]
    if texts and texts[-1].strip() == current.strip():
        texts = texts[:-1]
    return texts


def contact_check(caller_text: str, agent_text: str = "", earlier: List[str] = (), region: str = "") -> tuple[str, bool]:
    """Check a phone number or email the caller just gave. Returns (note for the model, problem found).
    The note tells the model the exact details to read back, or that what was heard is not valid and must be asked
    for again, so neither model accepts a wrong number or email."""
    notes: List[str] = []
    problem = False
    digits = said_number(caller_text, min_digits=6)
    if digits and (_PHONE_CONTEXT.search(caller_text) or _PHONE_CONTEXT.search(agent_text)):
        # Calls come from anywhere. The caller's country, best evidence first: their caller ID (`region`), a country
        # they named ("it's an Indian number"), then the business's own country as the likeliest for a local caller.
        known_country = region or mentioned_region([*earlier, caller_text])
        region = known_country or home_region()
        hint, usual = _format_hint(region)
        info = phone_info(digits, region)
        # Shorter than a local number is most likely an incomplete one, not a foreign one: ask for the rest.
        valid_elsewhere = [] if info or known_country or (usual and len(digits) < usual) else regions_where_valid(digits)
        if info:
            where = f"a valid {info['country']} number" if info["country"] else "a valid number"
            if info["region"] and info["region"] != region:
                where += " (an international number with its country code, which is fine: do not call it wrong or too long)"
            notes.append(f"Phone number heard: {say_digits(digits)}, {where}. Read it back exactly like that and ask if it is right. "
                         f"With its country code it is {info['international']}: that is what the team will use, so never ask "
                         "the caller for a country code.")
        elif valid_elsewhere:
            # Not a number of the business's country, and the caller's country is not known (no caller ID, none
            # named): it is a valid number in other countries, which a number alone cannot tell apart.
            places = ", ".join(_region_name(code) for code in valid_elsewhere[:6])
            problem = True  # needs care: the main model
            notes.append(f"Phone number heard: {say_digits(digits)}. It is a valid phone number in {places}, but the caller's "
                         "country is not known (no caller ID). Do not call it wrong. Read it back, then naturally ask which country "
                         "the number is in (for example 'and is that an Indian number?' only if they mentioned India); never ask "
                         "for a 'country code'.")
        elif any(said_number(text, min_digits=6) == digits for text in earlier):
            # Said the same way twice: the caller knows their own number. Never loop on it.
            notes.append(f"Phone number heard again: {say_digits(digits)}. The caller has now said this same number twice. It is not in "
                         f"the usual format ({hint}), so it may be an overseas number. Do not reject it again: read it back "
                         "digit by digit and ask the caller to confirm it is right.")
        else:
            problem = True
            size = "" if not usual else " (too many digits)" if len(digits) > usual else " (too few digits)" if len(digits) < usual else ""
            notes.append(f"Phone number heard: {say_digits(digits)}. That is {len(digits)} digits{size} and NOT in the usual format ({hint}). "
                         "Do not accept it or say it is noted yet. In one short sentence tell the caller why it doesn't look right "
                         "(use the format just described), and ask them to say the full number again, or to confirm it if it is an overseas number.")
    email = spoken_email(caller_text)
    if email:
        if _EMAIL.match(email):
            notes.append(f"Email heard: {email}. Read it back as '{say_email(email)}' and ask if it is right.")
        else:
            problem = True
            notes.append(f"Email heard: '{say_email(email)}', which is NOT a complete email address (it needs a name, 'at', and an "
                         "ending such as dot com). Do not accept it or say it is noted. Politely ask the caller to say the full email again.")
    if not notes:
        return "", False
    return "Check of the caller's details (done by the system; trust it over your own reading): " + " ".join(notes), problem


def last_agent_text(messages: List[Dict[str, str]]) -> str:
    """What the agent said last (to know whether it had just asked for a number or an email)."""
    for item in reversed(messages):
        if item.get("role") == "assistant":
            return str(item.get("content") or "")
    return ""


# ---- places the knowledge does not mention ---------------------------------------------------------------------

_PLACE_QUESTION = re.compile(r"\b(?:deliver\w*|ship\w*|send|install\w*|fit\w*|store|stores|showroom|shop|branch|office|"
                             r"pick ?up|collect\w*|service\w*|cover\w*|available|come out|travel)\b", re.I)
_PLACE_AFTER = re.compile(r"\b(?:in|to|at|near|around|into|from|for|out to)\s+((?:[A-Z][a-zA-Z'-]+)(?:\s+[A-Z][a-zA-Z'-]+){0,2})")


def unknown_places(caller_text: str, knowledge_text: str) -> List[str]:
    """Places the caller named in a delivery/installation/store question that the knowledge never mentions
    ("Do you install in Brisbane?" with no Brisbane in the knowledge)."""
    if not _PLACE_QUESTION.search(caller_text):
        return []
    places = []
    for match in _PLACE_AFTER.finditer(caller_text):
        place = match.group(1).strip()
        # Known only as a place of its own: "Sydney Road" in an address does not mean the knowledge covers Sydney.
        mentioned = re.search(rf"\b{re.escape(place)}\b(?!\s+(?:road|rd|street|st|avenue|ave|highway|hwy|drive|dr|lane|ln|parade|pde|way|place|pl)\b)",
                              knowledge_text, re.I)
        if not mentioned and place.lower() not in {"i", "the", "my", "our", "your"}:
            places.append(place)
    return places


def place_note(caller_text: str, knowledge_text: str, agent_text: str = "") -> str:
    """A note for this turn only, so the model does not say a place it knows nothing about is covered. Not while the
    caller is giving their details ("it's an India number, nine one ..."): that is about a phone number, not an area."""
    if _ASKED_FOR_DETAILS.search(agent_text or "") or said_number(caller_text, min_digits=4) or _PHONE_CONTEXT.search(caller_text):
        return ""
    places = unknown_places(caller_text, knowledge_text)
    if not places:
        return ""
    names = " and ".join(places)
    return (f"The caller asked about {names}. The business knowledge does not mention {names}, so you do not know whether "
            f"{names} is covered. Do not say yes, 'including {places[0]}', 'should be fine' or 'covered' for it, and do not say no. "
            "Say what the knowledge says in general (for example nationwide or most of Australia) and that the team will "
            "confirm for their area.")


def careful_voice_model(default: str = "") -> str:
    """VOICE_CAREFUL_MODEL: the model for turns where saying too much is risky (a place the knowledge does not
    mention). Empty: the fallback model."""
    return os.getenv("VOICE_CAREFUL_MODEL", "").strip() or default


# ---- acknowledgements: something said while the answer is being prepared -------------------------------------
#
# Context-aware, rule-based (no extra model call): the caller's words decide the kind of acknowledgement, the last few
# are never repeated, and simple questions, answers to the agent, short replies and sensitive issues get none at all.
# Phrases never claim to be checking something ("let me check"), and are only said if the answer is not ready yet.

# Looking for a product or a recommendation.
SEARCH_BRIDGES = ("Let's see what would work best for you.", "Let me think about what would suit you.")
# Explaining a problem they have (not an urgent or upset one).
PROBLEM_BRIDGES = ("I see what you mean. Let's work through that.", "Ah, I see. Let's sort that out.")
# Several things at once, or a long, detailed request.
DETAIL_BRIDGES = ("There are a couple of things to consider here.", "Got it, I'll take all of that into account.")
# Telling us about their situation or plans ("I just bought a new home").
STATEMENT_BRIDGES = ("Ah, okay.", "Got it.")  # neutral: "Oh, nice." did not fit news that was not good news
# Asking for a callback, a quote or a person.
REQUEST_BRIDGES = ("Sure, I can help with that.", "Yeah, I can sort that out.")
# Asking the agent to explain or repeat itself.
CLARIFY_BRIDGES = ("Oh, sorry.", "Ah, sure.")
# Said once if the answer still has not started a while after the caller finished (VOICE_STATUS_AFTER_MS).
STATUS_PHRASES = ("I'm working through the details now.", "Just putting that together for you.")
ALL_BRIDGES = (*SEARCH_BRIDGES, *PROBLEM_BRIDGES, *DETAIL_BRIDGES, *STATEMENT_BRIDGES, *REQUEST_BRIDGES, *CLARIFY_BRIDGES, *STATUS_PHRASES)

_SEARCH = re.compile(r"\b(?:looking for|look for|need (?:a|an|some|something)|want (?:a|an|some|something)|recommend\w*|suggest\w*|"
                     r"best|suit|which (?:one|ones|basin|tap|style)|what kind|what type|options? for|something for|ideas?)\b", re.I)
_PROBLEM = re.compile(r"\b(?:problem|issue|trouble|not working|doesn't work|does not work|stopped working|struggling|stuck|"
                      r"can't figure|confused|not sure (?:how|what|which))\b", re.I)
# Upset, urgent or money/damage matters: no casual acknowledgement, the answer itself must handle it.
_SENSITIVE = re.compile(r"\b(?:broken|damaged|faulty|leak\w*|flood\w*|refund|complain\w*|angry|upset|terrible|worst|urgent|"
                        r"emergency|wrong (?:item|order|product)|never (?:arrived|came)|missing|scam|lawyer|unhappy|disappointed)\b", re.I)
_CLARIFY = re.compile(r"\b(?:what do you mean|what does that mean|what you mean|meaning|sorry\?|pardon|say that again|say again|"
                      r"repeat|didn't (?:get|catch|hear|understand)|don't understand|what was that|come again)\b", re.I)
_REQUEST = re.compile(r"\b(?:call me|call back|callback|ring me|arrange|book|meeting|speak to|talk to|someone|quote|contact me)\b", re.I)
# The caller answering the agent's question ("Yes, please go ahead", "No, that's fine"): reply directly.
_ANSWER_START = re.compile(r"^\s*(?:yes|yeah|yep|yup|no|nope|nah|okay|ok|sure|alright|fine)\b", re.I)
_bridge_turn = 0


def bridge_delay_seconds() -> float:
    """VOICE_BRIDGE_DELAY_MS: wait this long after the caller's turn ends before an acknowledgement. If they carry on
    talking in that moment, or the answer is ready by then, nothing is said; the answer is being prepared meanwhile."""
    try:
        return max(0.0, float(os.getenv("VOICE_BRIDGE_DELAY_MS", "350"))) / 1000
    except ValueError:
        return 0.35


def status_after_seconds() -> float:
    """VOICE_STATUS_AFTER_MS: a short status update ("I'm working through the details now.") if the answer still has
    not started this long after the caller finished (0 = never)."""
    try:
        return max(0.0, float(os.getenv("VOICE_STATUS_AFTER_MS", "2500"))) / 1000
    except ValueError:
        return 2.5


def bridges_enabled() -> bool:
    """VOICE_BRIDGE: say a short acknowledgement that fits what the caller said, while the answer is prepared."""
    return os.getenv("VOICE_BRIDGE", "true").strip().lower() not in {"0", "false", "no", "off"}


def bridge_kind(caller_text: str) -> str:
    """Which acknowledgement fits ("" = none): rule-based, instant."""
    words = re.findall(r"[a-z']+", caller_text.lower())
    if is_acknowledgement(caller_text) or len(words) < 4:
        return ""  # "Hello?", "Yes", "So what?": answer straight away
    if _SENSITIVE.search(caller_text):
        return ""  # upset, urgent, damage or money: no casual filler
    if _ANSWER_START.match(caller_text) and len(words) <= 14 and "?" not in caller_text:
        return ""  # answering the agent's question: reply straight away
    if _CLARIFY.search(caller_text):
        return "clarify"
    if _REQUEST.search(caller_text):
        return "request"
    if _PROBLEM.search(caller_text):
        return "problem"
    if len(words) >= 22 or len(re.findall(r"\b(?:and|also|plus)\b", caller_text.lower())) >= 2:
        return "detail"
    if _SEARCH.search(caller_text):
        return "search"
    if needs_facts(caller_text):
        return ""  # a simple question: the answer itself comes quickly enough
    return "statement"


_KIND_PHRASES = {"search": SEARCH_BRIDGES, "problem": PROBLEM_BRIDGES, "detail": DETAIL_BRIDGES, "statement": STATEMENT_BRIDGES,
                 "request": REQUEST_BRIDGES, "clarify": CLARIFY_BRIDGES, "status": STATUS_PHRASES}


def bridge_phrase(caller_text: str, recent=()) -> str:
    """A short acknowledgement that fits what the caller said, or "" when none should be said. Never one of the
    recently used phrases (`recent`: a phrase or a list of the last few)."""
    global _bridge_turn
    recent = {recent} if isinstance(recent, str) else set(recent or ())
    kind = "status" if caller_text == "__status__" else bridge_kind(caller_text)
    if not kind:
        return ""
    options = _KIND_PHRASES[kind]
    _bridge_turn += 1
    for step in range(len(options)):
        phrase = options[(_bridge_turn + step) % len(options)]
        if phrase not in recent:
            return phrase
    return ""  # every fitting phrase was used just now: silence sounds better than a repeat


def status_phrase(recent=()) -> str:
    return bridge_phrase("__status__", recent)


def bridge_note(bridge: str) -> str:
    """Tells the model the reply has already begun with the bridge, so it continues instead of repeating it."""
    return (f"You (the agent) have already said \"{bridge}\" out loud as a short acknowledgement; it is not something the "
            "caller said. Continue straight on with the actual answer: do not repeat or rephrase it, and do not claim to "
            "have checked or looked anything up.")


FAST_TURN_NOTE = (
    "Reply in one or two short sentences. Never repeat anything you already said earlier in the call (no second 'check "
    "our website'), and do not repeat the caller's answer back to them ('You're looking for something specific'). If the "
    "caller just said yes, okay or yeah to your question, take the next step: ask for the detail "
    "you asked about, or ask one short new question that moves the conversation on; never answer a yes with 'Not yet'. "
    "If you are taking the caller's details for a callback, ask for the next missing detail only: their name, then their "
    "phone number ONLY if there is no caller ID (with caller ID the number is already saved: never ask for it). Never say "
    "anything is arranged, booked or passed on until you have a way to reach them (caller ID or a number they gave). If the caller "
    "is saying goodbye or thanks at the end, just say a warm goodbye."
)


# ---- the business name, misheard ------------------------------------------------------------------------------

def _similarity(left: str, right: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, left, right).ratio()


def name_note(caller_text: str, name: str) -> str:
    """When the caller's words contain something that sounds like the business name ("home data", "Home Depot",
    "Home BFI", "whom DFO" for "Home DFO"), a note for this turn so the model answers about the business instead of
    asking what they meant. Speech recognition often gets an unusual business name wrong."""
    name_words = re.findall(r"[a-z0-9]+", (name or "").lower())
    words = re.findall(r"[a-z0-9]+", caller_text.lower())
    if not name_words or not words:
        return ""
    target = "".join(name_words)
    if target in "".join(words):
        return ""  # said (and heard) correctly
    for size in {len(name_words), len(name_words) + 1}:
        for start in range(0, max(len(words) - size + 1, 0)):
            window = words[start:start + size]
            score = _similarity("".join(window), target)
            # Starting with the name's own first word ("home ...") is a strong hint; otherwise it must be very close.
            if score >= (0.6 if window[0] == name_words[0] and len(name_words) > 1 else 0.75):
                heard = " ".join(window)
                return (f"The caller said \"{heard}\", which is most likely the business name \"{name}\" misheard by "
                        f"speech recognition: treat it as \"{name}\" and answer; do not ask what they meant.")
    return ""


def caller_country_line(caller_id: str) -> str:
    """One line for the agent's instructions: which country the caller is in (from caller ID), so phone numbers they
    say are understood as that country's and the caller is never asked for a country code."""
    region = caller_region(caller_id)
    if region:
        import phonenumbers
        parsed = _parse(caller_id)
        country = _country(parsed) if parsed else region
        number = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL) if parsed else caller_id
        return (f"The caller is calling from {country} (caller ID {number}). We already have their phone number from caller ID "
                "and it is saved with the call: never ask for their phone number or a country code. For a callback, ask only "
                "for their name, and for an email only if it helps. If they want to be called on a different number, take "
                f"that one instead (numbers they say are {country} numbers unless they give another country code).")
    home = home_region()
    local = f" The business is in {_region_name(home)}, so numbers are checked against {_region_name(home)} formats first." if home else ""
    return ("Calls can come from any country, and this caller's country is not known (no caller ID): do not assume one. "
            f"A phone number they give is checked for every country by the system, which tells you what it found.{local} "
            "Never ask the caller for a 'country code'; if the system says the country is unclear, just ask naturally which "
            "country the number is in.")
