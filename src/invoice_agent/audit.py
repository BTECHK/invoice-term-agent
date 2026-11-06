"""Schema-bound audit events containing only redacted canonical hashes."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from .guard import mask_account_numbers
from .models import AuditEvent

_SENSITIVE_KEYS = frozenset({
    "authorization",
    "password",
    "secret",
    "token",
    "api_key",
    "bank_account",
    "iban",
    "card_number",
})


def _redact(value: Any, key: str | None = None) -> Any:
    if key and key.lower() in _SENSITIVE_KEYS:
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return mask_account_numbers(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(
        _redact(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    """Hash canonical JSON after the mandatory recursive redaction pass."""
    return hashlib.sha256(_canonical(value)).hexdigest()


def redacted_hash(value: Any) -> str:
    return sha256_json(value)


class AuditTrail:
    """Build ordered in-memory events for one role and invoice."""

    def __init__(
        self,
        invoice_id: str,
        role: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.invoice_id = invoice_id
        self.role = role
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.events: list[AuditEvent] = []
        self._last: datetime | None = None

    def event(
        self,
        tool: str,
        input_value: Any,
        output_value: Any = None,
        *,
        allowed: bool = True,
        note: str = "ok",
    ) -> AuditEvent:
        timestamp = self.clock()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp = timestamp.astimezone(timezone.utc)
        if self._last is not None and timestamp <= self._last:
            timestamp = self._last + timedelta(microseconds=1)
        self._last = timestamp
        event = AuditEvent(
            event_id=str(uuid4()),
            invoice_id=self.invoice_id,
            role=self.role,  # type: ignore[arg-type]
            tool=tool,
            allowed=allowed,
            input_hash=redacted_hash(input_value),
            output_hash=redacted_hash(output_value) if allowed and output_value is not None else None,
            timestamp=timestamp,
            note=mask_account_numbers(note)[:500],
        )
        self.events.append(event)
        return event
