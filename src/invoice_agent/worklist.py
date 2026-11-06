"""Durable single-writer bundle commit with idempotency and crash recovery."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from .guard import contains_iban
from .models import AuditEvent, Proposal, WorklistRecord


class InputConflict(RuntimeError):
    """An invoice identifier was already committed with different input hashes."""


class WriteFailure(RuntimeError):
    """A bundle could not be recovered or committed safely."""


@dataclass(frozen=True)
class WriteOutcome:
    status: str
    invoice_id: str
    paths: tuple[Path, ...]


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        + "\n"
    ).encode("utf-8")


def _semantic_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _semantic_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _semantic_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        items = [_semantic_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, (list, tuple)):
        return [_semantic_value(item) for item in value]
    return value


def semantic_input_hash(
    *,
    extraction_semantic_sha256: str,
    trusted_facts: Mapping[str, Any],
    checker_facts: Mapping[str, Any],
    policy: Any,
    resolution_facts: Mapping[str, Any] | None = None,
) -> str:
    """Hash every semantic input that can change a local proposal or worklist record."""
    payload = {
        "extraction_semantic_sha256": extraction_semantic_sha256,
        "trusted_facts": dict(trusted_facts),
        "checker_facts": dict(checker_facts),
        "resolution_facts": dict(resolution_facts or {}),
        # Scheduling limits do not change payment decisions or invalidate saved bundles.
        "policy": {key: value for key, value in _semantic_value(policy).items()
                   if key not in {"max_wait_seconds", "max_invoices_per_run",
                                  "max_invoices_per_supplier_per_run"}},
    }
    canonical = json.dumps(
        _semantic_value(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item)


def _safe_invoice_id(invoice_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", invoice_id) is None:
        raise WriteFailure("invoice_id is unsafe for a local record path")
    return invoice_id


def _write_fsync(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class BundleWriter:
    """Serialize all local output and publish one invoice bundle atomically enough to recover."""

    MANAGED_DIRS = (
        "proposals",
        "worklist",
        "audit",
        "results",
        "quarantine",
        ".state",
        ".transactions",
        ".locks",
    )

    def __init__(self, output_dir: Path, *, fault_after: str | None = None) -> None:
        requested = Path(output_dir).expanduser()
        requested = requested.absolute()
        if any(path.is_symlink() for path in (requested, *requested.parents)):
            raise WriteFailure("output path must not contain a symlink")
        if fault_after not in (None, "journal"):
            raise ValueError("fault_after must be None or 'journal'")
        self.fault_after = fault_after
        requested.mkdir(parents=True, exist_ok=True)
        self.output_dir = requested.resolve()
        for name in self.MANAGED_DIRS:
            path = self.output_dir / name
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise WriteFailure(f"managed output path {name} must be a real directory")
            path.mkdir(exist_ok=True)
            self._managed_path(name)
        self.lock_path = self._managed_path(".locks", "writer.lock")
        if self.lock_path.exists() and not self.lock_path.is_file():
            raise WriteFailure("writer lock must be a regular file")
        self.lock_path.touch(exist_ok=True)

    def _managed_path(self, *parts: str) -> Path:
        relative = Path(*parts)
        if relative.is_absolute() or ".." in relative.parts:
            raise WriteFailure("managed output path is not relative")
        candidate = self.output_dir
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                raise WriteFailure(f"managed output path contains a symlink: {relative}")
        try:
            candidate.resolve(strict=False).relative_to(self.output_dir)
        except ValueError as exc:
            raise WriteFailure("managed output path escapes the output directory") from exc
        return candidate

    @staticmethod
    def _staged_path(staging: Path, relative: str) -> Path:
        item = Path(relative)
        if item.is_absolute() or ".." in item.parts:
            raise WriteFailure("staged path is not relative")
        candidate = staging
        for part in item.parts:
            candidate /= part
            if candidate.is_symlink():
                raise WriteFailure(f"staged path contains a symlink: {relative}")
        try:
            candidate.resolve(strict=False).relative_to(staging)
        except ValueError as exc:
            raise WriteFailure("staged path escapes its transaction") from exc
        return candidate

    def _state_path(self, safe: str) -> Path:
        return self._managed_path(".state", f"{safe}.json")

    def _journal_path(self, safe: str) -> Path:
        return self._managed_path(".transactions", f"{safe}.json")

    def _validate_layout(self) -> None:
        for name in self.MANAGED_DIRS:
            path = self._managed_path(name)
            if not path.is_dir():
                raise WriteFailure(f"managed output path {name} is missing or is not a directory")
        lock_path = self._managed_path(".locks", "writer.lock")
        if not lock_path.is_file():
            raise WriteFailure("writer lock is missing or is not a regular file")

    def _with_lock(self):
        self._validate_layout()
        lock_path = self._managed_path(".locks", "writer.lock")
        handle = lock_path.open("r+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _recover_locked(self, safe: str) -> None:
        journal_path = self._journal_path(safe)
        if not journal_path.is_file():
            return
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            staging_raw = Path(journal["staging"])
            if staging_raw.is_symlink():
                raise WriteFailure("transaction staging directory must not be a symlink")
            staging = staging_raw.resolve()
            staging.relative_to(self._managed_path(".transactions").resolve())
            if not staging.is_dir():
                raise WriteFailure("transaction staging directory is missing")
            files = journal["files"]
            state = journal["state"]
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise WriteFailure(f"unreadable recovery journal for {safe}") from exc
        for relative in files:
            if not isinstance(relative, str):
                raise WriteFailure("recovery file path is malformed")
            target = self._managed_path(relative)
            source = self._staged_path(staging, relative)
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                _fsync_dir(target.parent)
            elif not target.is_file():
                raise WriteFailure(f"recovery is missing both staged and final {relative}")
        state_path = self._state_path(safe)
        tmp_state = self._managed_path(".state", f"{safe}.tmp")
        _write_fsync(tmp_state, _json_bytes(state))
        os.replace(tmp_state, state_path)
        _fsync_dir(state_path.parent)
        journal_path.unlink()
        shutil.rmtree(staging, ignore_errors=True)

    def recover(self) -> None:
        """Complete every durable journal before accepting more work."""
        lock = self._with_lock()
        try:
            for journal in sorted(self._managed_path(".transactions").glob("*.json")):
                self._recover_locked(journal.stem)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def _append_audit(self, safe: str, events: Sequence[AuditEvent]) -> None:
        if not events:
            return
        path = self._managed_path("audit", f"{safe}.jsonl")
        with path.open("ab") as handle:
            for event in events:
                handle.write(_json_bytes(event.model_dump(mode="json")))
            handle.flush()
            os.fsync(handle.fileno())

    def append_audit(self, invoice_id: str, events: Sequence[AuditEvent]) -> None:
        """Durably retain failure-path audit events even when no bundle can be committed."""
        safe = _safe_invoice_id(invoice_id)
        lock = self._with_lock()
        try:
            self._append_audit(safe, events)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def commit(
        self,
        *,
        invoice_id: str,
        source_sha256: str,
        semantic_sha256: str,
        proposal: Proposal,
        worklist: WorklistRecord | None,
        audit_events: Sequence[AuditEvent],
        result: Mapping[str, Any],
    ) -> WriteOutcome:
        """Commit once, skip identical input, and reject changed input under the same ID."""
        safe = _safe_invoice_id(invoice_id)
        if proposal.invoice_id != invoice_id or (worklist and worklist.invoice_id != invoice_id):
            raise WriteFailure("bundle invoice identifiers disagree")
        if worklist and (
            worklist.status != "open" or worklist.decided_by is not None or worklist.decided_at is not None
        ):
            raise WriteFailure("the offline writer cannot record approvals or decisions")
        # Last line of defence behind the masking in the runner. The message never repeats the value.
        documents = {"proposal": proposal.model_dump(mode="json"),
                     "worklist record": worklist.model_dump(mode="json") if worklist else None,
                     "result": dict(result)}
        for name, document in documents.items():
            if any(contains_iban(text) for text in _strings(document)):
                raise WriteFailure(f"the {name} holds an unmasked IBAN")
        state_value = {
            "invoice_id": invoice_id,
            "source_sha256": source_sha256,
            "semantic_sha256": semantic_sha256,
        }
        lock = self._with_lock()
        try:
            self._recover_locked(safe)
            state_path = self._state_path(safe)
            if state_path.is_file():
                current = json.loads(state_path.read_text(encoding="utf-8"))
                if current != state_value:
                    raise InputConflict(
                        f"invoice {invoice_id} already exists with different source or semantic input"
                    )
                self._append_audit(safe, audit_events)
                return WriteOutcome("skipped", invoice_id, (state_path,))
            staging = Path(
                tempfile.mkdtemp(prefix=f"{safe}-", dir=self._managed_path(".transactions"))
            ).resolve()
            audit_path = self._managed_path("audit", f"{safe}.jsonl")
            files: dict[str, bytes] = {
                f"proposals/{safe}.json": _json_bytes(proposal.model_dump(mode="json")),
                f"audit/{safe}.jsonl": (
                    audit_path.read_bytes() if audit_path.is_file() else b""
                ) + b"".join(_json_bytes(event.model_dump(mode="json")) for event in audit_events),
                f"results/{safe}.json": _json_bytes(dict(result)),
            }
            if worklist is not None:
                files[f"worklist/{safe}.json"] = _json_bytes(worklist.model_dump(mode="json"))
            for relative, data in files.items():
                _write_fsync(self._staged_path(staging, relative), data)
            journal = {
                "invoice_id": invoice_id,
                "staging": str(staging),
                "files": sorted(files),
                "state": state_value,
            }
            journal_path = self._journal_path(safe)
            tmp_journal = self._managed_path(".transactions", f"{safe}.tmp")
            _write_fsync(tmp_journal, _json_bytes(journal))
            os.replace(tmp_journal, journal_path)
            _fsync_dir(journal_path.parent)
            if self.fault_after == "journal":
                raise WriteFailure("injected interruption after durable journal")
            self._recover_locked(safe)
            paths = tuple(self._managed_path(relative) for relative in sorted(files))
            return WriteOutcome("written", invoice_id, paths)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def write_record(self, relative: str, value: Mapping[str, Any]) -> None:
        """Atomically replace a run summary or quarantine record through the writer lock."""
        if relative != "run-summary.json":
            path = Path(relative)
            if path.parent != Path("quarantine") or path.suffix != ".json":
                raise WriteFailure("unsupported operational record")
            _safe_invoice_id(path.stem)
        lock = self._with_lock()
        try:
            target = self._managed_path(relative)
            temp = self._managed_path(relative + ".tmp")
            _write_fsync(temp, _json_bytes(dict(value)))
            os.replace(temp, target)
            _fsync_dir(target.parent)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def clear_quarantine(self, invoice_id: str) -> None:
        lock = self._with_lock()
        try:
            path = self._managed_path("quarantine", _safe_invoice_id(invoice_id) + ".json")
            path.unlink(missing_ok=True)
            _fsync_dir(path.parent)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def finalize_results(self) -> Path:
        """Rebuild the aggregate JSONL deterministically from committed per-invoice results."""
        lock = self._with_lock()
        try:
            target = self._managed_path("results.jsonl")
            temp = self._managed_path("results.tmp")
            data = b"".join(
                path.read_bytes()
                for path in sorted(self._managed_path("results").glob("*.json"))
            )
            _write_fsync(temp, data)
            os.replace(temp, target)
            _fsync_dir(self.output_dir)
            return target
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
