"""Pydantic models for the four documents the agent exchanges.

Field names follow schemas/*.json. Every model forbids extra keys.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EXCEPTION_SKILLS = (
    "no-validated-term",
    "over-60-days",
    "missing-or-wrong-po",
    "po-not-approved-or-receipted",
    "supplier-or-remittance-mismatch",
    "discount-eligibility",
    "value-outside-tolerance",
)
ExceptionSkill = Literal[
    "no-validated-term",
    "over-60-days",
    "missing-or-wrong-po",
    "po-not-approved-or-receipted",
    "supplier-or-remittance-mismatch",
    "discount-eligibility",
    "value-outside-tolerance",
]
FlagType = Literal[
    "instruction_in_document",
    "bank_or_remit_to_change",
    "urgent_payment_request",
    "other_money_or_credential_ask",
]
REASON_CODES = (
    "AGREED_TERM",
    "VALIDATED_LONGER_TERM",
    "DISCOUNT_TAKEN",
    "DISCOUNT_DECLINED",
    "NEVER_ACCELERATE_VENDOR",
    "NO_VALIDATED_TERM",
    "OVER_60_DAYS",
    "MISSING_OR_WRONG_PO",
    "PO_NOT_APPROVED_OR_RECEIPTED",
    "SUPPLIER_OR_REMITTANCE_MISMATCH",
    "DISCOUNT_ELIGIBILITY",
    "VALUE_OUTSIDE_TOLERANCE",
)
CLEAR_REASON_CODES = REASON_CODES[:5]
SHA256 = r"^[0-9a-f]{64}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldValue(Strict):
    """One extracted field as printed, with where it was found."""

    value: str | None = Field(max_length=200)
    page: int | None = Field(ge=1)
    quote: str | None = Field(max_length=240)
    confidence: float = Field(ge=0, le=1)


class Flag(Strict):
    """Text in the document that tries to instruct the reader or asks for money changes."""

    type: FlagType
    quote: str = Field(max_length=240)
    page: int | None = Field(ge=1)


class Extraction(Strict):
    """The seven fields the reader returns for one invoice."""

    invoice_id: FieldValue
    vendor_name: FieldValue
    vendor_address: FieldValue
    payment_terms: FieldValue
    invoice_value: FieldValue
    company_code: FieldValue
    po_reference: FieldValue
    flags: list[Flag] = Field(default_factory=list, max_length=10)


FIELD_NAMES = (
    "vendor_name",
    "vendor_address",
    "payment_terms",
    "invoice_value",
    "company_code",
    "po_reference",
    "invoice_id",
)


class EvidenceItem(Strict):
    """One figure or quote the recommendation relies on, with its source."""

    source: Literal["erp", "invoice", "rules"]
    query_id: str = Field(min_length=1, max_length=80)
    value: str | float | int | bool | None


class Proposal(Strict):
    """One proposal per invoice. For an exception the payment day stays null."""

    invoice_id: str = Field(min_length=1, max_length=64)
    po_reference: str | None = Field(max_length=32)
    governing_term_days: int | None = Field(ge=0, le=365)
    term_source: Literal["printed", "po", "vendor_master", "validated"] | None
    proposed_payment_day: int | None = Field(ge=0, le=365)
    discount_taken: bool
    discount_rate: float | None = Field(ge=0, lt=1)
    annualized_discount: float | None = Field(ge=0)
    reason_code: str
    rule_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    checker_agrees: bool | None

    @field_validator("reason_code")
    @classmethod
    def _known_code(cls, v: str) -> str:
        if v not in REASON_CODES:
            raise ValueError(f"reason code {v!r} is not in the list")
        return v

    @model_validator(mode="after")
    def _exception_has_no_day(self) -> "Proposal":
        if self.reason_code not in CLEAR_REASON_CODES:
            if self.proposed_payment_day is not None or self.discount_taken:
                raise ValueError("an exception proposal has no payment day and takes no discount")
        if self.checker_agrees is not True:
            if self.proposed_payment_day is not None or self.discount_taken:
                raise ValueError("an unchecked or disagreed proposal has no payment day and takes no discount")
        if self.discount_taken and (self.discount_rate is None or self.proposed_payment_day is None):
            raise ValueError("a taken discount needs its rate and its day")
        return self


class MoneyImpact(Strict):
    amount: float
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class WorklistRecord(Strict):
    """One record per exception invoice, for the existing AP worklist."""

    invoice_id: str = Field(min_length=1, max_length=64)
    exception_types: list[ExceptionSkill] = Field(min_length=1)
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=40)
    recommendation: str = Field(min_length=1, max_length=1200)
    money_impact: MoneyImpact
    created_at: datetime
    status: Literal["open", "approved", "rejected"] = "open"
    decided_by: str | None = Field(default=None, max_length=64)
    decided_at: datetime | None = None

    @field_validator("exception_types")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("exception types must be unique")
        return v

    @model_validator(mode="after")
    def _open_has_no_decision(self) -> "WorklistRecord":
        if self.status == "open" and (self.decided_by or self.decided_at):
            raise ValueError("an open record has no decision")
        return self


class AuditEvent(Strict):
    """One tool call or step by one role for one invoice, allowed or denied."""

    event_id: str
    invoice_id: str = Field(min_length=1, max_length=64)
    role: Literal["extractor", "orchestrator", "checker", "proposal_writer", "person"]
    tool: str = Field(min_length=1, max_length=120)
    allowed: bool
    input_hash: str = Field(pattern=SHA256)
    output_hash: str | None = Field(pattern=SHA256)
    timestamp: datetime
    note: str = Field(max_length=500)

    @model_validator(mode="after")
    def _denied_has_no_output(self) -> "AuditEvent":
        if not self.allowed and self.output_hash is not None:
            raise ValueError("a denied call has no output hash")
        return self
