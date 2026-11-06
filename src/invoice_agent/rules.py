"""Pure rule functions: term parsing, date math, the 60 day rule, the discount rule, tolerances.

No clock, no randomness, no I/O. The model never calls these; the runner does.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

_DISCOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:/|%\s*)\s*(\d{1,3})\b")
_NET = re.compile(r"\bnet\s*(\d{1,3})", re.I)
_DAYS = re.compile(r"\b(\d{1,3})\s*days?\b", re.I)
_IMMEDIATE = re.compile(r"due on receipt|immediate|upon receipt", re.I)
# Legal forms by their normalized token. Spellings of one form share a value.
LEGAL_FORMS = {
    "gmbh": "gmbh", "ag": "ag", "kg": "kg", "se": "se", "ltd": "ltd", "limited": "ltd",
    "inc": "inc", "incorporated": "inc", "corp": "corp", "corporation": "corp", "co": "co", "company": "co",
    "llc": "llc", "lp": "lp", "llp": "llp", "plc": "plc", "sa": "sa", "sas": "sas", "srl": "srl", "spa": "spa",
    "bv": "bv", "nv": "nv", "ab": "ab", "oy": "oy",
}
# A dot or apostrophe inside a word is dropped ("L.t.d" is "ltd"); other punctuation ends the word.
_NAME_JOINERS = frozenset(".'\u2019")
_CURRENCIES = {"€": "EUR", "EUR": "EUR", "$": "USD", "USD": "USD", "£": "GBP", "GBP": "GBP"}


@dataclass(frozen=True)
class ParsedTerm:
    """A payment term. `discount_pct` is in percent: 2.0 means 2 percent."""

    net_days: int | None
    discount_pct: float | None = None
    discount_days: int | None = None


def parse_term(text: str | None) -> ParsedTerm | None:
    """Parse "Net 30", "30 days", "1/10 Net 45", "Net 45, 1/10". Return None when unsure."""
    if not text or not text.strip():
        return None
    pct = days = None
    rest = text
    disc = _DISCOUNT.search(text)
    if disc:
        pct, days = float(disc.group(1)), int(disc.group(2))
        rest = text[: disc.start()] + " " + text[disc.end():]
    found = _NET.search(rest) or _DAYS.search(rest)
    net = int(found.group(1)) if found else (0 if _IMMEDIATE.search(rest) else None)
    if net is None and pct is None:
        return None
    return ParsedTerm(net, pct, days)


def parse_printed_term(value: str | None, quote: str | None) -> ParsedTerm | None:
    """Net days come from the value. A discount may sit only in the quote (an e-invoice portal view keeps it on its own line)."""
    main = parse_term(value)
    if main is None or main.discount_pct is not None:
        return main
    extra = parse_term(quote)
    if extra and extra.discount_pct is not None:
        return ParsedTerm(main.net_days, extra.discount_pct, extra.discount_days)
    return main


def due_after(baseline: date, days: int) -> date:
    """Indicative date: the baseline plus term days. SAP computes the real due date."""
    return baseline + timedelta(days=days)


def due_after_ordinal(baseline: date, days: int) -> date:
    """Second route to the same date, used by the checker so the two share no code."""
    return date.fromordinal(baseline.toordinal() + days)


def annualized_discount(pct: float, discount_days: int, net_days: int, basis: int = 365) -> float | None:
    """d / (1 - d) * basis / (net - discount days). None when the terms make no sense.

    Worked example, 1/10 net 45: 0.01/0.99 x 365/35 = 10.5 percent.
    """
    rate = pct / 100
    span = net_days - discount_days
    if not 0 < rate < 1 or span <= 0:
        return None
    return rate / (1 - rate) * basis / span


def needs_agreement(days: int, confirmed_days: set[int] | frozenset[int], cap: int = 60) -> bool:
    """A term over the cap is an exception until a person confirms the express agreement."""
    return days > cap and days not in confirmed_days


def money(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def timing_value(amount: Decimal, rate: float, days: int, basis: int = 365) -> Decimal:
    """Cash timing value of `days` on `amount` at an annual rate."""
    return money(amount * Decimal(str(rate)) * Decimal(days) / Decimal(basis))


@dataclass(frozen=True)
class Gap:
    amount: Decimal
    pct: float
    limit: Decimal
    exceeds: bool


def total_gap(invoice_total: Decimal, po_total: Decimal, pct: float = 0.01) -> Gap:
    """Total variance: above `pct` of the PO value is outside tolerance."""
    amount = money(invoice_total - po_total)
    limit = money(abs(po_total) * Decimal(str(pct)))
    share = float(abs(amount) / abs(po_total)) if po_total else float("inf")
    return Gap(amount, share, limit, abs(amount) > limit)


def line_gap(invoice_line: Decimal, po_line: Decimal, pct: float = 0.02, absolute: float = 25.0) -> Gap:
    """Price variance per line: above the larger of `pct` of the PO line and the absolute amount."""
    amount = money(invoice_line - po_line)
    limit = max(money(abs(po_line) * Decimal(str(pct))), money(absolute))
    share = float(abs(amount) / abs(po_line)) if po_line else float("inf")
    return Gap(amount, share, limit, abs(amount) > limit)


def normalize_po_reference(text: str | None) -> str | None:
    """Digits only, zero padded to the 10 digit purchasing document number. None if it is not a reference."""
    digits = re.sub(r"\D", "", text or "")
    if not digits or len(digits) > 10 or len(digits) != len((text or "").strip()):
        return None
    return digits.zfill(10)


def parse_amount(text: str | None) -> tuple[Decimal | None, str | None]:
    """Parse "3,452.13", "EUR 4,573.80" or "2.614,07". Return (amount, currency or None)."""
    if not text:
        return None, None
    currency = None
    for token, code in _CURRENCIES.items():
        if token in text:
            currency = code
    digits = re.sub(r"[^\d.,]", "", text)
    if not digits:
        return None, currency
    last_dot, last_comma = digits.rfind("."), digits.rfind(",")
    if last_comma > last_dot:
        digits = digits.replace(".", "").replace(",", ".")
    else:
        digits = digits.replace(",", "")
    try:
        return Decimal(digits), currency
    except ArithmeticError:
        return None, currency


def name_tokens(text: str | None) -> list[str]:
    """Lower case word tokens of a company name, cleaned the same way for every name comparison.

    NFKC folds width and compatibility forms. Format (Cf) and nonspacing mark (Mn) characters are removed,
    so a joiner, soft hyphen or combining mark cannot hide a legal form; the zero width space is the one
    format character that ends a word. Punctuation ends a word too, except a dot or apostrophe inside it.
    A trailing "Gesellschaft mbH" is the long spelling of GmbH.
    """
    tokens: list[str] = []
    word: list[str] = []
    for char in unicodedata.normalize("NFKC", text or ""):
        if char != "\u200b" and (unicodedata.category(char) in ("Cf", "Mn") or char in _NAME_JOINERS):
            continue
        if char.isalnum():
            word.append(char.casefold())
        elif word:
            tokens.append("".join(word))
            word = []
    if word:
        tokens.append("".join(word))
    if tokens[-2:] == ["gesellschaft", "mbh"]:
        tokens[-2:] = ["gmbh"]
    return tokens


def vendor_key(text: str | None) -> str:
    """Lower case, no trailing legal form, no spaces or punctuation, for comparing a vendor name with a vendor key.

    Only trailing forms are dropped, which are the ones `legal_form` reads. A form anywhere else in the
    name stays in the key, so it can never be hidden from both comparisons at once.
    """
    tokens = name_tokens(text)
    while len(tokens) > 1 and tokens[-1] in LEGAL_FORMS:
        tokens.pop()
    return "".join(tokens)


def legal_form(text: str | None) -> str | None:
    """The normalized legal form a name ends with ("Inc." and "Incorporated" are both "inc"), else None.

    `vendor_key` ignores the form when comparing names. This is the other half: two names that both
    carry a form and carry different ones are not the same legal entity.
    """
    tokens = name_tokens(text)
    return LEGAL_FORMS.get(tokens[-1]) if len(tokens) > 1 else None


def company_code_agrees(invoice_code: str | None, header_code: str | None) -> bool:
    """Compare company codes by digits; a short entity label N also matches code N000."""
    inv = re.sub(r"\D", "", invoice_code or "")
    hdr = re.sub(r"\D", "", header_code or "")
    return bool(inv) and bool(hdr) and hdr in (inv, inv + "000")
