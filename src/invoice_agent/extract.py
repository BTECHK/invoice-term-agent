"""Offline extraction replay with source-byte provenance, quote verification and strict caps."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import REPO_ROOT
from .models import Extraction, FIELD_NAMES, Flag
from .guard import scan_text
from .policy import Policy, load_policy
from .rules import normalize_po_reference, parse_amount, parse_term


CURATED_MODE = "curated_synthetic"
READER_MODES = frozenset({"local_ocr", "pdf_text_layer"})
_BASE_METADATA = frozenset({
    "source_sha256", "role", "mode", "schema_version", "prompt_version", "rule_version", "source_path",
    "provenance_note",
})
_READER_METADATA = _BASE_METADATA | {"reader_version", "model"}
MAX_READING_LINES = 5000
MAX_LINE_CHARS = 1000


class ReplayError(RuntimeError):
    """A replay transcript is missing, malformed, or does not match its source."""


@dataclass(frozen=True)
class ReplayExtraction:
    extraction: Extraction
    metadata: dict[str, Any]
    source_path: Path
    source_sha256: str
    semantic_sha256: str
    transcript_sha256: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _inside(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ReplayError("transcript source_path must be relative to the agent root")
    root = root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ReplayError("transcript source_path escapes the agent root") from exc
    return path


def _verify_text_quotes(extraction: Extraction, source_bytes: bytes) -> None:
    """Every field of a text source needs a quote that is a passage of the source and holds the field's value.

    Whitespace is folded and case is kept. A field with a value and no quote would skip the source check
    altogether, and a value outside its quote would not be what the source shows. The message names the
    field, never the quote.
    """
    text = " ".join(source_bytes.decode("utf-8", errors="replace").split())
    for name in FIELD_NAMES:
        field = getattr(extraction, name)
        quote = " ".join((field.quote or "").split())
        if not quote:
            raise ReplayError(f"{name} has no quote in a text source")
        if quote not in text:
            raise ReplayError(f"{name} quote does not occur in the text source")
        if field.value is not None and " ".join(field.value.split()) not in quote:
            raise ReplayError(f"{name} value does not occur inside its quote")


def _validated_reading(raw: Any, policy: Policy) -> list[tuple[int, str, float]]:
    """The lines the reader kept, as (page, text, score). Shape and caps are checked; content is data."""
    if not isinstance(raw, dict) or set(raw) != {"lines"} or not isinstance(raw["lines"], list):
        raise ReplayError("transcript reading must be an object with a lines list")
    lines = raw["lines"]
    if not 1 <= len(lines) <= MAX_READING_LINES:
        raise ReplayError(f"transcript reading must hold 1 to {MAX_READING_LINES} lines")
    out = []
    for line in lines:
        if not isinstance(line, dict) or set(line) != {"page", "text", "score"}:
            raise ReplayError("each reading line must carry exactly page, text and score")
        page, text, score = line["page"], line["text"], line["score"]
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= policy.max_pages:
            raise ReplayError(f"reading line page must be an integer from 1 to {policy.max_pages}")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_LINE_CHARS:
            raise ReplayError(f"reading line text must be a non-empty string of at most {MAX_LINE_CHARS} characters")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            raise ReplayError("reading line score must be a number from 0 to 1")
        out.append((page, text, float(score)))
    return out


def _verify_reading_quotes(extraction: Extraction, reading: list[tuple[int, str, float]]) -> None:
    """Every field with a value quotes one or more lines of the reading, in order, on its page, and holds the value.

    Whitespace is folded within a line and case is kept. The quote's lines must each equal a line the reader
    kept for that page and follow one another in reading order, so a quote that was edited by hand, moved to
    another page or stitched from text the reader never saw is refused. The message names the field only.
    """
    folded = [(page, " ".join(text.split())) for page, text, _ in reading]
    for name in FIELD_NAMES:
        field = getattr(extraction, name)
        if field.value is None:
            continue
        if not field.quote or not field.quote.strip():
            raise ReplayError(f"{name} has no quote in a reader transcript")
        if field.page is None:
            raise ReplayError(f"{name} has a value but no page")
        position = -1
        parts = [" ".join(part.split()) for part in field.quote.split("\n")]
        for part in parts:
            index = next((i for i in range(position + 1, len(folded))
                          if folded[i][0] == field.page and folded[i][1] == part), None)
            if index is None:
                raise ReplayError(f"{name} quote is not a line of the reading on its page")
            position = index
        if " ".join(field.value.split()) not in " ".join(parts):
            raise ReplayError(f"{name} value does not occur inside its quote")


def load_replay(
    invoice_id: str,
    transcripts_dir: Path,
    *,
    root: Path = REPO_ROOT,
    source_path: Path | None = None,
    policy: Policy | None = None,
) -> ReplayExtraction:
    """Load one extraction transcript and verify it against the exact source bytes and its own reading."""
    root = Path(root).resolve()
    policy = policy or load_policy(root)
    transcript = Path(transcripts_dir) / invoice_id / "extractor.json"
    if not transcript.is_file():
        raise ReplayError(f"missing extractor transcript for {invoice_id}: {transcript}")
    transcript_bytes = transcript.read_bytes()
    try:
        envelope = json.loads(transcript_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"malformed extractor transcript for {invoice_id}") from exc
    if not isinstance(envelope, dict) or set(envelope) not in ({"metadata", "response"}, {"metadata", "response", "reading"}):
        raise ReplayError("transcript envelope must contain metadata and response, and at most a reading")
    metadata = envelope["metadata"]
    mode = metadata.get("mode") if isinstance(metadata, dict) else None
    required = _READER_METADATA if mode in READER_MODES else _BASE_METADATA
    if not isinstance(metadata, dict) or set(metadata) != required:
        raise ReplayError(f"transcript metadata must contain exactly {sorted(required)}")
    if mode != CURATED_MODE and mode not in READER_MODES:
        raise ReplayError(f"transcript metadata mode must be {CURATED_MODE!r} or one of {sorted(READER_MODES)}")
    expected = {
        "role": "extractor",
        "schema_version": 1,
        "prompt_version": 1,
        "rule_version": policy.rule_version,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise ReplayError(f"transcript metadata {key} must be {value!r}")
    for key in ("provenance_note", *(("reader_version", "model") if mode in READER_MODES else ())):
        if not isinstance(metadata[key], str) or not metadata[key].strip():
            raise ReplayError(f"transcript {key} must be non-empty")
    reading = _validated_reading(envelope["reading"], policy) if "reading" in envelope else None
    declared_source = _inside(root, str(metadata["source_path"]))
    actual_source = Path(source_path).resolve() if source_path is not None else declared_source
    if source_path is not None and actual_source.name != declared_source.name:
        raise ReplayError("source override filename does not match transcript source_path")
    if not actual_source.is_file():
        raise ReplayError(f"source file is missing: {actual_source}")
    if actual_source.stat().st_size > policy.max_file_bytes:
        raise ReplayError(f"source exceeds the {policy.max_file_bytes} byte cap")
    with actual_source.open("rb") as handle:
        source_bytes = handle.read(policy.max_file_bytes + 1)
    if len(source_bytes) > policy.max_file_bytes:
        raise ReplayError(f"source exceeds the {policy.max_file_bytes} byte cap")
    source_hash = _sha256(source_bytes)
    if metadata["source_sha256"] != source_hash:
        raise ReplayError(f"source hash mismatch for {invoice_id}")
    suffix = actual_source.suffix.lower()
    if suffix == ".txt":
        if mode != CURATED_MODE or reading is not None:
            raise ReplayError("a text source replays a curated_synthetic transcript without a reading")
    else:
        if mode not in READER_MODES or reading is None:
            raise ReplayError("a PDF or image source needs a reader transcript with its reading")
        if mode == "pdf_text_layer" and suffix != ".pdf":
            raise ReplayError("pdf_text_layer mode needs a PDF source")
    try:
        extraction = Extraction.model_validate(envelope["response"])
    except ValidationError as exc:
        raise ReplayError("extractor response failed schema validation") from exc
    if extraction.invoice_id.value != invoice_id:
        raise ReplayError("transcript invoice_id does not match its directory")
    pages = [getattr(extraction, name).page for name in FIELD_NAMES]
    pages.extend(flag.page for flag in extraction.flags)
    if any(page is not None and page > policy.max_pages for page in pages):
        raise ReplayError(f"extraction cites a page above the {policy.max_pages} page cap")
    if suffix == ".txt":
        _verify_text_quotes(extraction, source_bytes)
    else:
        assert reading is not None
        _verify_reading_quotes(extraction, reading)
    # Quotes and the reading remain untrusted data. Detection adds holds; it never changes instructions.
    flags = list(extraction.flags)
    seen = {(flag.type, flag.quote, flag.page) for flag in flags}
    quotes = [(getattr(extraction, name).quote or "", getattr(extraction, name).page)
              for name in FIELD_NAMES]
    if suffix == ".txt":
        quotes.append((source_bytes.decode("utf-8", errors="replace"), None))
    else:
        assert reading is not None
        for page in sorted({page for page, _, _ in reading}):
            quotes.append(("\n".join(text for p, text, _ in reading if p == page), page))
    for quote, page in quotes:
        for hit in scan_text(quote):
            key = (hit.type, hit.quote, page)
            if key not in seen:
                seen.add(key)
                flags.append(Flag(type=hit.type, quote=hit.quote, page=page))
    if len(flags) > 10:
        raise ReplayError("instruction flags exceed the extraction report cap")
    extraction = extraction.model_copy(update={"flags": flags})
    semantic = _sha256(_canonical(extraction.model_dump(mode="json")))
    return ReplayExtraction(
        extraction=extraction,
        metadata=dict(metadata),
        source_path=actual_source,
        source_sha256=source_hash,
        semantic_sha256=semantic,
        transcript_sha256=_sha256(transcript_bytes),
    )


def normalized_fields(extraction: Extraction) -> dict[str, Any]:
    """Normalize source strings only after the strict extraction has been retained."""
    raw = {name: getattr(extraction, name).value for name in FIELD_NAMES}
    vendor = raw["vendor_name"]
    if vendor:
        raw["vendor_name"] = vendor.strip()
    amount, _ = parse_amount(raw["invoice_value"])
    raw["invoice_value"] = float(amount) if amount is not None else None
    address = raw["vendor_address"]
    if address:
        raw["vendor_address"] = ", ".join(
            part.strip(" ,") for part in str(address).splitlines() if part.strip(" ,")
        )
    term = parse_term(raw["payment_terms"])
    if term and term.discount_pct is not None and term.net_days is not None:
        raw["payment_terms"] = f"NET {term.net_days} DAYS"
    elif term and term.net_days is not None and re.search(
        r"payment\s+is\s+due\s+within", str(raw["payment_terms"]), re.I
    ):
        raw["payment_terms"] = f"{term.net_days} days net"
    raw["po_reference"] = normalize_po_reference(raw["po_reference"])
    return raw
