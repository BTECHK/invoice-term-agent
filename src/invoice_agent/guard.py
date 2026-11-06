"""Untrusted text handling: instruction-like text is flagged and quoted, never followed."""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
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
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
_LONG_NUMBER = re.compile(r"\bINV-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b|\b\d[\d -]{6,}\d\b")
# Letters that look like a Latin letter, as code point to the letter they imitate (Cyrillic, then Greek).
_LOOK_ALIKES = {
    0x0430: "a", 0x0435: "e", 0x043E: "o", 0x0440: "p", 0x0441: "c", 0x0443: "y", 0x0445: "x", 0x0456: "i",
    0x0458: "j", 0x0455: "s", 0x0501: "d", 0x04CF: "l", 0x051B: "q", 0x051D: "w",
    0x0410: "A", 0x0412: "B", 0x0415: "E", 0x041A: "K", 0x041C: "M", 0x041D: "H", 0x041E: "O", 0x0420: "P",
    0x0421: "C", 0x0422: "T", 0x0425: "X", 0x0406: "I", 0x0408: "J", 0x0405: "S",
    0x03BF: "o", 0x03B1: "a", 0x03BD: "v", 0x03B9: "i", 0x03C1: "p", 0x03C4: "t", 0x03C5: "u", 0x03BA: "k",
    0x0391: "A", 0x0392: "B", 0x0395: "E", 0x0396: "Z", 0x0397: "H", 0x0399: "I", 0x039A: "K", 0x039C: "M",
    0x039D: "N", 0x039F: "O", 0x03A1: "P", 0x03A4: "T", 0x03A5: "Y", 0x03A7: "X",
}


# Default-ignorable code points outside the Cf and Mn categories: Hangul fillers and unassigned slots.
_IGNORABLE = frozenset({0x115F, 0x1160, 0x3164, 0xFFA0, 0x2065, *range(0xFFF0, 0xFFF9), *range(0xE0000, 0xE1000)})
_NON_ASCII = re.compile(r"[^\x00-\x7f]")


class _Fold(dict):
    """A str.translate table: one character to one character, worked out the first time it is seen.

    The NFKC form counts only when it is a single character, so the folded text keeps the length of
    the text it is applied to. A zero width space reads as a space.
    """

    def __missing__(self, code_point: int) -> str:
        if code_point == 0x200B:
            self[code_point] = " "
            return " "
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
    """A character a reader does not see or that only decorates the letter before it."""
    return char != "\u200b" and (unicodedata.category(char) in ("Cf", "Mn") or ord(char) in _IGNORABLE)


@dataclass(frozen=True)
class _Readable:
    """The text as a reader sees it, with a way back to positions in the original text."""

    text: str
    removed_before: tuple[int, ...]

    def original(self, position: int) -> int:
        return position + bisect_right(self.removed_before, position)


def _readable(text: str) -> _Readable:
    """Remove soft hyphens, joiners, marks and other unseen characters, then fold look-alikes.

    Unseen characters leave the text, so a match position maps back to the original through the
    positions removed before it. Folding keeps the length.
    """
    if text.isascii():
        return _Readable(text, ())
    removed = [m.start() for m in _NON_ASCII.finditer(text) if _unseen(m.group())]
    if removed:
        cuts = [*removed, len(text)]
        text = "".join(text[after:before] for after, before in zip([0, *(i + 1 for i in removed)], cuts))
    return _Readable(fold_look_alikes(text), tuple(i - k for k, i in enumerate(removed)))


def _original_span(readable: _Readable, start: int, end: int) -> tuple[int, int]:
    return readable.original(start), readable.original(end - 1) + 1


def mask_account_numbers(text: str) -> str:
    """Keep at most the last four characters of an account or card number.

    Numbers are found in the readable form, so a zero width character or a no-break space inside one
    does not hide it. The replacement is cut from the original text.
    """
    readable = _readable(text)
    view = readable.text
    found = [(*_original_span(readable, m.start(), m.end()), "****" + re.sub(r"\s", "", m.group())[-4:])
             for m in _IBAN.finditer(view)]
    # Numbers inside an IBAN are already masked: blank it so the long number pass cannot match them again.
    rest = _IBAN.sub(lambda m: "*" * len(m.group()), view)
    found += [(*_original_span(readable, m.start(), m.end()), "****" + re.sub(r"\D", "", m.group())[-4:])
              for m in _LONG_NUMBER.finditer(rest) if not m.group().startswith("INV-")]
    for start, end, mask in sorted(found, reverse=True):
        text = text[:start] + mask + text[end:]
    return text


def contains_iban(text: str) -> bool:
    """True when the readable form of the text holds a full IBAN. Only the IBAN shape counts, never a plain digit run."""
    return _IBAN.search(_readable(text).text) is not None


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

    Patterns run on the readable form of the text; the quote is cut from the original at the mapped
    positions, so it shows what the sender wrote, invisible characters included.
    """
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()
    readable = _readable(text)
    for _, (flag_type, pattern) in _PATTERNS.items():
        for m in re.finditer(pattern, readable.text, re.I | re.S):
            quote = mask_account_numbers(_sentence_around(text, *_original_span(readable, m.start(), m.end())))[:240]
            key = (flag_type, quote)
            if key not in seen:
                seen.add(key)
                hits.append(Hit(flag_type, quote))
    return hits
