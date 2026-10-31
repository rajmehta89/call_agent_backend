"""How a reply is written for the voice that says it (the transcript keeps the reply as written).

The Urdu voice (Deepgram flux-ambreen-ur) reads digits wrongly next to other words: a price "1,249" came out as
"one two two two four seven nine", the address "1537" as "543", and an English word repeated in brackets
("میٹ بلیک (matte black)") was said twice (2026-10-31, voice read back through speech recognition). Number words,
and an email spelled with Urdu "at" and "dot", were read correctly. This is formatting only: what the reply says is
never changed.
"""

import re

from voice_languages import normalize_language

_URDU_0_99 = (
    "صفر ایک دو تین چار پانچ چھ سات آٹھ نو دس گیارہ بارہ تیرہ چودہ پندرہ سولہ سترہ اٹھارہ انیس بیس "
    "اکیس بائیس تئیس چوبیس پچیس چھبیس ستائیس اٹھائیس انتیس تیس اکتیس بتیس تینتیس چونتیس پینتیس چھتیس "
    "سینتیس اڑتیس انتالیس چالیس اکتالیس بیالیس تینتالیس چوالیس پینتالیس چھیالیس سینتالیس اڑتالیس انچاس "
    "پچاس اکیاون باون ترپن چون پچپن چھپن ستاون اٹھاون انسٹھ ساٹھ اکسٹھ باسٹھ تریسٹھ چونسٹھ پینسٹھ چھیاسٹھ "
    "سڑسٹھ اڑسٹھ انہتر ستر اکہتر بہتر تہتر چوہتر پچہتر چھہتر ستتر اٹھہتر اناسی اسی اکیاسی بیاسی تراسی "
    "چوراسی پچاسی چھیاسی ستاسی اٹھاسی نواسی نوے اکانوے بانوے ترانوے چورانوے پچانوے چھیانوے ستانوے اٹھانوے ننانوے"
).split()
_URDU_SCALES = ((10 ** 7, "کروڑ"), (10 ** 5, "لاکھ"), (1000, "ہزار"), (100, "سو"))

# A phone number: 8+ digits in a run that may hold spaces, dashes and brackets ("(04) 2221 3831", "+61 422 213 831").
_PHONE = re.compile(r"\+?\(?\d[\d\s()\-]{6,}\d")
# Any other number: thousands commas and a decimal part allowed ("1,249", "149.95").
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_SIZE = re.compile(r"(?<=\d)\s*[x×]\s*(?=\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_WEB = re.compile(r"\b(?:www\.)?[a-z0-9-]+(?:\.[a-z]{2,})+\b", re.IGNORECASE)
# English in brackets right after the same words in Urdu script.
_LATIN_BRACKET = re.compile(r"\s*\(\s*[A-Za-z][A-Za-z\s\-'&/]*\)")
_LATIN_LETTERS = dict(zip("abcdefghijklmnopqrstuvwxyz", (
    "اے بی سی ڈی ای ایف جی ایچ آئی جے کے ایل ایم این او پی کیو آر ایس ٹی یو وی ڈبلیو ایکس وائی زیڈ").split()))
# Parts of an address short enough to be letters, not a word ("au" in ".com.au"); common endings stay words.
_SPOKEN_ENDINGS = {"com", "net", "org", "gov", "edu", "info", "shop", "store"}


def urdu_number(value: int) -> str:
    """1249 -> "ایک ہزار دو سو انچاس" (Pakistani/Indian scale: ہزار، لاکھ، کروڑ)."""
    if value < 100:
        return _URDU_0_99[value]
    for size, name in _URDU_SCALES:
        if value >= size:
            head, rest = divmod(value, size)
            words = f"{urdu_number(head)} {name}"
            return f"{words} {urdu_number(rest)}" if rest else words
    return _URDU_0_99[value]


def _urdu_digits(text: str) -> str:
    return " ".join(_URDU_0_99[int(digit)] for digit in text if digit.isdigit())


def _urdu_phone(match: re.Match) -> str:
    # Said digit by digit, in the groups it was written in (a pause at each space).
    groups = [group for group in re.split(r"[\s()\-]+", match.group(0).lstrip("+")) if group]
    spoken = "، ".join(_urdu_digits(group) for group in groups)
    return ("پلس " if match.group(0).startswith("+") else "") + spoken


def _urdu_amount(match: re.Match) -> str:
    whole, _, decimals = match.group(0).replace(",", "").partition(".")
    if len(whole) > 9:
        return _urdu_digits(whole)
    spoken = urdu_number(int(whole))
    return f"{spoken} اعشاریہ {_urdu_digits(decimals)}" if decimals else spoken


def _address_part(part: str) -> str:
    if len(part) <= 3 and part.lower() not in _SPOKEN_ENDINGS:
        return " ".join(_LATIN_LETTERS.get(letter, letter) for letter in part.lower())
    return part


def _urdu_address(text: str) -> str:
    """sales@homedfo.com.au -> "sales ایٹ homedfo ڈاٹ com ڈاٹ اے یو"."""
    name, at, domain = text.partition("@")
    def dotted(value: str) -> str:
        return " ڈاٹ ".join(_address_part(part) for part in value.split(".") if part)
    return f"{dotted(name)} ایٹ {dotted(domain)}" if at else dotted(name)


def for_voice(text: str, language: str = "en") -> str:
    """The text the voice should read for a reply in this language (unchanged where the voice reads it well)."""
    if normalize_language(language) != "ur" or not text:
        return text
    spoken = _LATIN_BRACKET.sub("", text)
    spoken = _EMAIL.sub(lambda match: _urdu_address(match.group(0)), spoken)
    spoken = _WEB.sub(lambda match: _urdu_address(match.group(0)), spoken)
    spoken = _SIZE.sub(" ضرب ", spoken)
    spoken = _PHONE.sub(lambda match: _urdu_phone(match) if sum(c.isdigit() for c in match.group(0)) >= 8
                        else match.group(0), spoken)
    spoken = _NUMBER.sub(_urdu_amount, spoken)
    return re.sub(r"\s{2,}", " ", spoken).strip()
