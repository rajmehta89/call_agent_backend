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
    """The fast model for this turn, or None for the main model, decided by what the caller MEANS (turn_meaning):
    only a goodbye, a plain "okay" that answers no question of ours, or the detail we just asked for (name, number,
    email, a time, an address) goes fast. Everything else, including anything about an order (looked up in the store),
    gets the main model and the knowledge."""
    fast = fast_voice_model()
    if not fast:
        return None
    is_simple = _simple_turn(caller_text, agent_text)
    if is_simple is None:  # the meaning model is unavailable: decide from words
        return _keyword_choose_voice_model(caller_text, agent_text)
    return fast if is_simple else None


DETAILS_WE_ASK_FOR = ("name", "phone", "email", "time", "address")


def main_sentence(caller_text: str) -> str:
    """The caller's point: their last sentence of three words or more ("Yeah. So what is your business for?" -> the
    question), or everything when it is one short sentence."""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", caller_text or "") if part.strip()]
    full = [sentence for sentence in sentences if len(sentence.split()) >= 3]
    return full[-1] if full else (caller_text or "").strip()


def kind_of_turn_text(caller_text: str) -> str:
    """The caller's point (main_sentence) with what they talk about made generic, so the decision is about the KIND of
    thing they said: the store's own product words become "a product" ("Can someone call me back about a vanity?" is a
    callback request, not a product search) and the business's name becomes "your company". Both lists come from the
    business's data (the Shopify catalogue, the knowledge key terms), nothing is written here."""
    text = main_sentence(caller_text)
    try:
        from voice_keyterms import business_name
        name = business_name()
        if name and len(name) > 2:
            text = re.sub(re.escape(name), "your company", text, flags=re.I)
        from shopify_service import shopify_service
        kinds = shopify_service.product_kinds() if shopify_service.configured else frozenset()
        if kinds:
            singular = lambda word: word[:-1] if word.endswith("s") and not word.endswith("ss") else word
            text = re.sub(r"[A-Za-z]{4,}", lambda match: "product" if singular(match.group(0).lower()) in kinds else match.group(0), text)
    except Exception:
        pass
    return text


def _simple_turn(caller_text: str, agent_text: str = "") -> Optional[bool]:
    """True when the turn needs no knowledge (see choose_voice_model); None when the meaning model is unavailable."""
    from turn_meaning import AGENT_ASKED, CALLER_TURN
    import store_intent
    kind = CALLER_TURN.best(kind_of_turn_text(caller_text))
    if kind is None:
        return None
    asked = AGENT_ASKED.best(agent_text) if (agent_text or "").strip() else ""
    if asked in ("order", "order_email") or "order" in (store_intent.wanted(caller_text) or []):
        return False  # an order number, or the email on an order, is looked up in the store
    if kind == "closing":
        return True
    if kind == "acknowledge":
        # "Yeah." answering our own question needs real understanding of what was asked: the main model.
        return not (agent_text or "").strip().endswith("?")
    if kind == "detail":
        return asked in DETAILS_WE_ASK_FOR
    return False


def _keyword_choose_voice_model(caller_text: str, agent_text: str = "") -> Optional[str]:
    """Fallback for choose_voice_model when the local meaning model cannot load."""
    fast = fast_voice_model()
    if not fast or needs_facts(caller_text) or _ORDER_TALK.search(f"{caller_text} {agent_text or ''}"):
        return None  # an order number or email for an order is looked up in the store: main model, with the lookup
    if _ASKED_FOR_DETAILS.search(agent_text or ""):
        return fast  # the name, number or email the agent just asked for
    if is_acknowledgement(caller_text):
        # "Yeah." / "No." answering the agent's own question needs real understanding of what was asked (the small
        # model replied "It sounds like you're looking for something,"): the main model. Goodbyes and thanks stay fast.
        if (agent_text or "").strip().endswith("?") and not _CLOSING.search(caller_text):
            return None
        return fast
    return None


_FAREWELL = re.compile(r"\b(?:good)?bye\b|\bhave a (?:great|good|lovely|nice|wonderful) (?:day|one|evening|night|weekend|afternoon)\b|"
                       r"\btake care\b", re.I)


_AFFIRM = re.compile(r"^\W*(?:yes|yeah|yep|yup|sure|ok|okay|alright|please|go ahead|sounds good)\b", re.I)


def yes_to_offer_note(caller_text: str, agent_text: str) -> str:
    """A note for this turn when the caller said yes to what we offered ("Would you like the sizes or the stock?"
    "Yeah."). From real calls: the model answered such a "Yeah" by repeating its previous answer almost word for word."""
    question = agent_question(agent_text)
    if not question.endswith("?") or not is_acknowledgement(caller_text) or not _AFFIRM.match(caller_text):
        return ""
    if detail_asked(agent_text) not in ("", "order"):
        return ""  # "Can I take your name?" "Yes." is handled by the details rules
    return (f"The caller just said yes to your question \"{question}\". Do that now: give the new information you offered "
            "(if you offered two things, cover both briefly). Do not repeat or rephrase your previous answer.")


def search_text(caller_text: str, messages: List[Dict[str, str]]) -> str:
    """The words the knowledge and store search use for this turn. A yes to our offer ("Yeah." to "Would you like the
    colours or prices for those?") is searched as the offer plus the caller's earlier question, so the facts offered
    are found: searching "Yeah." found nothing, and the agent said to check the website for prices the store has."""
    agent_text = last_agent_text(messages)
    if not yes_to_offer_note(caller_text, agent_text):
        return caller_text
    earlier = earlier_caller_texts(messages, caller_text)
    return " ".join(part for part in (earlier[-1] if earlier else "", agent_question(agent_text)) if part)


def agent_said_goodbye(agent_text: str) -> bool:
    """Our own last reply was a farewell ("Have a great day, Raj. Goodbye."), not a question."""
    text = (agent_text or "").strip()
    return bool(text) and "?" not in text and bool(_FAREWELL.search(text))


def call_is_over(caller_text: str, agent_text: str) -> bool:
    """Both sides have said goodbye: we already said it and the caller only says okay, thanks or bye back. From a
    real call: "Okay. / Thank you. / Bye. / Okay." got four goodbyes in a row, one per fragment. Anything that could
    be more than a goodbye (a question, a new request) keeps the call going."""
    if not agent_said_goodbye(agent_text) or needs_facts(caller_text):
        return False
    if is_acknowledgement(caller_text):
        return True
    from turn_meaning import CALLER_TURN
    return CALLER_TURN.best(kind_of_turn_text(caller_text)) == "closing"


_ORDER_TALK = re.compile(r"\border(?:s|ed)?\b", re.I)
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


def reply_known_numbers(knowledge_text: str, prompt: str, caller_digits: str) -> frozenset:
    """Numbers a reply may read out as written: the knowledge's, plus those in this turn's prompt (live store data such
    as a tracking number or order total). The caller's own number is left out, so a misread of it is still corrected."""
    tail = (caller_digits or "")[-8:]
    return frozenset(number for number in known_numbers(f"{knowledge_text}\n{prompt}") if not tail or tail not in number)


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
            if len(digits) >= _MIN_DIGITS - 2 and self.caller_digits and _close(digits, self.caller_digits):
                return say_digits(self.caller_digits)
            if len(digits) >= _MIN_DIGITS and not known:
                # Nearly one of the business's own numbers (a model once said the shop's "(04) 2221 3831" as
                # "0422 221 383"): say that number exactly as written in the knowledge.
                near = next((number for number in self.known_numbers if len(number) >= _MIN_DIGITS and _close(digits, number)), None)
                if near:
                    return say_digits(near)
                # Any other phone-length number can only be the caller's number being read back: always say the
                # caller's exact digits (a model once read "8866631491" back as "8866663149").
                if self.caller_digits:
                    return say_digits(self.caller_digits)
            return match.group(0)
        return _PLUS_NUMBER_RUN.sub(replace, text)

    def feed(self, chunk: str) -> str:
        if not self.caller_digits and not self.known_numbers:
            return chunk  # nothing to protect (no caller number, no business numbers)
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
        return self._rewrite(out) if out and (self.caller_digits or self.known_numbers) else out


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


# Words that come before the name part of a spoken email ("yeah, it is raj m 267747 at gmail dot com"), never in it.
_EMAIL_LEAD_IN = {"is", "it's", "its", "it", "email", "e-mail", "mail", "address", "my", "the", "so", "yeah", "yes", "yep", "okay",
                  "ok", "um", "uh", "and", "that's", "thats", "id", "me", "at", "on", "use", "please", "sure", "right", "well"}


def spoken_email(text: str) -> str:
    """The email address in what a caller said, rebuilt from speech, or '' when none was said. Callers say it in
    pieces: the name part ("raj m two six seven seven four seven"), "at" (or "at the rate", which speech recognition may
    hear as "at direct"), the provider (sometimes split: "g mail", "out look") and the ending ("dot com dot au")."""
    if not _EMAIL_CONTEXT.search(text):
        return ""
    spoken = f" {text.lower()} "
    # Digits said as words: "rajm two six seven" -> "rajm 2 6 7" ("oh" is left alone: too often a real word).
    spoken = re.sub(r"\b(zero|one|two|three|four|five|six|seven|eight|nine)\b", lambda m: _DIGIT_WORDS[m.group(1)], spoken)
    # "at the rate (of)" (Indian English for @), also as speech recognition hears it ("at direct", "at the red", "at rate").
    spoken = re.sub(r"\s+at\s+(?:the\s+rate(?:\s+of)?|direct|the\s+red|rate)\s+", " @ ", spoken)
    spoken = re.sub(r"\s+at\s+", " @ ", spoken)
    spoken = re.sub(r"\s*\b(?:dot|period)\b\s*", " . ", spoken)
    spoken = re.sub(r"\s*\bunderscore\b\s*", " _ ", spoken)
    spoken = re.sub(r"\s*\b(?:dash|hyphen)\b\s*", " - ", spoken)
    spoken = spoken.replace("@", " @ ")
    tokens = [token.strip(",;:!?") for token in spoken.split()]
    tokens = [token for token in tokens if token]
    if "@" not in tokens:
        return ""
    at = len(tokens) - 1 - tokens[::-1].index("@")  # the last "at": the one before the provider
    # The name part: the pieces just before "@", back to the words that introduce it.
    name: List[str] = []
    for token in reversed(tokens[:at]):
        if token == ".":  # a spoken "dot" inside the name ("john dot smith")
            name.insert(0, ".")
            continue
        piece = token.strip(".")  # "m." : a full stop speech recognition put after a piece
        if not piece:
            continue
        if piece in _EMAIL_LEAD_IN or not re.fullmatch(r"[a-z0-9._%+\-]+", piece):
            break
        name.insert(0, piece)
    # The provider and ending: the pieces after "@" up to the last ending ("gmail . com", "g mail . com . au").
    domain: List[str] = []
    for index, token in enumerate(tokens[at + 1:]):
        if not re.fullmatch(r"[a-z0-9.\-]+", token):
            break
        domain.append(token)
        rest = tokens[at + 2 + index:]
        if "." in domain and token != "." and not (rest and rest[0] == "."):
            break  # an ending was said and no further "dot" follows: the address is complete
    local, host = "".join(name).strip("."), "".join(domain).strip(".")
    return f"{local}@{host}" if local or host else ""


def say_email(email: str) -> str:
    """How to read an email back so the caller can check it by ear: 'rajm267747@gmail.com' -> 'r a j m, two six seven
    seven four seven, at gmail dot com'. In the name part, digits are said one by one and short letter groups are
    spelled (a whole word like 'john' stays a word); the provider and ending are said as words."""
    local, _, host = email.partition("@")
    pieces = []
    for chunk in re.findall(r"[a-z]+|\d+|[._\-]", local.lower()):
        if chunk.isdigit():
            pieces.append(" ".join(_SPOKEN[int(digit)] for digit in chunk) + ",")
        elif chunk in "._-":
            pieces.append({".": "dot", "_": "underscore", "-": "dash"}[chunk])
        else:
            pieces.append(" ".join(chunk) + "," if len(chunk) <= 4 else chunk)
    spoken_host = host.replace(".", " dot ").replace("-", " dash ")
    return re.sub(r"\s+", " ", f"{' '.join(pieces)} at {spoken_host}").replace(", at", " at").strip()


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


def about_phone(caller_text: str, agent_text: str = "") -> bool:
    """Whether the caller is giving a phone number (not an order number, postcode, price or size), by meaning: what
    they said, or our question just before ("What's the best number to reach you?")."""
    from turn_meaning import AGENT_ASKED, CONTACT
    said = CONTACT.best(caller_text)
    asked = AGENT_ASKED.best(agent_text) if (agent_text or "").strip() else ""
    if said is None or asked is None:  # the meaning model is unavailable: decide from words
        return bool(_PHONE_CONTEXT.search(caller_text) or _PHONE_CONTEXT.search(agent_text or ""))
    return said == "phone" or (asked == "phone" and said != "email")


def provided_contact_phone(caller_text: str, agent_text: str = "", earlier: List[str] = (), region: str = "") -> str:
    """Return the caller's valid spoken contact number in E.164, or an empty string."""
    digits = said_number(caller_text, min_digits=9)
    if not digits or not about_phone(caller_text, agent_text):
        return ""
    country = region or mentioned_region([*earlier, caller_text]) or home_region()
    info = phone_info(digits, country)
    return info["e164"] if info else ""


def agent_question(agent_text: str) -> str:
    """What our last reply asked: its last question, or the whole reply when it asked none. "You can pick up your order
    from our store. Would you like details on collection?" asked about collection, not for an order number."""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", agent_text or "") if part.strip()]
    questions = [sentence for sentence in sentences if sentence.endswith("?")]
    return questions[-1] if questions else (agent_text or "").strip()


def detail_asked(agent_text: str) -> str:
    """Which of the caller's details our last reply asked for ("" for none): name, phone, email, time, address, order,
    order_email. None of the meaning model's answer is trusted when it is unavailable: then words decide ("detail")."""
    question = agent_question(agent_text)
    if not question:
        return ""
    from turn_meaning import AGENT_ASKED
    asked = AGENT_ASKED.best(question)
    if asked is None:
        return "detail" if _ASKED_FOR_DETAILS.search(question) else ""
    return asked if asked in (*DETAILS_WE_ASK_FOR, "order", "order_email") else ""


def asked_for_a_detail(agent_text: str) -> bool:
    """Whether our last reply asked the caller for one of their details (name, number, email, a time, an address)."""
    return bool(detail_asked(agent_text))


def contact_check(caller_text: str, agent_text: str = "", earlier: List[str] = (), region: str = "") -> tuple[str, bool]:
    """Check a phone number or email the caller just gave. Returns (note for the model, problem found).
    The note tells the model the exact details to read back, or that what was heard is not valid and must be asked
    for again, so neither model accepts a wrong number or email."""
    notes: List[str] = []
    problem = False
    digits = said_number(caller_text, min_digits=6)
    if digits and about_phone(caller_text, agent_text):
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


def is_place_question(caller_text: str) -> bool:
    """Whether the caller asks if something covers a place ("can you get it out to Geelong?"), by meaning."""
    from turn_meaning import PLACE_QUESTION
    # The place itself is blanked out ("Do you install in that town?"): the small model otherwise reacts to a city name
    # whether or not anything is asked about it ("I live in Brisbane and like your taps" asks nothing).
    masked = _PLACE_AFTER.sub(lambda match: match.group(0).replace(match.group(1), "that town"), caller_text)
    kind = PLACE_QUESTION.best(masked)
    if kind is None:  # the meaning model is unavailable: decide from words
        return bool(_PLACE_QUESTION.search(caller_text))
    return kind == "place"


def unknown_places(caller_text: str, knowledge_text: str) -> List[str]:
    """Places the caller named in a delivery/installation/store question that the knowledge never mentions
    ("Do you install in Brisbane?" with no Brisbane in the knowledge)."""
    if not is_place_question(caller_text):
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


def _named_area(caller_text: str, knowledge_text: str) -> List[str]:
    """A short answer naming an area ("Geelong.", "It's Ballarat."), when we just asked which area: the capitalised
    name, unless the knowledge mentions it."""
    if len(caller_text.split()) > 5:
        return []
    names = re.findall(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*\b", caller_text)
    skip = {"yes", "yeah", "yep", "no", "nope", "okay", "it's", "its", "it", "the", "in", "to", "sure", "well", "so", "um", "uh"}
    return [name for name in names if name.lower() not in skip and not re.search(rf"\b{re.escape(name)}\b", knowledge_text, re.I)]


def place_note(caller_text: str, knowledge_text: str, agent_text: str = "") -> str:
    """A note for this turn only, so the model does not say a place it knows nothing about is covered. Not while the
    caller is giving their details ("it's an India number, nine one ..."): that is about a phone number, not an area.
    When we asked for their area or address, the place they name is exactly what must not be confirmed, so that stays."""
    asked = detail_asked(agent_text)
    if asked not in ("", "address", "order") or said_number(caller_text, min_digits=4) or about_phone(caller_text):
        return ""
    places = unknown_places(caller_text, knowledge_text)
    if not places and asked == "address":
        places = _named_area(caller_text, knowledge_text)  # "Are you after delivery to a specific area?" "Geelong."
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
# Only LOOKUP_BRIDGES say "let me check", and only when a live store lookup really runs; all are said only if the
# answer is not ready yet.

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
# A question the live store answers (a price, stock, sizes, a warranty, an order): the store lookup really runs, so
# saying so is true, and it fills the ~2 s the lookup and the model take. Only for those turns (store_lookup_runs).
LOOKUP_BRIDGES = ("Let me check that for you.", "One sec, I'll have a look.", "Let me just look that up.", "Bear with me, I'll check.")
ALL_BRIDGES = (*SEARCH_BRIDGES, *PROBLEM_BRIDGES, *DETAIL_BRIDGES, *STATEMENT_BRIDGES, *REQUEST_BRIDGES, *CLARIFY_BRIDGES,
               *STATUS_PHRASES, *LOOKUP_BRIDGES)

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


def store_lookup_runs(caller_text: str, earlier: str = "") -> bool:
    """True when this turn clearly asks for live store data (products, an order, store policies), by meaning
    (store_intent, ~7 ms): the store lookup then runs, so a "let me check" is never untrue, and an off-topic
    question that only half-matches ("who won the football?") gets its answer straight away instead."""
    try:
        import store_intent
        from shopify_service import shopify_service
        if not shopify_service.configured:
            return False
        return store_intent.clearly_wanted(caller_text, " ".join(earlier.split()[-25:]))
    except Exception:
        return False


def bridge_kind(caller_text: str, earlier: str = "") -> str:
    """Which acknowledgement fits ("" = none), by what the caller means (turn_meaning.CALLER_TURN): none for a yes/no,
    a goodbye, a detail, a simple question the business knowledge answers, or anything upset or urgent (the answer
    itself must handle that). A question the live store answers gets a short "let me check" (`earlier`: what the
    caller said before, for follow-ups such as "how much is that one?")."""
    words = re.findall(r"[a-z']+", caller_text.lower())
    if len(words) < 4:
        return ""  # "Hello?", "Yes", "So what?": answer straight away
    from turn_meaning import CALLER_TURN
    turn_text = kind_of_turn_text(caller_text)
    kind = CALLER_TURN.best(turn_text)
    if kind is None:  # the meaning model is unavailable: decide from words
        return _keyword_bridge_kind(caller_text, earlier)
    if kind == "request" and not _clear_lead(CALLER_TURN.rank(turn_text)):
        # "What's your phone number?" scored request, detail and question within 0.01: "Yeah, I can sort that out."
        # before a plain answer sounded wrong. Only a clear request ("Can someone call me back?") gets one.
        kind = "question"
    if kind in ("clarify", "request", "problem"):
        return kind
    if kind in ("", "acknowledge", "closing", "detail", "sensitive"):
        return ""
    if len(words) >= 22 or len(re.findall(r"\b(?:and|also|plus)\b", caller_text.lower())) >= 2:
        return "detail"  # a long message with several things in it
    if kind in ("search", "question") and needs_facts(caller_text) and store_lookup_runs(caller_text, earlier):
        return "lookup"  # "How much is the Lydia mixer?" reads as a search, but it is a question the store answers
    if kind == "search":
        return "search"
    return "" if kind == "question" else "statement"


CLEAR_LEAD = 0.03  # how far the best kind of turn must lead the next one to be taken as clearly that kind


def _clear_lead(ranked) -> bool:
    return bool(ranked) and (len(ranked) < 2 or ranked[0][1] - ranked[1][1] >= CLEAR_LEAD)


def _keyword_bridge_kind(caller_text: str, earlier: str = "") -> str:
    """Fallback for bridge_kind when the local meaning model cannot load."""
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
        # A simple question the knowledge answers comes quickly enough; a live store lookup takes longer.
        return "lookup" if store_lookup_runs(caller_text, earlier) else ""
    return "statement"


_KIND_PHRASES = {"search": SEARCH_BRIDGES, "problem": PROBLEM_BRIDGES, "detail": DETAIL_BRIDGES, "statement": STATEMENT_BRIDGES,
                 "request": REQUEST_BRIDGES, "clarify": CLARIFY_BRIDGES, "status": STATUS_PHRASES, "lookup": LOOKUP_BRIDGES}


def bridge_phrase(caller_text: str, recent=(), earlier: str = "") -> str:
    """A short acknowledgement that fits what the caller said, or "" when none should be said. Never one of the
    recently used phrases (`recent`: a phrase or a list of the last few; `earlier`: what the caller said before)."""
    global _bridge_turn
    recent = {recent} if isinstance(recent, str) else set(recent or ())
    kind = "status" if caller_text == "__status__" else bridge_kind(caller_text, earlier)
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


def bridge_model_seconds() -> float:
    """VOICE_BRIDGE_MODEL_MS: how long the small model may take to write the "let me check" line (counted from the end
    of the caller's turn); slower than this, a fixed LOOKUP_BRIDGES phrase is said instead."""
    try:
        return max(0.1, float(os.getenv("VOICE_BRIDGE_MODEL_MS", "900"))) / 1000
    except ValueError:
        return 0.9


# The small model only phrases what to say while the store lookup runs; it is given no facts and must not answer.
LOOKUP_BRIDGE_PROMPT = (
    "You are the voice of a shop on a live phone call. The caller just asked something and you are looking it up right now. "
    "Write only one short, natural spoken sentence (4 to 9 words) to say while you look it up, for example \"Let me check "
    "the Lydia mixer for you.\" or \"One sec, I'll look up the warranty on that.\" Name what they asked about in a few of "
    "their words, or say 'that' when unsure. Never answer the question: no price, number, size, colour, yes or no, or any "
    "other fact. No greeting, no question, no quotes."
)
_LOOKUP_VERB = re.compile(r"\b(?:check\w*|look\w*|find\w*|see|pull\w*|moment|sec|second)\b", re.I)
_ANSWER_WORDS = re.compile(r"\d|\$|%|\b(?:yes|no|yeah|nope|costs?|priced|available|in stock|comes? in|warranty is|aud|dollars?)\b", re.I)


def lookup_bridge_messages(caller_text: str, agent_text: str = "") -> List[Dict[str, str]]:
    context = f"You last said: {agent_text}\n" if agent_text else ""
    return [{"role": "system", "content": LOOKUP_BRIDGE_PROMPT}, {"role": "user", "content": f"{context}Caller: {caller_text}"}]


def clean_lookup_bridge(text: str) -> str:
    """The small model's line if it is safe to say while looking up ("" otherwise): one short sentence that says it is
    checking and states nothing (a digit, a yes/no or a price would be a guess said before the real answer)."""
    line = re.split(r"(?<=[.!])\s", " ".join((text or "").replace('"', "").split()), maxsplit=1)[0].strip()
    words = line.split()
    if not 3 <= len(words) <= 12 or "?" in line or not _LOOKUP_VERB.search(line) or _ANSWER_WORDS.search(line):
        return ""
    return line if line[-1] in ".!" else f"{line}."


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
