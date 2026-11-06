"""Untrusted text handling: instruction-like text is flagged and quoted, never followed."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_PATTERNS: dict[str, tuple[str, str]] = {
    "ignore_instructions": (
        "instruction_in_document",
        r"\b(?:ignore|disregard|forget)\b.{0,30}\b(?:previous|prior|above|earlier|your)\b.{0,20}"
        r"\b(?:rules?|instructions?|prompts?|polic(?:y|ies))\b",
    ),
    "addressed_to_system": (
        "instruction_in_document",
        r"\b(?:system prompt|note to (?:the )?(?:ai|assistant|automated|system)|automated systems?:|you are now|system:)",
    ),
    "skip_controls": (
        "instruction_in_document",
        r"\b(?:do not|don't|no need to|never)\b.{0,25}\b(?:flag|route|escalate|approve|review|verify|check)\b"
        r"|\b(?:bypass|skip|waive|override)\b.{0,25}\b(?:approval|review|checks?|controls?|rules?)\b",
    ),
    "urgent_payment": (
        "urgent_payment_request",
        r"\bpay\b.{0,30}\b(?:immediately|right away|today|urgently|now|without delay)\b",
    ),
    "pay_to_account": (
        "bank_or_remit_to_change",
        r"\b(?:pay|remit|transfer)\b.{0,40}\b(?:to|into)\b.{0,20}\b(?:account|iban)\b",
    ),
    "bank_change": (
        "bank_or_remit_to_change",
        r"\b(?:new|changed?|updated?|different|replacement|moved|relocated|transferred)\b.{0,25}"
        r"\b(?:bank|account|iban|remit\w*|payee|recipient|treasury|settlement)\b"
        r"|\b(?:bank|account|iban|remit\w*|payee|recipient|treasury|settlement)\b.{0,25}"
        r"\b(?:replacement|moved|relocated|transferred)\b"
        r"|\b(?:bank|remit\w*|payee)\b.{0,30}\b(?:details?|account|address)\b.{0,25}\b(?:chang\w*|updat\w*)\b",
    ),
    "remit_to_change": (
        "bank_or_remit_to_change",
        r"\b(?:remit[- ]to|remittance|pay to the order of)\b.{0,40}\b(?:new|replacement|updated|different|other)\b",
    ),
}
# IBAN length by country code, from the IBAN registry (ISO 13616). A string is an account only with one of
# these codes and exactly this many characters, so a hash or a reference that merely starts like one is left alone.
_IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BI": 27, "BR": 29,
    "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DJ": 27, "DK": 18, "DO": 28, "EE": 20, "EG": 29,
    "ES": 24, "FI": 18, "FK": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28,
    "HN": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24, "ME": 22, "MK": 19,
    "MN": 20, "MR": 27, "MT": 31, "MU": 30, "NI": 28, "NL": 18, "NO": 15, "OM": 23, "PK": 24, "PL": 28, "PS": 29,
    "PT": 25, "QA": 29, "RO": 24, "RS": 22, "RU": 33, "SA": 24, "SC": 31, "SD": 18, "SE": 24, "SI": 19, "SK": 24,
    "SM": 27, "SO": 23, "ST": 25, "SV": 28, "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
    "YE": 30,
}
# Where an account may start, in any letter case, and how far its characters may run with one common separator
# between any two of them, the first four included. Starts are found with a lookahead so that one run cannot
# swallow an account that begins inside it.
_SEPARATOR = r"[ \t\n\r.-]"
_IBAN_START = re.compile(rf"(?=([A-Z]){_SEPARATOR}?([A-Z]){_SEPARATOR}?\d{_SEPARATOR}?\d)", re.I)
_IBAN_RUN = re.compile(rf"[A-Z]{_SEPARATOR}?[A-Z]{_SEPARATOR}?\d{_SEPARATOR}?\d(?:{_SEPARATOR}?[A-Z0-9]){{11,30}}", re.I)
# What may follow an account inside its token: nothing, or up to three letters such as a currency code.
_IBAN_TAIL = re.compile(r"[A-Z]{0,3}(?![A-Z0-9])", re.I)
# Eight or more digits, with at most one space or hyphen between any two; an invoice id of the INV- form survives.
_LONG_NUMBER = re.compile(r"\bINV-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b|\b\d(?:[ -]?\d){7,}\b")
# Letters that look like a Latin letter, as code point to the letter they imitate: Cyrillic, Greek, then the
# small capitals of the phonetic blocks, which compatibility normalization leaves alone.
_LOOK_ALIKES = {
    0x0430: "a", 0x0435: "e", 0x043E: "o", 0x0440: "p", 0x0441: "c", 0x0443: "y", 0x0445: "x", 0x0456: "i",
    0x0458: "j", 0x0455: "s", 0x0501: "d", 0x04CF: "l", 0x051B: "q", 0x051D: "w",
    0x0410: "A", 0x0412: "B", 0x0415: "E", 0x041A: "K", 0x041C: "M", 0x041D: "H", 0x041E: "O", 0x0420: "P",
    0x0421: "C", 0x0422: "T", 0x0425: "X", 0x0406: "I", 0x0408: "J", 0x0405: "S",
    0x03BF: "o", 0x03B1: "a", 0x03BD: "v", 0x03B9: "i", 0x03C1: "p", 0x03C4: "t", 0x03C5: "u", 0x03BA: "k",
    0x0391: "A", 0x0392: "B", 0x0395: "E", 0x0396: "Z", 0x0397: "H", 0x0399: "I", 0x039A: "K", 0x039C: "M",
    0x039D: "N", 0x039F: "O", 0x03A1: "P", 0x03A4: "T", 0x03A5: "Y", 0x03A7: "X",
    0x1D00: "a", 0x0299: "b", 0x1D04: "c", 0x1D05: "d", 0x1D07: "e", 0xA730: "f", 0x0262: "g", 0x029C: "h",
    0x026A: "i", 0x1D0A: "j", 0x1D0B: "k", 0x029F: "l", 0x1D0D: "m", 0x0274: "n", 0x1D0F: "o", 0x1D18: "p",
    0xA7AF: "q", 0x0280: "r", 0xA731: "s", 0x1D1B: "t", 0x1D1C: "u", 0x1D20: "v", 0x1D21: "w", 0x028F: "y",
    0x1D22: "z",
}


# Default-ignorable code points outside the Cf and Mn categories: Hangul fillers and unassigned slots.
_IGNORABLE = frozenset({0x115F, 0x1160, 0x3164, 0xFFA0, 0x2065, *range(0xFFF0, 0xFFF9), *range(0xE0000, 0xE1000)})
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
        self[code_point] = folded = _LOOK_ALIKES.get(ord(char), char)
        return folded


_FOLD = _Fold()


@dataclass(frozen=True)
class Hit:
    type: str
    quote: str


def _unseen(char: str) -> bool:
    """A character a reader does not see, or that only decorates the letter it sits on."""
    category = unicodedata.category(char)
    return (category in ("Cf", "Mn", "Me") or ord(char) in _IGNORABLE
            or (category == "Cc" and char not in "\t\n\x0b\x0c\r"))


def _pieces(char: str) -> str:
    """What a reader sees for one character: its compatibility form with look-alikes folded, one or more characters."""
    normal = unicodedata.normalize("NFKC", char)
    return normal.translate(_FOLD) if len(normal) > 1 else _FOLD[ord(char)]


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
    the gap unless it is read as a space. Patterns run on both forms. A ligature or another compatibility
    character may expand to several characters, so each form keeps the original position of every character.
    """
    if not _NEEDS_A_LOOK.search(text):
        return (_Readable(text, None),)
    stripped: list[tuple[str, int]] = []
    spaced: list[tuple[str, int]] = []
    removed = False
    for index, char in enumerate(text):
        if _unseen(char):
            removed = True
            spaced.append((" ", index))
            continue
        for piece in (char if char.isascii() else _pieces(char)):
            stripped.append((piece, index))
            spaced.append((piece, index))
    views = [_Readable("".join(c for c, _ in stripped), tuple(i for _, i in stripped), loose=removed)]
    if removed:
        views.append(_Readable("".join(c for c, _ in spaced), tuple(i for _, i in spaced)))
    return tuple(views)


def _original_span(readable: _Readable, start: int, end: int) -> tuple[int, int]:
    return readable.original(start), readable.original(end - 1) + 1


def _check_digits_ok(compact: str) -> bool:
    """The IBAN check (ISO 7064 mod 97-10) on an account without separators, upper case."""
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def _ibans(view: str) -> list[tuple[int, int]]:
    """Spans of accounts in a readable form.

    A span needs a registered country code and that country's exact length, and its token must end there or
    after at most three more letters (a currency code). One that stands alone counts as it is, placeholder
    check digits included. One glued to a label in front (IBANGB82...), to letters behind (...32EUR) or to a
    longer dotted or hyphenated token (the tail of a UUID) must also pass the check digits, so a hash or a
    reference number that happens to start like an account is left alone.
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
            if char.isalnum():
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
        if (glued_left or glued_right) and not _check_digits_ok("".join(compact[:length])):
            continue
        spans.append((run.start(), end))
    return spans


def mask_account_numbers(text: str) -> str:
    """Keep at most the last four characters of an account or card number.

    Numbers are found in the readable forms, so a zero width character or a no-break space inside one
    does not hide it, and an IBAN is found in any letter case, with any grouping and even glued to the end
    of a label. The replacement covers the span of the original text.
    """
    found: set[tuple[int, int, str]] = set()
    for readable in _views(text):
        view = readable.text
        blanked = view
        for start, end in _ibans(view):
            found.add((*_original_span(readable, start, end), "****" + re.sub(r"[^0-9A-Za-z]", "", view[start:end])[-4:]))
            # Digits inside an IBAN are already masked: blank it so the long number pass cannot match them again.
            blanked = blanked[:start] + "*" * (end - start) + blanked[end:]
        found.update((*_original_span(readable, m.start(), m.end()), "****" + re.sub(r"\D", "", m.group())[-4:])
                     for m in _LONG_NUMBER.finditer(blanked) if not m.group().startswith("INV-"))
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


def scan_text(text: str) -> list[Hit]:
    """Find instruction-like spans. Each hit quotes the sentence, with account numbers masked.

    Patterns run on the readable forms of the text; the quote is cut from the original at the mapped
    positions, so it shows what the sender wrote, invisible characters included. Text that carried
    invisible characters is matched without word boundaries, since removing a zero width space can
    glue two words together just as reading one as a space can split a word.
    """
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()
    for readable in _views(text):
        for _, (flag_type, pattern) in _PATTERNS.items():
            expression = pattern.replace(r"\b", "") if readable.loose else pattern
            for m in re.finditer(expression, readable.text, re.I | re.S):
                quote = mask_account_numbers(_sentence_around(text, *_original_span(readable, m.start(), m.end())))[:240]
                key = (flag_type, quote)
                if key not in seen:
                    seen.add(key)
                    hits.append(Hit(flag_type, quote))
    return hits
