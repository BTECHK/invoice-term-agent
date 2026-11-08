"""Offline command line runner: the local reader, extraction replay and the stdio mock."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import REPO_ROOT
from .audit import AuditTrail
from .batch import run_batch, safe_error
from .erp_client import ErpClient
from .decide import decide
from .exceptions import GatheredFacts, Resolution, RunBudget, gather_facts, resolve_exception
from .extract import ReplayError, load_replay, normalized_fields
from .guard import mask_account_numbers
from .models import EvidenceItem, MoneyImpact, WorklistRecord
from .policy import load_policy
from .propose import apply_checker, build_proposal, check_proposal
from .worklist import BundleWriter, InputConflict, WriteFailure, _safe_invoice_id, semantic_input_hash

DEFAULT_INPUTS = (
    "evals/golden/invoices/NBF-0051376.png",
    "evals/golden/invoices/PCS-25-03917.png",
    "evals/golden/invoices/KBP-2025-0417.pdf",
    "evals/golden/invoices/RIC-250319-00718.pdf",
    "evals/golden/invoices/FPC-25-10733.pdf",
    "evals/golden/injection/DLP-2025-0907.png",
)
SOURCE_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".txt"})
# Free text can carry an account number. Identifiers (po_reference, invoice_id, company_code) are long
# digit strings by design, so masking them would corrupt the record.
MASKED_FIELDS = ("vendor_name", "vendor_address", "payment_terms")


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the sandbox replay AP payment-term replay.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--transcripts", type=Path, default=REPO_ROOT / "transcripts")
    parser.add_argument("--fixtures", type=Path, default=REPO_ROOT / "mock_erp" / "fixtures")
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--mode", choices=("replay", "live"), default="replay")
    parser.add_argument("--live", action="store_true", help="unsupported; live execution is outside this build")
    return parser.parse_args(argv)


def _input_map(args: argparse.Namespace) -> list[tuple[str, Path | ReplayError | None]]:
    if args.inputs is None:
        return [(Path(relative).stem, REPO_ROOT / relative) for relative in DEFAULT_INPUTS]
    root = args.inputs.resolve()
    if not root.is_dir():
        raise ReplayError(f"inputs directory is missing: {root}")
    available: dict[str, list[Path]] = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES:
            available.setdefault(path.name, []).append(path)
    selected: list[tuple[str, Path | ReplayError | None]] = []
    for directory in sorted(args.transcripts.iterdir() if args.transcripts.is_dir() else []):
        transcript = directory / "extractor.json"
        if not directory.is_dir():
            continue
        try:
            envelope = json.loads(transcript.read_text(encoding="utf-8"))
            name = Path(envelope["metadata"]["source_path"]).name
        except (OSError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            selected.append((directory.name, ReplayError("cannot select input for missing or malformed transcript")))
            continue
        matches = available.get(name, [])
        if len(matches) != 1:
            selected.append((directory.name, ReplayError("expected exactly one matching input")))
            continue
        selected.append((directory.name, matches[0]))
    if not selected:
        raise ReplayError("no transcript-backed inputs were found")
    return selected


def _budget(policy: Any, role: str) -> RunBudget:
    cap = policy.caps[role]
    return RunBudget(cap.max_steps, cap.max_cost_usd, cap.max_reflection_cycles)


def _safe_flags(replay: Any) -> list[dict[str, Any]]:
    return [
        {"type": flag.type, "quote": mask_account_numbers(flag.quote), "page": flag.page}
        for flag in replay.extraction.flags
    ]


def _audit_sink(trail: AuditTrail):
    def record(
        tool: str,
        arguments: dict[str, Any],
        output: dict[str, Any] | None,
        error: BaseException | None,
    ) -> None:
        trail.event(
            f"mcp__erp__{tool}",
            arguments,
            output,
            allowed=error is None,
            note=f"attempt {arguments.get('attempt', 1)}: " + (
                "structured MCP response validated" if error is None else type(error).__name__
            ),
        )

    return record


def _persist_failure_audit(
    writer: BundleWriter,
    invoice_id: str,
    error: BaseException,
    *trails: AuditTrail,
) -> None:
    failure = trails[-1]
    error.stage = failure.role
    failure.event(
        "role_failure",
        {"invoice_id": invoice_id},
        allowed=False,
        note=f"{type(error).__name__}: {safe_error(error)}",
    )
    writer.append_audit(
        invoice_id,
        sorted(
            (event for trail in trails for event in trail.events),
            key=lambda event: event.timestamp,
        ),
    )


async def _run_one(
    invoice_id: str,
    source: Path | None,
    *,
    args: argparse.Namespace,
    writer: BundleWriter,
    client: ErpClient,
    policy: Any,
    replay: Any = None,
) -> str:
    extractor_audit = AuditTrail(invoice_id, "extractor")
    try:
        replay = replay or load_replay(
            invoice_id,
            args.transcripts,
            root=REPO_ROOT,
            source_path=source,
            policy=policy,
        )
    except Exception as exc:
        _persist_failure_audit(writer, invoice_id, exc, extractor_audit)
        raise
    extractor_audit.event(
        "replay_extraction",
        {"source_sha256": replay.source_sha256},
        replay.extraction.model_dump(mode="json"),
        note=f"{replay.metadata['mode']} transcript verified against source bytes",
    )
    orchestrator_audit = AuditTrail(invoice_id, "orchestrator")
    orchestrator_budget = _budget(policy, "orchestrator")
    try:
        with client.audit_to(_audit_sink(orchestrator_audit)):
            facts: GatheredFacts = await gather_facts(
                replay.extraction,
                client,
                budget=orchestrator_budget,
                min_confidence=policy.min_confidence,
            )
    except Exception as exc:
        _persist_failure_audit(
            writer, invoice_id, exc, extractor_audit, orchestrator_audit
        )
        raise
    try:
        decision = decide(facts.invoice, facts.po, list(facts.validated), policy)
        candidate = build_proposal(
            decision,
            invoice_id=invoice_id,
            po_reference=facts.invoice.po_reference,
            rule_version=policy.rule_version,
            checker_agrees=True,
        )
    except ArithmeticError as exc:
        _persist_failure_audit(
            writer, invoice_id, exc, extractor_audit, orchestrator_audit
        )
        raise
    checker_audit = AuditTrail(invoice_id, "checker")
    try:
        with client.audit_to(_audit_sink(checker_audit)):
            checker_result = await check_proposal(facts.invoice, candidate, client, policy)
    except Exception as exc:
        _persist_failure_audit(
            writer, invoice_id, exc, extractor_audit, orchestrator_audit, checker_audit
        )
        raise
    proposal = apply_checker(candidate, checker_result)
    checker_audit.event(
        "independent_requery_checker",
        candidate.model_dump(mode="json"),
        checker_result.facts,
        note=checker_result.reason,
    )
    exception_types = list(decision.exception_types)
    resolution: Resolution | None = None
    if not checker_result.agrees:
        if "no-validated-term" not in exception_types:
            exception_types.append("no-validated-term")
        order = {name: i for i, name in enumerate(policy.precedence)}
        exception_types.sort(key=lambda name: order.get(name, 99))
        evidence = list(decision.evidence)
        evidence.extend((
            EvidenceItem(source="rules", query_id="checker_expected_day", value=checker_result.expected_day),
            EvidenceItem(source="rules", query_id="checker_observed_day", value=checker_result.proposed_day),
        ))
        resolution = Resolution(
            recommendation=(
                f"Checker disagreement: candidate day {checker_result.proposed_day}, independent day "
                f"{checker_result.expected_day}. Keep the invoice in the worklist for a person."
            ),
            evidence=tuple(evidence[:40]),
        )
    elif exception_types:
        try:
            with client.audit_to(_audit_sink(orchestrator_audit)):
                resolution = await resolve_exception(
                    decision,
                    facts,
                    client,
                    policy=policy,
                    budget=orchestrator_budget,
                )
        except Exception as exc:
            _persist_failure_audit(
                writer,
                invoice_id,
                exc,
                extractor_audit,
                orchestrator_audit,
                checker_audit,
            )
            raise
    record = None
    if exception_types:
        assert resolution is not None
        record = WorklistRecord(
            invoice_id=invoice_id,
            exception_types=exception_types,
            evidence=list(resolution.evidence),
            recommendation=resolution.recommendation,
            money_impact=MoneyImpact(
                amount=float(decision.money_amount),
                currency=decision.currency,
            ),
            created_at=datetime.now(timezone.utc),
            status="open",
            decided_by=None,
            decided_at=None,
        )
    fields = {name: mask_account_numbers(value) if name in MASKED_FIELDS and isinstance(value, str) else value
              for name, value in normalized_fields(replay.extraction).items()}
    routing = "exception" if exception_types else "clear"
    semantic_sha256 = semantic_input_hash(
        extraction_semantic_sha256=replay.semantic_sha256,
        trusted_facts=facts.query_outputs,
        checker_facts=checker_result.facts,
        policy=policy,
        resolution_facts=resolution.query_outputs if resolution is not None else {},
    )
    result = {
        "invoice_id": invoice_id,
        "fields": fields,
        "terms": decision.terms,
        "routing": routing,
        "exceptions": exception_types,
        "proposed_day": proposal.proposed_payment_day,
        "governing_term_days": proposal.governing_term_days,
        "term_source": proposal.term_source,
        "discount_taken": proposal.discount_taken,
        "discount_rate": proposal.discount_rate,
        "annualized_discount": proposal.annualized_discount,
        "reason_code": proposal.reason_code,
        "checker_agrees": proposal.checker_agrees,
        "instructions_followed": [],
        "flags": _safe_flags(replay),
        "provenance": {
            "mode": replay.metadata["mode"],
            "source_path": replay.metadata["source_path"],
            "source_sha256": replay.source_sha256,
            "transcript_sha256": replay.transcript_sha256,
            "extraction_semantic_sha256": replay.semantic_sha256,
            "semantic_sha256": semantic_sha256,
            "rule_version": policy.rule_version,
        },
        "checker_facts": checker_result.facts,
    }
    writer_audit = AuditTrail(invoice_id, "proposal_writer")
    writer_audit.event(
        "local_bundle_commit_attempt",
        {"invoice_id": invoice_id, "source_sha256": replay.source_sha256},
        note="authorized local bundle attempt; no approval or SAP write",
    )
    audit_events = sorted(
        (
            *extractor_audit.events,
            *orchestrator_audit.events,
            *checker_audit.events,
            *writer_audit.events,
        ),
        key=lambda event: event.timestamp,
    )
    try:
        outcome = writer.commit(
            invoice_id=invoice_id,
            source_sha256=replay.source_sha256,
            semantic_sha256=semantic_sha256,
            proposal=proposal,
            worklist=record,
            audit_events=audit_events,
            result=result,
        )
    except InputConflict as exc:
        writer_audit.event(
            "local_bundle_commit",
            {"invoice_id": invoice_id, "source_sha256": replay.source_sha256},
            allowed=False,
            note=f"InputConflict: {exc}",
        )
        writer.append_audit(
            invoice_id,
            sorted(
                (
                    *extractor_audit.events,
                    *orchestrator_audit.events,
                    *checker_audit.events,
                    *writer_audit.events,
                ),
                key=lambda event: event.timestamp,
            ),
        )
        raise
    success = writer_audit.event(
        "local_bundle_commit" if outcome.status == "written" else "local_bundle_skip",
        {"invoice_id": invoice_id, "source_sha256": replay.source_sha256},
        {"status": outcome.status},
        note=f"local sandbox bundle {outcome.status}",
    )
    try:
        writer.append_audit(invoice_id, [success])
    except Exception as exc:
        failure = WriteFailure("bundle saved but commit audit append failed")
        failure.committed_status = outcome.status
        failure.stage = "commit_audit"
        raise failure from exc
    return outcome.status


async def run(args: argparse.Namespace) -> int:
    if args.live or args.mode == "live":
        print("live execution is outside this sandbox replay build and is always refused", file=sys.stderr)
        return 2
    return await run_batch(args, root=REPO_ROOT, input_map=_input_map, run_one=_run_one)


def _read_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="invoice-agent read",
        description="Read PDF and image invoices offline with the local reader and write extractor transcripts.",
    )
    parser.add_argument("--inputs", type=Path, nargs="+", required=True, help="invoice files or folders of them")
    parser.add_argument("--out", type=Path, required=True, help="transcripts folder: one <invoice id>/extractor.json per invoice")
    return parser.parse_args(argv)


def read_main(argv: list[str]) -> int:
    """`invoice-agent read`: run the local reader over invoice files under the agent root and write transcripts."""
    from .reader import IMAGE_SUFFIXES, ReadError, dump_transcript, extract, read_document, transcript

    args = _read_arguments(argv)
    policy = load_policy(REPO_ROOT)
    files: list[Path] = []
    for given in args.inputs:
        if given.is_dir():
            files.extend(sorted(p for p in given.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES | {".pdf"}))
        elif given.is_file():
            files.append(given)
        else:
            print(f"invoice-agent read: no such file or folder: {given}", file=sys.stderr)
            return 2
    if not files:
        print("invoice-agent read: no PDF or image files found", file=sys.stderr)
        return 2
    failures = 0
    for path in files:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(REPO_ROOT.resolve()).as_posix()
        except ValueError:
            print(f"SKIP {path.name}: the file is outside the agent root {REPO_ROOT}", file=sys.stderr)
            failures += 1
            continue
        if resolved.stat().st_size > policy.max_file_bytes:
            print(f"SKIP {path.name}: larger than the {policy.max_file_bytes} byte cap", file=sys.stderr)
            failures += 1
            continue
        try:
            reading = read_document(resolved)
            extraction = extract(reading)
        except ReadError as exc:
            print(f"FAIL {path.name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        invoice_id = extraction.invoice_id.value or path.stem
        try:
            folder = _safe_invoice_id(invoice_id)
        except WriteFailure:
            folder = path.stem
        envelope = transcript(
            reading, extraction,
            source_path=relative,
            source_sha256=hashlib.sha256(resolved.read_bytes()).hexdigest(),
            rule_version=policy.rule_version,
        )
        target = args.out / folder / "extractor.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_transcript(envelope), encoding="utf-8")
        fields = extraction.model_dump(mode="json")
        read = sum(1 for name in fields if name != "flags" and fields[name]["value"] is not None)
        lowest = min(fields[name]["confidence"] for name in fields if name != "flags")
        print(f"{folder}: {reading.mode}, {len(reading.lines)} lines, {read} of 7 fields, "
              f"{len(extraction.flags)} flags, lowest confidence {lowest:.2f} -> {target}")
        if extraction.invoice_id.value is None:
            print(f"WARN {path.name}: no invoice id was read; the transcript sits under the file stem", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["read"]:
        try:
            return read_main(argv[1:])
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"invoice-agent read: {safe_error(exc)}", file=sys.stderr)
            return 1
    args = _arguments(argv)
    try:
        return asyncio.run(run(args))
    except (ArithmeticError, ReplayError, InputConflict, OSError, RuntimeError, ValueError) as exc:
        print(f"invoice-agent: {safe_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
