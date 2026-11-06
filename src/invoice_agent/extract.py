"""Offline extraction replay with source-byte provenance and strict caps."""
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
    altogether, and a value outside its quote would not be what the source shows. PDF and image sources
    cannot be checked this way without OCR. The message names the field, never the quote.
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


def load_replay(
    invoice_id: str,
    transcripts_dir: Path,
    *,
    root: Path = REPO_ROOT,
    source_path: Path | None = None,
    policy: Policy | None = None,
) -> ReplayExtraction:
    """Load one curated replay and verify it against the exact source bytes."""
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
    if not isinstance(envelope, dict) or set(envelope) != {"metadata", "response"}:
        raise ReplayError("transcript envelope must contain only metadata and response")
    metadata = envelope["metadata"]
    required = {
        "source_sha256",
        "role",
        "mode",
        "schema_version",
        "prompt_version",
        "rule_version",
        "source_path",
        "provenance_note",
    }
    if not isinstance(metadata, dict) or set(metadata) != required:
        raise ReplayError(f"transcript metadata must contain exactly {sorted(required)}")
    expected = {
        "role": "extractor",
        "mode": "curated_synthetic",
        "schema_version": 1,
        "prompt_version": 1,
        "rule_version": policy.rule_version,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise ReplayError(f"transcript metadata {key} must be {value!r}")
    if not isinstance(metadata["provenance_note"], str) or not metadata["provenance_note"].strip():
        raise ReplayError("transcript provenance_note must be non-empty")
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
    if actual_source.suffix.lower() == ".txt":
        _verify_text_quotes(extraction, source_bytes)
    # Quotes remain untrusted data. Detection adds holds; it never changes instructions.
    flags = list(extraction.flags)
    seen = {(flag.type, flag.quote, flag.page) for flag in flags}
    quotes = [(getattr(extraction, name).quote or "", getattr(extraction, name).page)
              for name in FIELD_NAMES]
    if actual_source.suffix.lower() == ".txt":
        quotes.append((source_bytes.decode("utf-8", errors="replace"), None))
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
