"""Local invoice reader: the PDF text layer when a page has one, OCR on the rendered page or image otherwise.

OCR is RapidOCR with the PP-OCRv4 detection and recognition models that ship inside the rapidocr-onnxruntime
wheel, so nothing is downloaded or called at run time. The reader returns the seven fields with the line or
lines of the page each value was read from, the recognizer's score for those lines, every line it read (the
reading) and the guard's flags over that reading.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .guard import scan_text
from .models import Extraction, FieldValue, Flag, FIELD_NAMES
from .rules import LEGAL_FORMS

READER_VERSION = "1.0.0"
OCR_MODEL = "PP-OCRv4 mobile detection and recognition ONNX models from rapidocr-onnxruntime 1.4.4"
TEXT_LAYER_MODEL = "PyMuPDF text layer"
MODE_TEXT_LAYER = "pdf_text_layer"
MODE_OCR = "local_ocr"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"})
# A page with fewer characters than this in its text layer is read by OCR (a scanned PDF carries none).
MIN_TEXT_LAYER_CHARS = 50
# A PDF page without a text layer is rendered at this resolution before OCR.
RENDER_DPI = 200
# Each page is recognized at these long sides (None is the page as supplied), because PP-OCR drops the space
# between two words at some sizes and keeps it at others. Per line the reading with more spaces is kept, then
# the higher score; the recognizer is not known to invent a space.
PASS_LONG_SIDES: tuple[int | None, ...] = (None, 3500)
MAX_PAGES = 10


class ReadError(RuntimeError):
    """The file cannot be opened as a PDF or an image."""


@dataclass(frozen=True)
class Line:
    """One line of text on a page with its box and the recognizer's score (1.0 for a text layer)."""

    page: int
    text: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(frozen=True)
class Reading:
    mode: str
    lines: tuple[Line, ...]
    page_heights: dict[int, float]


# ---- text normalization for recognizer output --------------------------------------------------------------

# The recognizer drops the space between two words more often than it adds one. These boundaries are safe to
# re-open: a lower-case letter or a digit followed by a capitalized word, a lower-case letter followed by a
# digit, a digit run against a run of three or more capitals, an ampersand between letters, a comma or a
# sentence period glued to the next word.
_RESPACE = (
    re.compile(r"(?<=[a-z])(?=[A-Z][a-z])"),
    re.compile(r"(?<=\d)(?=[A-Z][a-z])"),
    re.compile(r"(?<=[a-z])(?=\d)"),
    re.compile(r"(?<=\d)(?=[A-Z]{3,}(?![a-z]))"),
    re.compile(r"(?<=[A-Z]{3})(?=\d)"),
    re.compile(r"(?<=,)(?=[A-Za-z])"),
    re.compile(r"(?<=[a-z]\.)(?=[A-Z])"),
    re.compile(r"(?<=[A-Za-z]:)(?=[A-Za-z])"),
)
# A banking or tax keyword glued to the code that follows it ("IBANBE00", "VATDE0").
_KEYWORD_GLUE = re.compile(
    r"(?<=\bIBAN)(?=[A-Z0-9])|(?<=\bSWIFT)(?=[A-Z0-9])|(?<=\bBIC)(?=[A-Z0-9])|(?<=\bVAT)(?=[A-Z0-9])"
    r"|(?<=\bACH)(?=[A-Z0-9])|(?<=\bEIN)(?=[A-Z0-9])"
)
_AMPERSAND = re.compile(r"(?<=[A-Za-z])\s*&\s*(?=[A-Za-z])")
# The recognizer reads the digit 0 as the letter o. In an IBAN the two characters after the country code are
# check digits and a group made only of o and digits is numeric; a group with other letters (a bank code) is
# left alone.
_IBAN_GROUPS = re.compile(r"(?i)(\bIBAN\b\s*:?\s*)([A-Z]{2}[0-9oO]{2}(?:[ \-]?[A-Za-z0-9]{1,4})+)")
# Printed legal forms whose letter case the recognizer gets wrong ("B.v."). Only a token of mixed case is
# re-cased, so a supplier that prints LTD or ltd keeps its spelling.
_FORM_CASE = {
    "bv": "BV", "nv": "NV", "gmbh": "GmbH", "llc": "LLC", "llp": "LLP", "lp": "LP", "plc": "plc", "ag": "AG",
    "kg": "KG", "se": "SE", "sa": "SA", "sas": "SAS", "ltd": "Ltd", "inc": "Inc", "corp": "Corp", "co": "Co",
    "srl": "Srl", "spa": "SpA", "ab": "AB", "oy": "Oy",
}


def _fix_iban_zeros(text: str) -> str:
    def fix(match: re.Match[str]) -> str:
        head, body = match.group(1), match.group(2)
        groups = re.split(r"([ \-])", body)
        out = []
        for i, group in enumerate(groups):
            if i == 0:
                group = group[:2] + group[2:4].replace("o", "0").replace("O", "0") + group[4:]
            if re.fullmatch(r"[0-9oO]+", group) and (i > 0 or len(group) > 4):
                group = group.replace("o", "0").replace("O", "0")
            elif i == 0 and re.fullmatch(r"[A-Z]{2}\d{2}[0-9oO]*", group):
                group = group[:4] + group[4:].replace("o", "0").replace("O", "0")
            out.append(group)
        return head + "".join(out)

    return _IBAN_GROUPS.sub(fix, text)


def _recase_legal_form(token: str) -> str:
    letters = re.sub(r"[^A-Za-z]", "", token)
    form = _FORM_CASE.get(letters.lower())
    if not form or letters == form or letters.isupper() or letters.islower():
        return token
    chars, i = list(token), 0
    for position, char in enumerate(chars):
        if char.isalpha():
            chars[position] = form[i]
            i += 1
    return "".join(chars)


def _split_glued_legal_form(token: str) -> str:
    """"PaperB.V." becomes "Paper B.V."; a token that is only a legal form is left alone."""
    letters = re.sub(r"[^A-Za-z]", "", token)
    for form in sorted(_FORM_CASE, key=len, reverse=True):
        if letters.lower().endswith(form) and len(letters) > len(form):
            # Walk back over the token to the first letter of the form.
            count, cut = 0, len(token)
            while cut > 0 and count < len(form):
                cut -= 1
                if token[cut].isalpha():
                    count += 1
            if cut > 0 and token[cut - 1].islower() and token[cut].isupper():
                return token[:cut] + " " + token[cut:]
    return token


def normalize_ocr_text(text: str) -> str:
    """Re-open dropped word boundaries, read IBAN zeros as digits and re-case a mixed-case legal form."""
    text = _KEYWORD_GLUE.sub(" ", " ".join(text.split()))
    text = _fix_iban_zeros(text)
    for pattern in _RESPACE:
        text = pattern.sub(" ", text)
    text = _AMPERSAND.sub(" & ", text)
    tokens = text.split(" ")
    if tokens:
        tokens[-1] = _split_glued_legal_form(tokens[-1])
        last = tokens[-1].split(" ")
        last[-1] = _recase_legal_form(last[-1])
        tokens[-1] = " ".join(last)
    return " ".join(tokens)


# ---- reading a document --------------------------------------------------------------------------------------

_ENGINE: Any = None


def _engine() -> Any:
    global _ENGINE
    if _ENGINE is None:
        import cv2
        from rapidocr_onnxruntime import RapidOCR

        # OpenCV's own thread pool crashed inside resize when other thread pools and subprocesses were alive in
        # the same process; inference runs in onnxruntime's pool, so OpenCV works single threaded here.
        cv2.setNumThreads(0)
        # Invoices are read upright, so the angle classifier is off; it mis-rotated a label in testing.
        _ENGINE = RapidOCR(use_cls=False, max_side_len=max(side or 0 for side in PASS_LONG_SIDES) + 1000)
    return _ENGINE


def _ocr_pass(image: Any, long_side: int | None) -> list[tuple[tuple[float, float, float, float], str, float]]:
    import numpy as np

    scale = 1.0
    if long_side is not None and max(image.size) != long_side:
        scale = long_side / max(image.size)
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
    result, _ = _engine()(np.asarray(image.convert("RGB")))
    lines = []
    for box, text, score in result or []:
        xs = [point[0] / scale for point in box]
        ys = [point[1] / scale for point in box]
        if text and text.strip():
            lines.append(((min(xs), min(ys), max(xs), max(ys)), text, float(score)))
    return lines


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _ocr_lines(image: Any, page: int) -> list[Line]:
    """Recognition passes merged per line of the first pass: more spaces wins, then the higher score."""
    base = _ocr_pass(image, PASS_LONG_SIDES[0])
    others = [_ocr_pass(image, side) for side in PASS_LONG_SIDES[1:]]
    lines = []
    for box, text, score in base:
        for other in others:
            best = max(other, key=lambda item: _overlap(box, item[0]), default=None)
            if best is not None and _overlap(box, best[0]) >= 0.5:
                if (best[1].count(" "), best[2]) > (text.count(" "), score):
                    text, score = best[1], best[2]
        lines.append(Line(page, normalize_ocr_text(text), round(score, 3), *box))
    return [line for line in lines if line.text]


def _text_layer_lines(page_object: Any, page: int) -> list[Line]:
    lines = []
    for block in page_object.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = " ".join("".join(span["text"] for span in line["spans"]).split())
            if text:
                x0, y0, x1, y1 = line["bbox"]
                lines.append(Line(page, text, 1.0, x0, y0, x1, y1))
    return lines


def read_document(path: Path) -> Reading:
    """Every line of every page, from the text layer when the PDF has one, else from OCR."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pymupdf

        try:
            document = pymupdf.open(path)
        except Exception as exc:  # pymupdf raises its own hierarchy
            raise ReadError(f"cannot open PDF: {path.name}") from exc
        with document:
            if document.page_count == 0:
                raise ReadError(f"PDF has no pages: {path.name}")
            count = min(document.page_count, MAX_PAGES)
            # Page objects are used one at a time and never kept past the document, which MuPDF requires.
            layered = all(len(document[i].get_text().strip()) >= MIN_TEXT_LAYER_CHARS for i in range(count))
            lines: list[Line] = []
            heights = {}
            renders: list[bytes] = []
            for index in range(count):
                page = document[index]
                if layered:
                    lines.extend(_text_layer_lines(page, index + 1))
                    heights[index + 1] = float(page.rect.height)
                else:
                    renders.append(page.get_pixmap(dpi=RENDER_DPI).tobytes("png"))
                del page
        if layered:
            return Reading(MODE_TEXT_LAYER, tuple(lines), heights)
        from PIL import Image

        for number, png in enumerate(renders, 1):
            image = Image.open(io.BytesIO(png))
            image.load()
            lines.extend(_ocr_lines(image, number))
            heights[number] = float(image.height)
        return Reading(MODE_OCR, tuple(lines), heights)
    if suffix in IMAGE_SUFFIXES:
        from PIL import Image

        try:
            image = Image.open(path)
            image.load()
        except Exception as exc:  # PIL raises several types for a bad file
            raise ReadError(f"cannot open image: {path.name}") from exc
        return Reading(MODE_OCR, tuple(_ocr_lines(image, 1)), {1: float(image.height)})
    raise ReadError(f"unsupported file type: {path.name}")


# ---- finding the seven fields --------------------------------------------------------------------------------

def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9#]", "", text.lower())


def _after_label(text: str, label: re.Pattern[str]) -> str | None:
    """The text that follows a label on the same line, or None when the line is not that label."""
    match = label.match(text)
    if not match:
        return None
    return text[match.end():].strip(" :;-#.").strip()


def _same_row(anchor: Line, line: Line) -> bool:
    overlap = min(anchor.y1, line.y1) - max(anchor.y0, line.y0)
    return overlap > 0.4 * min(anchor.height, line.height)


def _x_aligned(anchor: Line, line: Line) -> bool:
    overlap = min(anchor.x1, line.x1) - max(anchor.x0, line.x0)
    return overlap > 0 or abs(line.x0 - anchor.x0) <= 1.5 * anchor.height


def _right_of(lines: list[Line], anchor: Line) -> list[Line]:
    found = [line for line in lines if line.page == anchor.page and line is not anchor
             and line.x0 >= anchor.x1 - 0.5 * anchor.height and _same_row(anchor, line)]
    return sorted(found, key=lambda line: line.x0)


def _below(lines: list[Line], anchor: Line, *, within: float = 3.0) -> list[Line]:
    found = [line for line in lines if line.page == anchor.page and line is not anchor
             and line.y0 >= anchor.y1 - 0.3 * anchor.height and line.y0 - anchor.y1 <= within * anchor.height
             and _x_aligned(anchor, line)]
    return sorted(found, key=lambda line: (line.y0, line.x0))


_LABELS: dict[str, tuple[re.Pattern[str], ...]] = {
    "invoice_id": (re.compile(r"(?i)^\s*(?:tax\s*)?invoice\s*(?:no\.?|number|nr\.?|num\.?|id|#)\s*[:#]?"),),
    "po_reference": (re.compile(
        r"(?i)^\s*(?:your|customer|buyer)?\s*(?:purchase\s*order|p\.?\s?o\.?)\s*(?:no\.?|number|nr\.?|num\.?|reference|ref\.?|#)?\s*[:#]?"
    ),),
    "company_code": (re.compile(r"(?i)^.*?\bcompany\s*code\s*[:#]?"),),
    "payment_terms": (
        re.compile(r"(?i)^\s*payment\s*terms?\s*[:#]?"),
        re.compile(r"(?i)^\s*terms?\s*of\s*payment\s*[:#]?"),
        re.compile(r"(?i)^\s*payment\s*conditions?\s*[:#]?"),
        re.compile(r"(?i)^\s*terms\s*[:#]?"),
    ),
    "invoice_value": (
        re.compile(r"(?i)^\s*(?:total\s*)?amount\s*due\s*[:#]?"),
        re.compile(r"(?i)^\s*total\s*due\s*(?:eur|usd|gbp|chf)?\s*[:#]?"),
        re.compile(r"(?i)^\s*balance\s*due\s*[:#]?"),
        re.compile(r"(?i)^\s*(?:invoice|grand)\s*total\s*[:#]?"),
        re.compile(r"(?i)^\s*total\s*(?:amount)?\s*(?:eur|usd|gbp|chf)?\s*[:#]?"),
    ),
}
_DISCOUNT_LABEL = re.compile(r"(?i)^\s*(?:early\s*payment\s*discount|cash\s*discount|discount\s*terms?|discount)\s*[:#]?")
_VENDOR_BLOCK = re.compile(r"(?i)^\s*(?:from|supplier|vendor|seller|sold\s*by|issued\s*by|invoice\s*from)\s*[:#]?\s*$")
_REMIT_BLOCK = re.compile(r"(?i)^\s*(?:remit\s*to|payable\s*to|pay\s*to|remittance)\s*[:#]?\s*$")
_ADDRESS_LABEL = re.compile(r"(?i)^\s*(?:(?:registered|supplier|vendor|business)\s*)?address\s*[:#]?\s*$")

_ID_VALUE = re.compile(r"^(?=.*\d)[A-Z0-9][A-Z0-9/.-]{3,40}$", re.I)
_PO_VALUE = re.compile(r"^\d{6,12}$")
_CODE_VALUE = re.compile(r"^\d{3,6}$")
_AMOUNT = re.compile(
    r"^(?:(?:EUR|USD|GBP|CHF|€|\$|£)\s?)?-?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?(?:\s?(?:EUR|USD|GBP|CHF))?$"
)
_CURRENCY = re.compile(r"(?i)\b(?:EUR|USD|GBP|CHF)\b|[€$£]")
_TERM_VALUE = re.compile(r"(?i)\bnet\s*\d|\b\d{1,3}\s*days?\b|due\s*(?:on|upon)\s*receipt|\b\d+(?:[.,]\d+)?\s*%?\s*/\s*\d")
_DISCOUNT_VALUE = re.compile(r"(?i)\d+(?:[.,]\d+)?\s*%?\s*/\s*\d{1,3}|\d+(?:[.,]\d+)?\s*%.{0,20}\b\d{1,3}\s*days?")
_TITLE_WORDS = re.compile(r"(?i)^\s*(?:(?:tax|freight|commercial|pro\s*forma|proforma)\s*)?(?:invoice|factuur|rechnung|facture|fattura|factura|invoice\s*details|document\s*view)\s*$")
_STOP_LINE = re.compile(
    r"(?i)^\s*(?:tel|phone|fax|mobile|e-?mail|web|www\.|http|vat|tax\s*id|ust|ein\b|iban|bic|swift|ach|routing|account|"
    r"sort\s*code|bank|lockbox|company\s*(?:code|no|number|reg)|registered|chamber|page\b|invoice|date|due|po\b|customer|"
    r"delivery|ship|bill\s*to|invoice\s*to|sold\s*to|contact|notes?\b|terms|payment|remit|order|description|qty|quantity)"
    r"|@|\+\d[\d\s().-]{5,}$|^\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$"
)
_UPPER_HEADING = re.compile(r"^[A-Z][A-Z\s&/.-]{1,30}$")


def _looks_like_name(text: str) -> bool:
    if "@" in text or _TITLE_WORDS.match(text) or _STOP_LINE.match(text):
        return False
    letters = sum(char.isalpha() for char in text)
    digits = sum(char.isdigit() for char in text)
    words = text.split()
    return letters >= 4 and digits <= 2 and (len(words) >= 2 or _legal_form(text) is not None)


def _legal_form(text: str) -> str | None:
    tokens = [re.sub(r"[^A-Za-z]", "", token).lower() for token in text.split()]
    tokens = [token for token in tokens if token]
    return LEGAL_FORMS.get(tokens[-1]) if len(tokens) > 1 else None


def _is_street_line(text: str) -> bool:
    return bool(re.search(r"\d", text)) and not _STOP_LINE.match(text) and len(text) <= 60


def _is_address_continuation(text: str) -> bool:
    if _STOP_LINE.match(text) or "@" in text:
        return False
    if _UPPER_HEADING.match(text) and not re.search(r"\d", text):
        return False
    return bool(re.search(r"\d", text)) or len(text.split()) <= 3


def _address_block(lines: list[Line], anchor: Line) -> list[Line]:
    """Up to four aligned lines under the anchor: a street line (with a digit), then short or numbered lines.

    Up to two lines without a digit (a tagline under a letterhead) may sit between the anchor and the street
    line; a stop line (phone, mail, tax or bank detail, another label) ends the search or the block.
    """
    block: list[Line] = []
    skipped = 0
    previous = anchor
    for line in _below(lines, anchor, within=8.0):
        if line.y0 - previous.y1 > 1.8 * max(previous.height, line.height):
            break
        if not block:
            if _is_street_line(line.text):
                block.append(line)
            elif _STOP_LINE.match(line.text) or skipped == 2:
                break
            else:
                skipped += 1
        elif _is_address_continuation(line.text):
            block.append(line)
        else:
            break
        previous = line
        if len(block) == 4:
            break
    return block


@dataclass
class Found:
    value: str
    quote_lines: list[Line]


def _value_near(lines: list[Line], anchor: Line, label: re.Pattern[str], accept: re.Pattern[str]) -> Found | None:
    inline = _after_label(anchor.text, label)
    if inline and accept.search(inline):
        return Found(inline, [anchor])
    if inline:
        return None
    for candidate in _right_of(lines, anchor)[:2] + _below(lines, anchor)[:2]:
        text = candidate.text.strip()
        if accept.search(text) and not any(pattern.match(text) for patterns in _LABELS.values() for pattern in patterns):
            return Found(text, [anchor, candidate])
    return None


def _find_labelled(lines: list[Line], field: str, accept: re.Pattern[str], *, lowest: bool = False) -> Found | None:
    for label in _LABELS[field]:
        anchors = [line for line in lines if label.match(line.text)]
        if lowest:
            anchors.sort(key=lambda line: (line.page, -line.y0))
        for anchor in anchors:
            found = _value_near(lines, anchor, label, accept)
            if found:
                return found
    return None


def _find_company_code(lines: list[Line]) -> Found | None:
    label = _LABELS["company_code"][0]
    for line in lines:
        after = _after_label(line.text, label)
        if after is None:
            continue
        match = re.match(r"^(\d{3,6})\b", after)
        if match:
            return Found(match.group(1), [line])
        for candidate in _right_of(lines, line)[:1] + _below(lines, line)[:1]:
            if _CODE_VALUE.match(candidate.text.strip()):
                return Found(candidate.text.strip(), [line, candidate])
    return None


def _find_terms(lines: list[Line]) -> Found | None:
    term = _find_labelled(lines, "payment_terms", _TERM_VALUE)
    discount = None
    for anchor in (line for line in lines if _DISCOUNT_LABEL.match(line.text)):
        discount = _value_near(lines, anchor, _DISCOUNT_LABEL, _DISCOUNT_VALUE)
        if discount:
            break
    if term is None:
        return discount
    if discount is None or discount.value in term.value:
        return term
    from .rules import parse_term

    parsed = parse_term(discount.value)
    value = discount.value if parsed and parsed.net_days is not None else f"{term.value}, {discount.value}"
    return Found(value, term.quote_lines + [line for line in discount.quote_lines if line not in term.quote_lines])


def _name_under(lines: list[Line], anchor: Line) -> Line | None:
    below = [line for line in _below(lines, anchor, within=2.0) if _looks_like_name(line.text)]
    return below[0] if below else None


def _find_vendor(reading: Reading) -> tuple[Found | None, Found | None]:
    """The supplier's name and address.

    Name candidates in order: the line under a supplier heading (From, Supplier, Vendor), the letterhead (the
    first name-like line in the top quarter of page 1, one with a legal form first), the line under a remit-to
    heading. The address is the block under an Address heading, else the block under the first name candidate
    that has one.
    """
    lines = list(reading.lines)
    candidates: list[Found] = []
    for anchor in (line for line in lines if _VENDOR_BLOCK.match(line.text)):
        name_line = _name_under(lines, anchor)
        if name_line:
            candidates.append(Found(name_line.text, [anchor, name_line]))
            break
    top = 0.25 * reading.page_heights.get(1, 0.0)
    head = [line for line in lines if line.page == 1 and line.y0 <= top and _looks_like_name(line.text)]
    for line in sorted(head, key=lambda line: (_legal_form(line.text) is None, lines.index(line))):
        candidates.append(Found(line.text, [line]))
        break
    for anchor in (line for line in lines if _REMIT_BLOCK.match(line.text)):
        name_line = _name_under(lines, anchor)
        if name_line:
            candidates.append(Found(name_line.text, [anchor, name_line]))
            break
    name = candidates[0] if candidates else None
    anchors = [line for line in lines if _ADDRESS_LABEL.match(line.text)]
    anchors += [candidate.quote_lines[-1] for candidate in candidates]
    for anchor in anchors:
        block = _address_block(lines, anchor)
        if block:
            return name, Found("\n".join(line.text for line in block), block)
    return name, None


def extract(reading: Reading) -> Extraction:
    """The seven fields and the guard's flags from a reading. A field that is not found stays null."""
    lines = list(reading.lines)
    found: dict[str, Found | None] = {
        "invoice_id": _find_labelled(lines, "invoice_id", _ID_VALUE),
        "po_reference": _find_labelled(lines, "po_reference", _PO_VALUE),
        "company_code": _find_company_code(lines),
        "invoice_value": _find_labelled(lines, "invoice_value", _AMOUNT, lowest=True),
        "payment_terms": _find_terms(lines),
    }
    found["vendor_name"], found["vendor_address"] = _find_vendor(reading)
    amount = found["invoice_value"]
    if amount and len(amount.quote_lines) > 1 and not _CURRENCY.search(amount.value):
        # The currency printed in the label column ("TOTAL DUE EUR") applies to the amount beside it.
        code = _CURRENCY.search(" ".join(line.text for line in amount.quote_lines[:-1]))
        if code:
            amount.value = f"{code.group(0).upper()} {amount.value}"
    fields = {}
    for name in FIELD_NAMES:
        item = found[name]
        if item is None:
            fields[name] = FieldValue(value=None, page=None, quote=None, confidence=0.0)
            continue
        quote_lines = sorted(item.quote_lines, key=lambda line: lines.index(line))
        quote = "\n".join(line.text for line in quote_lines)
        fields[name] = FieldValue(
            value=item.value[:200],
            page=quote_lines[0].page,
            quote=quote[:240],
            confidence=round(min(line.score for line in quote_lines), 2),
        )
    flags = []
    seen = set()
    for page in sorted({line.page for line in lines}):
        text = "\n".join(line.text for line in lines if line.page == page)
        for hit in scan_text(text):
            key = (hit.type, hit.quote, page)
            if key not in seen and len(flags) < 10:
                seen.add(key)
                flags.append(Flag(type=hit.type, quote=hit.quote[:240], page=page))
    return Extraction(**fields, flags=flags)


# ---- transcripts ---------------------------------------------------------------------------------------------

def transcript(reading: Reading, extraction: Extraction, *, source_path: str, source_sha256: str, rule_version: str) -> dict[str, Any]:
    model = TEXT_LAYER_MODEL if reading.mode == MODE_TEXT_LAYER else OCR_MODEL
    if reading.mode == MODE_TEXT_LAYER:
        note = ("Read offline by invoice_agent.reader from the PDF text layer; every quote is a line of that "
                "layer and confidence is 1.0 because no recognition took place.")
    else:
        note = ("Read offline by invoice_agent.reader with RapidOCR: two recognition passes per page, the reading "
                "with more spaces kept per line, dropped word boundaries re-opened; each confidence is the lowest "
                "recognizer score over the lines quoted.")
    return {
        "metadata": {
            "source_sha256": source_sha256,
            "role": "extractor",
            "mode": reading.mode,
            "schema_version": 1,
            "prompt_version": 1,
            "rule_version": rule_version,
            "source_path": source_path,
            "provenance_note": note,
            "reader_version": READER_VERSION,
            "model": model,
        },
        "response": extraction.model_dump(mode="json"),
        "reading": {
            "lines": [{"page": line.page, "text": line.text, "score": line.score} for line in reading.lines],
        },
    }


def dump_transcript(envelope: dict[str, Any]) -> str:
    """One field, flag or line per row, so a diff of two transcripts reads line by line."""
    def row(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))

    metadata = ",\n".join(f"    {json.dumps(k)}: {row(v)}" for k, v in envelope["metadata"].items())
    response = envelope["response"]
    fields = ",\n".join(f"    {json.dumps(name)}: {row(response[name])}" for name in response if name != "flags")
    flags = ",\n".join(f"      {row(flag)}" for flag in response.get("flags", []))
    flags_block = "[\n" + flags + "\n    ]" if flags else "[]"
    lines = ",\n".join(f"      {row(line)}" for line in envelope["reading"]["lines"])
    return (
        "{\n  \"metadata\": {\n" + metadata + "\n  },\n"
        "  \"response\": {\n" + fields + ",\n    \"flags\": " + flags_block + "\n  },\n"
        "  \"reading\": {\n    \"lines\": [\n" + lines + "\n    ]\n  }\n}\n"
    )
