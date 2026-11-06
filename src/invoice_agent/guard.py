"""Untrusted text handling: instruction-like text is flagged and quoted, never followed."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Stands for an account number in the text the instruction patterns read, so that a long IBAN between "pay" and
# "immediately" does not use up a keyword window. A private-use character that never occurs in input.
_ACCOUNT = ""

# Keyword heuristics for common English phrasings. Each runs on text that has been normalized (see _scan_views):
# look-alike letters folded, hidden characters handled, whitespace collapsed, word-joining punctuation read as a
# space, digits inside words read as the letters they imitate, and account numbers replaced by _ACCOUNT.
_PATTERNS: dict[str, tuple[str, str]] = {
    "ignore_instructions": (
        "instruction_in_document",
        r"\b(?:ignor\w*|disregard\w*|forget)\b.{0,40}\b(?:previous|prior|above|earlier|your|all)\b.{0,30}"
        r"\b(?:rules?|instructions?|prompts?|polic(?:y|ies))\b"
        r"|\b(?:previous|prior|earlier|above|all)\b.{0,20}\b(?:rules?|instructions?|prompts?|polic(?:y|ies))\b.{0,30}"
        r"\b(?:ignor\w*|disregard\w*)"
        r"|\b(?:ignor\w*|disregard\w*|forget)\b.{0,30}\b(?:rules?|instructions?|prompts?|polic(?:y|ies))\b.{0,15}"
        r"\b(?:above|earlier|previous|prior)\b",
    ),
    "addressed_to_system": (
        "instruction_in_document",
        r"\b(?:system prompt|note to (?:the )?(?:ai|assistant|automated|system)|automated systems? ?:|you are now|system ?:)",
    ),
    "skip_controls": (
        "instruction_in_document",
        r"\b(?:do ?n[o']?t|don'?t|no need to|never)\b.{0,40}\b(?:flag\w*|rout\w*|escalat\w*|approv\w*|review\w*|verif\w*|check\w*)\b"
        r"|\b(?:bypass\w*|skip\w*|waiv\w*|overrid\w*)\b.{0,40}\b(?:approvals?|reviews?|checks?|controls?|rules?)\b",
    ),
    "urgent_payment": (
        "urgent_payment_request",
        r"\bpay\b.{0,50}\b(?:immediately|right away|today|urgently|now|without delay)\b"
        r"|\b(?:immediately|urgently|today|now)\b.{0,10}\bpay\b",
    ),
    "pay_to_account": (
        "bank_or_remit_to_change",
        r"\b(?:pay|remit|transfer)\b.{0,50}\b(?:to|into)\b.{0,40}(?:\baccount\b|\biban\b|" + _ACCOUNT + ")",
    ),
    "bank_change": (
        "bank_or_remit_to_change",
        r"\b(?:new|changed?|updated?|different|replacement|moved|relocated|transferred)\b.{0,35}"
        r"\b(?:bank\w*|account|iban|remit\w*|payee|recipient|treasury|settlement)\b"
        r"|\b(?:bank\w*|account|iban|remit\w*|payee|recipient|treasury|settlement)\b.{0,35}"
        r"\b(?:replacement|moved|relocated|transferred|chang\w*|updat\w*)\b"
        r"|\b(?:bank\w*|remit\w*|payee)\b.{0,30}\b(?:details?|account|address)\b.{0,25}\b(?:chang\w*|updat\w*)\b",
    ),
    "remit_to_change": (
        "bank_or_remit_to_change",
        r"\b(?:remit[- ]to|remittance|pay to the order of)\b.{0,40}\b(?:new|replacement|updated|different|other)\b",
    ),
}

# IBAN length by country code: the IBAN registry (ISO 13616) and the countries that use the format outside it.
# A string is an account only with one of these codes and exactly this many characters, so a hash or a
# reference that merely starts like one is left alone.
_IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AO": 25, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BF": 28, "BG": 22, "BH": 22,
    "BI": 27, "BJ": 28, "BR": 29, "BY": 28, "CF": 27, "CG": 27, "CH": 21, "CI": 28, "CM": 27, "CR": 22, "CV": 25,
    "CY": 28, "CZ": 24, "DE": 22, "DJ": 27, "DK": 18, "DO": 28, "DZ": 26, "EE": 20, "EG": 29, "ES": 24, "FI": 18,
    "FK": 18, "FO": 18, "FR": 27, "GA": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GQ": 27, "GR": 27, "GT": 28,
    "GW": 25, "HN": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IR": 26, "IS": 26, "IT": 27, "JO": 30,
    "KM": 27, "KW": 30, "KZ": 20, "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MA": 28,
    "MC": 27, "MD": 24, "ME": 22, "MG": 27, "MK": 19, "ML": 28, "MN": 20, "MR": 27, "MT": 31, "MU": 30, "MZ": 25,
    "NE": 28, "NI": 28, "NL": 18, "NO": 15, "OM": 23, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24,
    "RS": 22, "RU": 33, "SA": 24, "SC": 31, "SD": 18, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "SN": 28, "SO": 23,
    "ST": 25, "SV": 28, "TD": 27, "TG": 28, "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
    "YE": 30,
}
# Up to two separators may sit between any two characters of an account (a double space, a line wrap, a dotted or
# hyphenated grouping). Dashes and blank characters of other kinds are folded to "-" and " " before this is applied.
_SEPARATOR = r"[\s.\-_/·•]{0,2}"
# Where an account may start, in any letter case, and how far its characters may run. Starts are found with a
# lookahead so that one run cannot swallow an account that begins inside it. ASCII matching only: with IGNORECASE
# alone, [A-Z] would also match the Turkish dotted I and the Kelvin sign.
_IBAN_START = re.compile(rf"(?=([A-Z]){_SEPARATOR}([A-Z]){_SEPARATOR}\d{_SEPARATOR}\d)", re.I | re.A)
_IBAN_RUN = re.compile(rf"[A-Z]{_SEPARATOR}[A-Z]{_SEPARATOR}\d{_SEPARATOR}\d(?:{_SEPARATOR}[A-Z0-9]){{11,30}}", re.I | re.A)
# What may follow an account inside its token: nothing, or up to three letters such as a currency code.
_IBAN_TAIL = re.compile(r"[A-Z]{0,3}(?![A-Z0-9])", re.I | re.A)
_GROUP_SPLIT = re.compile(r"[\s.\-_/·•]+")
# Eight or more digits, with one or two separators between any two, glued to letters or not; an invoice id of the
# INV- form survives.
_LONG_NUMBER = re.compile(rf"\bINV-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b|(?<!\d)\d(?:{_SEPARATOR}\d){{7,}}(?!\d)", re.I)

# Letters that look like a Latin letter, as code point to the letter they imitate, when compatibility
# normalization leaves them alone: Cyrillic, Greek, Armenian, Cherokee, Lisu, Georgian, Latin letters with
# strokes, the small capitals of the phonetic blocks, and the enclosed letter and digit series.
_LOOK_ALIKES = {
    # Cyrillic
    0x0430: "a", 0x0435: "e", 0x043E: "o", 0x0440: "p", 0x0441: "c", 0x0443: "y", 0x0445: "x", 0x0456: "i",
    0x0458: "j", 0x0455: "s", 0x0501: "d", 0x04CF: "l", 0x051B: "q", 0x051D: "w", 0x043A: "k", 0x043D: "h",
    0x0442: "t", 0x043C: "m", 0x0475: "v", 0x0437: "3", 0x04BB: "h", 0x04AF: "y", 0x0433: "r",
    0x0410: "A", 0x0412: "B", 0x0415: "E", 0x041A: "K", 0x041C: "M", 0x041D: "H", 0x041E: "O", 0x0420: "P",
    0x0421: "C", 0x0422: "T", 0x0425: "X", 0x0406: "I", 0x0408: "J", 0x0405: "S", 0x0423: "Y", 0x051C: "W",
    0x051A: "Q", 0x0500: "D", 0x0474: "V", 0x0417: "3", 0x04AE: "Y",
    # Greek
    0x03BF: "o", 0x03B1: "a", 0x03BD: "v", 0x03B9: "i", 0x03C1: "p", 0x03C4: "t", 0x03C5: "u", 0x03BA: "k",
    0x03F2: "c", 0x03C9: "w", 0x03B3: "y", 0x03B5: "e", 0x03B7: "n", 0x03BC: "u", 0x03C7: "x",
    0x0391: "A", 0x0392: "B", 0x0395: "E", 0x0396: "Z", 0x0397: "H", 0x0399: "I", 0x039A: "K", 0x039C: "M",
    0x039D: "N", 0x039F: "O", 0x03A1: "P", 0x03A4: "T", 0x03A5: "Y", 0x03A7: "X", 0x03F9: "C",
    # Armenian
    0x0585: "o", 0x0578: "n", 0x0570: "h", 0x0575: "j", 0x057D: "u", 0x0561: "w", 0x0581: "g", 0x0563: "q",
    0x0584: "p", 0x054F: "S", 0x0555: "O", 0x053C: "L",
    # Cherokee
    0x13A0: "D", 0x13A1: "R", 0x13A2: "T", 0x13A5: "i", 0x13A9: "Y", 0x13AA: "A", 0x13AB: "J", 0x13AC: "E",
    0x13B3: "W", 0x13B7: "M", 0x13BB: "H", 0x13BD: "Y", 0x13C0: "G", 0x13C2: "h", 0x13C3: "Z", 0x13CF: "b",
    0x13D2: "R", 0x13D9: "V", 0x13DA: "S", 0x13DE: "L", 0x13DF: "C", 0x13E2: "P", 0x13E6: "K", 0x13F3: "G",
    0x13F4: "B",
    # Lisu
    0xA4D0: "B", 0xA4D1: "P", 0xA4D2: "d", 0xA4D3: "D", 0xA4D4: "T", 0xA4D6: "G", 0xA4D7: "K", 0xA4D9: "J",
    0xA4DA: "C", 0xA4DC: "Z", 0xA4DD: "F", 0xA4DF: "M", 0xA4E0: "N", 0xA4E1: "L", 0xA4E2: "S", 0xA4E3: "R",
    0xA4E6: "V", 0xA4E7: "H", 0xA4EA: "W", 0xA4EB: "X", 0xA4EC: "Y", 0xA4EE: "A", 0xA4F0: "E", 0xA4F2: "I",
    0xA4F3: "O", 0xA4F4: "U",
    # Georgian
    0x10DD: "o", 0x10E7: "y",
    # Latin letters with strokes, hooks or other shapes that normalization keeps
    0x0261: "g", 0x0251: "a", 0x00F8: "o", 0x00D8: "O", 0x0142: "l", 0x0141: "L", 0x0131: "i", 0x0130: "I",
    0x017F: "s", 0x0111: "d", 0x0110: "D", 0x0127: "h", 0x0167: "t", 0x0180: "b", 0x01C0: "l", 0x0237: "j",
    0x0138: "k", 0x00DF: "ss",
    # small capitals
    0x1D00: "a", 0x0299: "b", 0x1D04: "c", 0x1D05: "d", 0x1D07: "e", 0xA730: "f", 0x0262: "g", 0x029C: "h",
    0x026A: "i", 0x1D0A: "j", 0x1D0B: "k", 0x029F: "l", 0x1D0D: "m", 0x0274: "n", 0x1D0F: "o", 0x1D18: "p",
    0xA7AF: "q", 0x0280: "r", 0xA731: "s", 0x1D1B: "t", 0x1D1C: "u", 0x1D20: "v", 0x1D21: "w", 0x028F: "y",
    0x1D22: "z",
}
# Enclosed letters and digits without a compatibility mapping: negative circled and squared capitals, regional
# indicator symbols, the dingbat and double circled digit series.
_LOOK_ALIKES.update({0x1F150 + i: chr(65 + i) for i in range(26)})
_LOOK_ALIKES.update({0x1F170 + i: chr(65 + i) for i in range(26)})
_LOOK_ALIKES.update({0x1F1E6 + i: chr(65 + i) for i in range(26)})
for _base in (0x2776, 0x2780, 0x278A, 0x24F5):
    _LOOK_ALIKES.update({_base + i: str(i + 1) for i in range(9)})
_LOOK_ALIKES[0x24FF] = "0"
# Blanks that normalization leaves alone, read as a space; dashes of every kind, read as a hyphen; apostrophes.
_SPACE_LIKE = frozenset({0x2800, 0x1680, 0x2028, 0x2029, 0x3000, 0x205F})
_DASHES = frozenset({0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212, 0xFE58, 0xFE63, 0xFF0D})
_APOSTROPHES = frozenset({0x2018, 0x2019, 0x02BC, 0x2032, 0x201B})
# Default-ignorable code points outside the Cf and Mn categories (Hangul fillers and unassigned slots), and the
# thin, hair and narrow spaces, which a reader does not see as a gap.
_IGNORABLE = frozenset({0x115F, 0x1160, 0x3164, 0xFFA0, 0x2065, 0x2009, 0x200A, 0x202F,
                        *range(0xFFF0, 0xFFF9), *range(0xE0000, 0xE1000)})
# Anything that is not printable ASCII or ASCII whitespace needs a look: a control character or a non-ASCII one.
_NEEDS_A_LOOK = re.compile(r"[^\x09-\x0d\x20-\x7e]")


class _Fold(dict):
    """A str.translate table: one character to one character, worked out the first time it is seen.

    The NFKC form counts only when it is a single character, so the folded text keeps the length of
    the text it is applied to.
    """

    def __missing__(self, code_point: int) -> str:
        char = chr(code_point)
        normal = unicodedata.normalize("NFKC", char)
        char = normal if len(normal) == 1 else char
        folded = _LOOK_ALIKES.get(ord(char), char)
        self[code_point] = folded = folded if len(folded) == 1 else char
        return folded


_FOLD = _Fold()


@dataclass(frozen=True)
class Hit:
    type: str
    quote: str


def _unseen(char: str) -> bool:
    """A character a reader does not see, or that only decorates the letter it sits on."""
    category = unicodedata.category(char)
    return (category in ("Cf", "Mn", "Me", "Mc", "Sk") or ord(char) in _IGNORABLE
            or (category == "Cc" and char not in "\t\n\x0b\x0c\r"))


def _pieces(char: str) -> str:
    """What a reader sees for one character: one or more characters, or none.

    Blanks become a space and dashes a hyphen. A look-alike becomes the letter it imitates. Anything else is
    decomposed (NFKD), so an accented letter gives its base letter and a ligature or an enclosed digit gives
    its letters or digit; the marks of the decomposition are dropped.
    """
    code_point = ord(char)
    if code_point in _SPACE_LIKE:
        return " "
    if code_point in _DASHES:
        return "-"
    if code_point in _APOSTROPHES:
        return "'"
    if code_point in _LOOK_ALIKES:
        return _LOOK_ALIKES[code_point]
    normal = unicodedata.normalize("NFKD", char)
    if len(normal) > 1:
        normal = normal.replace("(", "").replace(")", "")
    return "".join(_LOOK_ALIKES.get(ord(piece), piece) for piece in normal
                   if unicodedata.category(piece) not in ("Mn", "Me", "Mc"))


@dataclass(frozen=True)
class _Readable:
    """The text as a reader sees it, with the original position of each character."""

    text: str
    positions: tuple[int, ...] | None  # None: the text is unchanged
    # Set when unseen characters were removed: a word boundary may then be an artefact of the removal.
    loose: bool = False

    def original(self, position: int) -> int:
        return position if self.positions is None else self.positions[position]


def _views(text: str) -> tuple[_Readable, ...]:
    """The text as a reader sees it: unseen characters removed, and again with each read as a space.

    A zero width space inside a word hides the word unless it is removed; one between two words hides
    the gap unless it is read as a space. Patterns run on both forms. A character may expand to several
    or to none, so each form keeps the original position of every character.
    """
    if not _NEEDS_A_LOOK.search(text):
        return (_Readable(text, None),)
    stripped: list[tuple[str, int]] = []
    spaced: list[tuple[str, int]] = []
    removed = False
    for index, char in enumerate(text):
        if char.isascii() and (char >= " " or char in "\t\n\x0b\x0c\r") and char != "\x7f":
            stripped.append((char, index))
            spaced.append((char, index))
            continue
        if _unseen(char):
            removed = True
            spaced.append((" ", index))
            continue
        for piece in _pieces(char):
            stripped.append((piece, index))
            spaced.append((piece, index))
    views = [_Readable("".join(c for c, _ in stripped), tuple(i for _, i in stripped), loose=removed)]
    if removed:
        views.append(_Readable("".join(c for c, _ in spaced), tuple(i for _, i in spaced)))
    if "\u202e" in text:
        # A right-to-left override shows the stored order reversed, so a reader sees the reversed text.
        views.extend(_Readable(v.text[::-1], tuple(reversed(v.positions)), v.loose) for v in list(views))
    return tuple(views)


def _original_span(readable: _Readable, start: int, end: int) -> tuple[int, int]:
    first, last = sorted((readable.original(start), readable.original(end - 1)))
    return first, last + 1


def _check_digits_ok(compact: str) -> bool:
    """The IBAN check (ISO 7064 mod 97-10) on an account without separators, upper case ASCII."""
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def _ibans(view: str) -> list[tuple[int, int]]:
    """Spans of accounts in a readable form.

    A span needs a registered country code and that country's exact length, and its token must end there or
    after at most three more letters (a currency code). One that stands alone, printed as one block or in the
    usual groups of four, counts as it is, placeholder check digits included. One glued to a label in front
    (IBANGB82...), to letters behind (...32EUR) or to a longer dotted or hyphenated token (the tail of a UUID),
    or one printed in any other grouping, must also pass the check digits, so a hash, a reference number or a
    run of short words that happens to have the shape of an account is left alone.
    """
    spans: list[tuple[int, int]] = []
    for start in _IBAN_START.finditer(view):
        length = _IBAN_LENGTHS.get((start.group(1) + start.group(2)).upper())
        run = _IBAN_RUN.match(view, start.start()) if length else None
        if run is None:
            continue
        compact: list[str] = []
        ends: list[int] = []
        for offset, char in enumerate(run.group()):
            if char.isascii() and char.isalnum():
                compact.append(char.upper())
                ends.append(offset + 1)
        if len(compact) < length:
            continue
        end = run.start() + ends[length - 1]
        tail = _IBAN_TAIL.match(view, end)
        if tail is None:
            continue
        before, after = view[:run.start()], view[tail.end():]
        glued_left = bool(before) and (before[-1].isalnum() or (before[-1] in ".-" and before[-2:-1].isalnum()))
        glued_right = tail.end() > end or (after[:1] in (".", "-") and after[1:2].isalnum())
        if glued_left and glued_right:
            continue
        groups = _GROUP_SPLIT.split(view[run.start():end])
        conventional = len(groups) == 1 or (all(len(g) == 4 for g in groups[:-1]) and 1 <= len(groups[-1]) <= 4)
        if (glued_left or glued_right or not conventional) and not _check_digits_ok("".join(compact[:length])):
            continue
        spans.append((run.start(), end))
    return spans


def _account_spans(view: str) -> list[tuple[int, int, str]]:
    """Every account span in a readable form with its mask: IBANs first, then runs of digits outside them."""
    found: list[tuple[int, int, str]] = []
    blanked = view
    for start, end in _ibans(view):
        found.append((start, end, "****" + re.sub(r"[^0-9A-Za-z]", "", view[start:end])[-4:]))
        blanked = blanked[:start] + "*" * (end - start) + blanked[end:]
    found.extend((m.start(), m.end(), "****" + re.sub(r"\D", "", m.group())[-4:])
                 for m in _LONG_NUMBER.finditer(blanked) if not m.group().upper().startswith("INV-"))
    return found


def mask_account_numbers(text: str) -> str:
    """Keep at most the last four characters of an account or card number.

    Numbers are found in the readable forms, so a zero width character or a no-break space inside one
    does not hide it, and an IBAN is found in any letter case, with one or two separators between any two
    characters, and even glued to a label. The replacement covers the span of the original text.
    """
    found: set[tuple[int, int, str]] = set()
    for readable in _views(text):
        found.update((*_original_span(readable, start, end), mask) for start, end, mask in _account_spans(readable.text))
    # The forms usually find the same spans; where spans overlap, the earlier and longer one wins.
    chosen: list[tuple[int, int, str]] = []
    for span in sorted(found, key=lambda s: (s[0], s[0] - s[1])):
        if not chosen or span[0] >= chosen[-1][1]:
            chosen.append(span)
    for start, end, mask in reversed(chosen):
        text = text[:start] + mask + text[end:]
    return text


def contains_iban(text: str) -> bool:
    """True when a readable form of the text holds a full IBAN (registered country, exact length). Never a plain digit run."""
    return any(_ibans(readable.text) for readable in _views(text))


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(". ", 0, start), text.rfind("\n", 0, start))
    right_candidates = [i for i in (text.find(". ", end), text.find("\n", end)) if i != -1]
    right = min(right_candidates) if right_candidates else len(text)
    return " ".join(text[left + 1 if left != -1 else 0: right + 1].split())


def fold_look_alikes(text: str) -> str:
    """Compatibility forms (full width, superscript) and Cyrillic or Greek look-alikes as Latin letters. Same length."""
    return text if text.isascii() else text.translate(_FOLD)


# Rewrites applied to a readable form before the instruction patterns run, in this order.
_LEET = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "@": "a", "$": "s", "|": "l"}
_REWRITES: tuple[tuple[re.Pattern[str], object], ...] = (
    (re.compile(r"(?<=[A-Za-z])-[ \t]*\n[ \t]*(?=[A-Za-z])"), ""),          # a word hyphenated across a line wrap
    (re.compile(r"\*{1,2}|__|</?[a-z]{1,6}>"), ""),                           # emphasis marks and tags
    (re.compile(r"(?<=[A-Za-z])[-_·.](?=[A-Za-z])"), " "),                    # do-not-flag, ignore_previous_rules
    (re.compile(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b"), lambda m: m.group().replace(" ", "")),  # l e t t e r spacing
    (re.compile(r"\s+"), " "),                                                 # any gap is one space
    (re.compile(r"(?<=[A-Za-z])[01345678@$|](?=[A-Za-z])"), lambda m: _LEET[m.group()]),  # ign0re, f|ag
)


def _rewrite(pairs: list[tuple[str, int]], pattern: re.Pattern[str], replacement: object) -> list[tuple[str, int]]:
    """Apply one rewrite to (character, original position) pairs; replacement characters map to the match start."""
    text = "".join(c for c, _ in pairs)
    out: list[tuple[str, int]] = []
    last = 0
    for m in pattern.finditer(text):
        out.extend(pairs[last:m.start()])
        new = replacement(m) if callable(replacement) else replacement
        out.extend((c, pairs[m.start()][1]) for c in new)
        last = m.end()
    out.extend(pairs[last:])
    return out


def _scan_views(text: str) -> list[_Readable]:
    """The forms the instruction patterns read: each readable form, rewritten, with accounts as one character."""
    views: list[_Readable] = []
    for readable in _views(text):
        pairs = [(c, readable.original(i)) for i, c in enumerate(readable.text)]
        for pattern, replacement in _REWRITES:
            pairs = _rewrite(pairs, pattern, replacement)
        plain = "".join(c for c, _ in pairs)
        for start, end, _ in sorted(_account_spans(plain), reverse=True):
            pairs[start:end] = [(_ACCOUNT, pairs[start][1])]
        views.append(_Readable("".join(c for c, _ in pairs), tuple(i for _, i in pairs), readable.loose))
    return views


def scan_text(text: str) -> list[Hit]:
    """Find instruction-like spans. Each hit quotes the sentence, with account numbers masked.

    Patterns run on the rewritten readable forms of the text; the quote is cut from the original at the
    mapped positions, so it shows what the sender wrote, invisible characters included. Text that carried
    invisible characters is matched without word boundaries and with the spaces inside a phrase optional,
    since removing a zero width space can glue two words together just as reading one as a space can split
    a word.
    """
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()
    for readable in _scan_views(text):
        for _, (flag_type, pattern) in _PATTERNS.items():
            expression = pattern.replace(r"\b", "").replace(" ", " ?") if readable.loose else pattern
            for m in re.finditer(expression, readable.text, re.I | re.S):
                quote = mask_account_numbers(_sentence_around(text, *_original_span(readable, m.start(), m.end())))[:240]
                key = (flag_type, quote)
                if key not in seen:
                    seen.add(key)
                    hits.append(Hit(flag_type, quote))
    return hits
