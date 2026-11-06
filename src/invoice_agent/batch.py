"""Batch lifecycle: isolate bad invoices, retain completed work, and report partial runs."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import AuditTrail
from .erp_client import ErpClient
from .exceptions import CapExceeded
from .extract import ReplayError, load_replay
from .policy import load_policy
from .worklist import BundleWriter, InputConflict, WriteFailure, _safe_invoice_id

LOG = logging.getLogger(__name__)


class UnsafeInvoiceName(ReplayError):
    """A transcript directory name that cannot be used as a record path."""


PER_INVOICE = (ReplayError, ValueError, ArithmeticError, CapExceeded, InputConflict)


def safe_error(error: BaseException) -> str:
    """Return operational diagnostics without interpolating untrusted payloads or secrets."""
    message = str(error)
    # These prefixes are fixed in trusted-term validation; omit the supplied value.
    for prefix in ("validated_terms term_days", "po_terms po_term_days"):
        if message.startswith(prefix + " must be an integer"):
            return prefix + " must be an integer from 0 to 365"
    if isinstance(error, CapExceeded):
        return "cap"
    if isinstance(error, InputConflict):
        return "InputConflict: saved input differs; earlier bundle retained"
    if isinstance(error, UnsafeInvoiceName):
        return "transcript directory name is not a safe invoice id; rename the folder"
    if isinstance(error, ReplayError):
        return "replay validation failed; repair the source or transcript"
    return type(error).__name__ + ": operation failed; inspect the local configuration"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def recorded_id(invoice_id: str) -> str:
    """The id a record is kept under. An unsafe directory name is replaced, with a hash so two never collide."""
    try:
        return _safe_invoice_id(invoice_id)
    except WriteFailure:
        digest = hashlib.sha256(invoice_id.encode("utf-8", "surrogatepass")).hexdigest()[:8]
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", invoice_id).lstrip("._-")[:40]
        return f"unsafe-{stem or 'unnamed'}-{digest}"


async def run_batch(args: Any, *, root: Path, input_map: Any, run_one: Any) -> int:
    started = _now()
    writer = None
    records: list[dict[str, Any]] = []
    fatal = None
    stage = "output"
    try:
        writer = BundleWriter(args.output)
        writer.recover()
        stage = "input_selection"
        inputs = input_map(args)
        records = [{"invoice_id": recorded_id(invoice_id), "status": "not_processed", "reason": "pending"}
                   for invoice_id, _ in inputs]
        stage = "policy"
        policy = load_policy(root)
        stage = "client_open"
        supplier_counts: Counter[str] = Counter()
        async with ErpClient.stdio(root=root, fixtures_dir=args.fixtures,
                                      calls_per_minute=policy.calls_per_minute,
                                      max_wait_seconds=policy.max_wait_seconds) as client:
            for index, ((invoice_id, source), record) in enumerate(zip(inputs, records)):
                began = time.monotonic()
                stage = "preflight"
                try:
                    if index >= policy.max_invoices_per_run:
                        raise CapExceeded("per-run invoice cap exceeded")
                    if isinstance(source, ReplayError):
                        raise source
                    if recorded_id(invoice_id) != invoice_id:
                        raise UnsafeInvoiceName("transcript directory name is not a safe invoice id")
                    replay = load_replay(invoice_id, args.transcripts, root=root,
                                         source_path=source, policy=policy)
                    # A claimed name is a workload bucket, never authenticated identity.
                    supplier = " ".join((replay.extraction.vendor_name.value or "").casefold().split())
                    supplier_counts[supplier] += 1
                    if supplier_counts[supplier] > policy.max_invoices_per_supplier_per_run:
                        raise CapExceeded("per-supplier invoice cap exceeded")
                    stage = "processing"
                    status = await run_one(invoice_id, source, args=args, writer=writer,
                                           client=client, policy=policy, replay=replay)
                    record.update(status=status, reason="completed", stage="commit")
                    stage = "quarantine_cleanup"
                    writer.clear_quarantine(invoice_id)
                except PER_INVOICE as exc:
                    failure_stage = getattr(exc, "stage", stage)
                    message = safe_error(exc)
                    kept = record["invoice_id"]
                    audit = AuditTrail(kept, "extractor" if stage == "preflight" else "orchestrator")
                    audit.event("invoice_quarantine", {"invoice_id": kept},
                                allowed=False, note=f"{type(exc).__name__}: {message}")
                    writer.append_audit(kept, audit.events)
                    writer.write_record(f"quarantine/{kept}.json", {
                        "invoice_id": kept, "stage": failure_stage,
                        "error_class": type(exc).__name__, "message": message,
                        "retryable": False, "timestamp": _now(),
                    })
                    record.update(status="quarantined", reason=message, stage=failure_stage)
                except Exception as exc:
                    committed = getattr(exc, "committed_status", None)
                    if committed in ("written", "skipped"):
                        record["status"] = committed
                    if record["status"] in ("written", "skipped"):
                        record.update(reason="saved; run-level failure", stage=getattr(exc, "stage", stage))
                    raise
                finally:
                    if record["status"] == "not_processed":
                        record["reason"] = "run-level failure"
                    record["duration_ms"] = round((time.monotonic() - began) * 1000, 2)
                    # IDs are metadata only, with a fixed alphabet to prevent log injection.
                    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", invoice_id)[:64]
                    LOG.info("invoice_id=%s stage=%s outcome=%s duration_ms=%.2f reason=%s",
                             safe_id, record.get("stage", stage), record["status"],
                             record["duration_ms"], record["reason"])
            stage = "client_close"
    except Exception as exc:
        fatal = {"stage": stage, "error_class": type(exc).__name__, "message": safe_error(exc)}
        LOG.error("run stopped stage=%s error=%s", stage, type(exc).__name__)
        if not records and stage == "output":
            try:
                records = [{"invoice_id": recorded_id(invoice_id), "status": "not_processed", "reason": "pending"}
                           for invoice_id, _ in input_map(args)]
            except Exception:
                pass  # An unreadable inventory cannot be fabricated.
    finally:
        for record in records:
            if record["status"] == "not_processed":
                record.update(reason="run-level failure", stage=stage)
                if "duration_ms" not in record:
                    LOG.info("invoice_id=%s stage=%s outcome=not_processed duration_ms=0",
                             re.sub(r"[^A-Za-z0-9._-]", "_", record["invoice_id"])[:64], stage)
        counts = {status: sum(r["status"] == status for r in records)
                  for status in ("written", "skipped", "quarantined", "not_processed")}
        summary = {"started": started, "ended": _now(), "counts": counts,
                   "invoices": records, "run_error": fatal}
        if writer is not None:
            try:
                writer.finalize_results()
            except Exception as exc:
                fatal = {"stage": "finalize", "error_class": type(exc).__name__}
                summary["run_error"] = fatal
            try:
                writer.write_record("run-summary.json", summary)
            except Exception as exc:
                fatal = {"stage": "summary", "error_class": type(exc).__name__}
                summary["run_error"] = fatal
                print("run-summary unavailable on disk: " + json.dumps(summary), file=sys.stderr)
        else:
            print("run-summary unavailable on disk: " + json.dumps(summary), file=sys.stderr)
    if fatal is not None:
        return 1
    print(f"sandbox replay complete: {counts['written']} written, {counts['skipped']} skipped, "
          f"{counts['quarantined']} quarantined; results {args.output / 'results.jsonl'}")
    return 3 if counts["quarantined"] else 0
