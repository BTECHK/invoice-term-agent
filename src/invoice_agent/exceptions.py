"""Deterministic fact gathering and offline exception recommendations."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import REPO_ROOT
from .erp_client import ErpClient
from .decide import Decision, InvoiceFacts, PoFacts, ValidatedTerm
from .guard import mask_account_numbers
from .models import Extraction, EvidenceItem
from .policy import Policy
from .rules import normalize_po_reference, parse_amount, parse_printed_term


class CapExceeded(RuntimeError):
    """A role exceeded a policy step, cost, or reflection cap."""


@dataclass
class RunBudget:
    max_steps: int
    max_cost_usd: float
    max_reflection_cycles: int = 3
    steps: int = 0
    cost_usd: float = 0
    reflection_cycles: int = 0

    def step(self, *, cost_usd: float = 0) -> None:
        self.steps += 1
        self.cost_usd += cost_usd
        if self.steps > self.max_steps:
            raise CapExceeded(f"step cap {self.max_steps} exceeded")
        if self.cost_usd > self.max_cost_usd:
            raise CapExceeded(f"cost cap {self.max_cost_usd:.2f} USD exceeded")

    def reflect(self) -> None:
        self.reflection_cycles += 1
        if self.reflection_cycles > self.max_reflection_cycles:
            raise CapExceeded(f"reflection cap {self.max_reflection_cycles} exceeded")


@dataclass(frozen=True)
class GatheredFacts:
    invoice: InvoiceFacts
    po: PoFacts | None
    validated: tuple[ValidatedTerm, ...]
    query_outputs: dict[str, dict[str, Any]]
    vendor: str | None
    company_code: str | None


@dataclass(frozen=True)
class Resolution:
    recommendation: str
    evidence: tuple[EvidenceItem, ...]
    query_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)


def _one(body: dict[str, Any]) -> dict[str, Any] | None:
    rows = body.get("rows") or []
    return rows[0] if rows else None


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _trusted_term_days(value: Any, source: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    message = f"{source} must be an integer from 0 to 365, got {value!r}"
    try:
        days = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if (
        isinstance(value, bool)
        or not days.is_finite()
        or days != days.to_integral_value()
        or not 0 <= days <= 365
    ):
        raise ValueError(message)
    return int(days)


async def gather_facts(
    extraction: Extraction,
    client: ErpClient,
    *,
    allowed_queries: set[str] | None = None,
    budget: RunBudget | None = None,
    min_confidence: float = 0.80,
) -> GatheredFacts:
    """Read the minimum fixed-query facts needed for a deterministic decision."""
    outputs: dict[str, dict[str, Any]] = {}
    amount, currency = parse_amount(extraction.invoice_value.value)
    printed = parse_printed_term(extraction.payment_terms.value, extraction.payment_terms.quote)
    unreadable = tuple(
        name
        for name in (
            "vendor_name",
            "vendor_address",
            "payment_terms",
            "invoice_value",
            "company_code",
            "po_reference",
            "invoice_id",
        )
        if (field := getattr(extraction, name)).value is None or field.confidence < min_confidence
    )
    invoice_id = extraction.invoice_id.value or ""
    reference = normalize_po_reference(extraction.po_reference.value)
    invoice = InvoiceFacts(
        invoice_id=invoice_id,
        po_reference=reference,
        vendor_name=extraction.vendor_name.value,
        company_code=extraction.company_code.value,
        value=amount,
        currency=currency,
        printed=printed,
        terms_page=extraction.payment_terms.page,
        terms_quote=extraction.payment_terms.quote,
        unreadable=unreadable,
        flags=tuple((flag.type, flag.quote, flag.page) for flag in extraction.flags),
    )
    if not invoice_id or "invoice_id" in unreadable:
        raise ValueError("invoice_id is unreadable; no record can be written")
    if reference is None:
        return GatheredFacts(invoice, None, (), outputs, None, None)

    async def query(query_id: str, variables: dict[str, str]) -> dict[str, Any]:
        if budget:
            budget.step()
        body = await client.run_query(query_id, variables, allowed_queries=allowed_queries)
        outputs[query_id] = body
        return body

    header = _one(await query("po_terms", {"po_reference": reference}))
    activity = _one(await query("po_activity_state", {"po_reference": reference}))
    if header is None:
        return GatheredFacts(invoice, None, (), outputs, None, None)
    vendor = str(header["vendor"]) if header.get("vendor") is not None else None
    company = str(header["company_code"]) if header.get("company_code") is not None else None
    vendor_name = None
    if vendor:
        vendor_name = (_one(await query("vendor_record", {"vendor": vendor})) or {}).get("vendor_name")
    validated: list[ValidatedTerm] = []
    if vendor and company:
        rows = (await query("validated_terms", {"vendor": vendor, "company_code": company})).get("rows") or []
        for row in rows:
            validated.append(
                ValidatedTerm(
                    days=_trusted_term_days(row.get("term_days"), "validated_terms term_days"),
                    discount_pct=float(row["discount_percent"]) if row.get("discount_percent") is not None else None,
                    discount_days=int(row["discount_days"]) if row.get("discount_days") is not None else None,
                    express_agreement=bool(row.get("express_agreement")),
                )
            )
    created = _as_date((activity or {}).get("created_at"))
    received = _as_date((activity or {}).get("invoice_received_at"))
    po = PoFacts(
        found=True,
        vendor=vendor,
        vendor_name=str(vendor_name) if vendor_name is not None else None,
        company_code=company,
        po_term_days=_trusted_term_days(header.get("po_term_days"), "po_terms po_term_days", optional=True),
        master_term_days=_trusted_term_days(header.get("master_term_days"), "po_terms master_term_days", optional=True),
        value=Decimal(str(header["net_order_value"])) if header.get("net_order_value") is not None else None,
        baseline_date=received,
        invoice_before_po=bool(created and received and received < created),
        goods_receipt=int((activity or {}).get("goods_receipt_count") or 0) > 0,
        blocked_without_release=(
            int((activity or {}).get("block_count") or 0) > 0
            and int((activity or {}).get("release_count") or 0) == 0
        ),
    )
    return GatheredFacts(invoice, po, tuple(validated), outputs, vendor, company)


def _skill_queries(path: Path) -> tuple[str, ...]:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^\s*queries:\s*["\']([^"\']*)["\']\s*$', text, re.M)
    if match is None:
        raise ValueError(f"skill has no fixed query metadata: {path}")
    return tuple(item.strip() for item in match.group(1).split(",") if item.strip())


async def resolve_exception(
    decision: Decision,
    facts: GatheredFacts,
    client: ErpClient,
    *,
    skill_root: Path = REPO_ROOT / "skills",
    policy: Policy,
    budget: RunBudget | None = None,
) -> Resolution:
    """Run only the primary skill's declared fixed queries and draft plain text."""
    if not decision.exception_types:
        raise ValueError("a clear decision has no exception skill")
    primary = decision.exception_types[0]
    skill_path = Path(skill_root) / primary / "SKILL.md"
    if not skill_path.is_file():
        raise ValueError(f"missing skill: {primary}")
    allowed = _skill_queries(skill_path)
    variables = {
        "invoice_id": facts.invoice.invoice_id,
        "po_reference": facts.invoice.po_reference,
        "vendor": facts.vendor,
        "company_code": facts.company_code,
    }
    outputs: dict[str, dict[str, Any]] = {}
    declared = client._queries  # adapter owns declaration validation; no query text reaches this module
    for query_id in allowed:
        spec = declared.get(query_id)
        if spec is None:
            raise ValueError(f"skill {primary} names undeclared query {query_id}")
        needed = list(spec.get("variables") or [])
        if any(variables.get(name) is None for name in needed):
            continue
        if budget:
            budget.step()
        outputs[query_id] = await client.run_query(
            query_id,
            {name: str(variables[name]) for name in needed},
            allowed_queries=allowed,
        )
    trigger = mask_account_numbers(decision.triggers[primary])
    terms = decision.terms
    action = (
        "Hold. No proposal or payment day. Verify by phone on the number already on file."
        if primary == "supplier-or-remittance-mismatch"
        else "Keep this invoice in the worklist until the named exception is resolved by a person."
    )
    recommendation = (
        f"Finding: {trigger}. Terms: printed {terms['printed']}, PO {terms['po']}, "
        f"vendor master {terms['master']}. Action: {action} "
        f"Money impact: {decision.money_amount} {decision.currency}."
    )
    words = recommendation.split()
    if len(words) > 120:
        recommendation = " ".join(words[:120])
    evidence = list(decision.evidence)
    for query_id, body in outputs.items():
        evidence.append(EvidenceItem(source="erp", query_id=query_id, value=f"{body['row_count']} row(s)"))
    return Resolution(recommendation, tuple(evidence[:40]), outputs)
