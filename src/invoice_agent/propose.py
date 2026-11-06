"""Proposal construction and a separate deterministic fact re-query checker."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .erp_client import ErpClient
from .decide import Decision, InvoiceFacts
from .models import CLEAR_REASON_CODES, Proposal
from .policy import Policy
from .rules import LEGAL_FORMS, name_tokens

_ROUTE_CODES = {
    "supplier-or-remittance-mismatch": "SUPPLIER_OR_REMITTANCE_MISMATCH",
    "missing-or-wrong-po": "MISSING_OR_WRONG_PO",
    "po-not-approved-or-receipted": "PO_NOT_APPROVED_OR_RECEIPTED",
    "over-60-days": "OVER_60_DAYS",
    "no-validated-term": "NO_VALIDATED_TERM",
    "discount-eligibility": "DISCOUNT_ELIGIBILITY",
    "value-outside-tolerance": "VALUE_OUTSIDE_TOLERANCE",
}


@dataclass(frozen=True)
class CheckerResult:
    agrees: bool
    expected_day: int | None
    proposed_day: int | None
    facts: dict[str, Any]
    reason: str


def build_proposal(
    decision: Decision,
    *,
    invoice_id: str,
    po_reference: str | None,
    rule_version: str,
    checker_agrees: bool | None,
) -> Proposal:
    """Build a strict proposal. Only a checked clear decision may carry a day."""
    checked_clear = decision.route == "clear" and checker_agrees is True
    return Proposal(
        invoice_id=invoice_id,
        po_reference=po_reference,
        governing_term_days=decision.governing_term_days,
        term_source=decision.term_source,
        proposed_payment_day=decision.proposed_day if checked_clear else None,
        discount_taken=bool(decision.discount_taken and checked_clear),
        discount_rate=decision.discount_rate,
        annualized_discount=decision.annualized_discount,
        reason_code=decision.reason_code,
        rule_version=rule_version,
        checker_agrees=checker_agrees,
    )


def _term(text: Any) -> tuple[int | None, float | None, int | None]:
    """Independent, deliberately small parser for checker input."""
    value = str(text or "")
    net_match = re.search(r"\bnet\s*(\d{1,3})|\b(\d{1,3})\s*days?\b", value, re.I)
    net = int(next(x for x in net_match.groups() if x is not None)) if net_match else None
    discount = re.search(r"(\d+(?:\.\d+)?)\s*(?:/|%\s*)(\d{1,3})\b", value)
    return (
        net,
        float(discount.group(1)) if discount else None,
        int(discount.group(2)) if discount else None,
    )


def _amount(value: Any) -> Decimal | None:
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    digits = re.sub(r"[^\d.,-]", "", str(value or ""))
    if not digits:
        return None
    if digits.rfind(",") > digits.rfind("."):
        digits = digits.replace(".", "").replace(",", ".")
    else:
        digits = digits.replace(",", "")
    try:
        return Decimal(digits)
    except InvalidOperation:
        return None


def _vendor_key(value: Any) -> str:
    tokens = name_tokens(str(value or ""))
    while len(tokens) > 1 and tokens[-1] in LEGAL_FORMS:
        tokens.pop()
    return "".join(tokens)


def _legal_form(value: Any) -> str | None:
    """The legal form a name ends with. Tokens come from the shared normalizer in rules, the lookup is this function's own."""
    tokens = name_tokens(str(value or ""))
    return LEGAL_FORMS.get(tokens[-1]) if len(tokens) > 1 else None


def _company_agrees(invoice_code: Any, header_code: Any) -> bool:
    left = re.sub(r"\D", "", str(invoice_code or ""))
    right = re.sub(r"\D", "", str(header_code or ""))
    return bool(left and right and right in (left, left + "000"))


def _input_exception_codes(
    inv: InvoiceFacts,
    policy: Policy,
    reasons: list[str],
) -> set[str]:
    codes = {"SUPPLIER_OR_REMITTANCE_MISMATCH"} if inv.flags else set()
    for field in inv.unreadable:
        route = policy.unreadable_routes.get(field, "no-validated-term")
        code = _ROUTE_CODES.get(route)
        if code is None:
            reasons.append(f"hard-stop unreadable field {field} reached the checker")
        else:
            codes.add(code)
    return codes


def _missing_po_result(
    proposal: Proposal,
    facts: dict[str, Any],
    reasons: list[str],
    detail: str,
    applicable: set[str],
) -> CheckerResult:
    facts.update({
        "po_term_days": None,
        "master_term_days": None,
        "po_net_order_value": None,
        "validated_terms": [],
        "expected_governing_term_days": None,
        "expected_term_source": None,
        "expected_discount_taken": False,
        "expected_discount_rate": None,
        "expected_annualized_discount": None,
        "applicable_reason_codes": sorted(applicable),
        "expected_day": None,
        "observed_day": proposal.proposed_payment_day,
    })
    if proposal.reason_code not in applicable:
        reasons.append("missing PO reason code differs from independently queried facts")
    if proposal.governing_term_days is not None or proposal.term_source is not None:
        reasons.append("missing PO proposal carries an unsupported governing term")
    if proposal.proposed_payment_day is not None or proposal.discount_taken:
        reasons.append("missing PO proposal carries an action")
    if proposal.discount_rate is not None or proposal.annualized_discount is not None:
        reasons.append("missing PO proposal carries an unsupported discount")
    return CheckerResult(
        agrees=not reasons,
        expected_day=None,
        proposed_day=proposal.proposed_payment_day,
        facts=facts,
        reason="; ".join(reasons) or detail,
    )


async def check_proposal(
    inv: InvoiceFacts,
    proposal: Proposal,
    client: ErpClient,
    policy: Policy,
) -> CheckerResult:
    """Re-query trusted rows and recompute without calling the orchestrator's decision code."""
    facts: dict[str, Any] = {"po_reference": proposal.po_reference}
    reasons: list[str] = []
    input_applicable = _input_exception_codes(inv, policy, reasons)
    extracted_body = await client.run_query(
        "extracted_invoice", {"invoice_id": proposal.invoice_id}
    )
    extracted_rows = extracted_body["rows"]
    if len(extracted_rows) != 1:
        return CheckerResult(False, None, proposal.proposed_payment_day, facts, "extracted invoice row missing")
    extracted = extracted_rows[0]
    source_term = _term(extracted.get("payment_terms"))
    source_amount = _amount(extracted.get("invoice_value"))
    facts.update({
        "source_payment_terms": extracted.get("payment_terms"),
        "source_invoice_value": float(source_amount) if source_amount is not None else None,
        "source_printed_days": source_term[0],
    })
    if inv.value != source_amount:
        reasons.append("invoice amount differs from the independently queried extraction")
    printed_days = inv.printed.net_days if inv.printed is not None else None
    if printed_days != source_term[0]:
        reasons.append("printed term differs from the independently queried extraction")
    if inv.po_reference != proposal.po_reference or (
        proposal.po_reference is not None
        and extracted.get("po_reference") != proposal.po_reference
    ):
        reasons.append("PO reference differs from the independently queried extraction")
    if proposal.po_reference is None:
        return _missing_po_result(
            proposal,
            facts,
            reasons,
            "missing PO independently confirmed",
            input_applicable | {"MISSING_OR_WRONG_PO"},
        )
    po_rows = (await client.run_query(
        "po_terms", {"po_reference": proposal.po_reference}
    ))["rows"]
    if len(po_rows) != 1:
        return _missing_po_result(
            proposal,
            facts,
            reasons,
            "missing PO row independently confirmed",
            input_applicable | {"MISSING_OR_WRONG_PO"},
        )
    po = po_rows[0]
    vendor, company = str(po["vendor"]), str(po["company_code"])
    validated = (await client.run_query(
        "validated_terms", {"vendor": vendor, "company_code": company}
    ))["rows"]
    activity_rows = (await client.run_query(
        "po_activity_state", {"po_reference": proposal.po_reference}
    ))["rows"]
    activity = activity_rows[0] if len(activity_rows) == 1 else {}
    vendor_rows = (await client.run_query("vendor_record", {"vendor": vendor}))["rows"]
    vendor_record = vendor_rows[0] if len(vendor_rows) == 1 else {}
    printed, po_days, master_days = source_term[0], po.get("po_term_days"), po.get("master_term_days")
    validated_days = [int(row["term_days"]) for row in validated if row.get("term_days") is not None]
    candidates = [int(x) for x in (printed, po_days, master_days, *validated_days) if x is not None]
    longest = max(candidates, default=None)
    covering = [row for row in validated if row.get("term_days") == longest]
    agreed = printed is not None and printed == po_days == master_days
    confirmed_days = {
        int(row["term_days"])
        for row in validated
        if row.get("term_days") is not None and bool(row.get("express_agreement"))
    }
    term_clear = bool(agreed or covering)
    over_cap = any(
        days > policy.max_days_without_agreement and days not in confirmed_days
        for days in candidates
    )
    expected_source = "validated" if covering else ("po" if agreed else None)
    applicable: set[str] = set(input_applicable)
    if not term_clear:
        applicable.add("NO_VALIDATED_TERM")
    if over_cap:
        applicable.add("OVER_60_DAYS")
    names = {_vendor_key(vendor), _vendor_key(vendor_record.get("vendor_name"))} - {""}
    invoice_form = _legal_form(extracted.get("vendor_name"))
    record_form = _legal_form(vendor_record.get("vendor_name"))
    forms_differ = invoice_form is not None and record_form is not None and invoice_form != record_form
    if inv.flags or forms_differ or _vendor_key(extracted.get("vendor_name")) not in names or not _company_agrees(
        extracted.get("company_code"), company
    ):
        applicable.add("SUPPLIER_OR_REMITTANCE_MISMATCH")
    po_amount = _amount(po.get("net_order_value"))
    if (
        inv.currency != policy.po_currency
        or source_amount is None
        or po_amount is None
        or (po_amount != 0 and abs(source_amount - po_amount) > abs(po_amount) * Decimal(str(policy.total_variance_pct)))
    ):
        applicable.add("VALUE_OUTSIDE_TOLERANCE")
    if (
        int(activity.get("goods_receipt_count") or 0) == 0
        or (
            int(activity.get("block_count") or 0) > 0
            and int(activity.get("release_count") or 0) == 0
        )
    ):
        applicable.add("PO_NOT_APPROVED_OR_RECEIPTED")
    if activity.get("invoice_received_at") and activity.get("created_at") and activity["invoice_received_at"] < activity["created_at"]:
        applicable.add("MISSING_OR_WRONG_PO")
    expected_governing = longest if term_clear else max(
        (int(x) for x in (po_days, master_days) if x is not None), default=None
    )
    if not term_clear:
        expected_source = (
            "po" if expected_governing is not None and expected_governing == po_days
            else "vendor_master" if expected_governing is not None
            else None
        )
    if inv.flags or inv.unreadable:
        expected_governing = None
        expected_source = None
    expected_day = expected_governing if not applicable else None
    discount_taken = False
    expected_rate = source_term[1] / 100 if source_term[1] is not None else None
    expected_annualized = None
    if source_term[1] is not None and source_term[2] is not None and source_term[0] is not None:
        pct, discount_days = source_term[1], source_term[2]
        eligible = any(
            row.get("term_days") == printed == expected_governing
            and row.get("discount_percent") == pct
            and row.get("discount_days") == discount_days
            for row in validated
        )
        span = (printed or 0) - discount_days
        expected_annualized = (
            (pct / 100) / (1 - pct / 100) * policy.day_count_basis / span if span > 0 else None
        )
        if not eligible:
            applicable.add("DISCOUNT_ELIGIBILITY")
            expected_day = None
        if (
            not applicable
            and vendor not in policy.never_accelerate
            and eligible
            and expected_annualized is not None
            and expected_annualized > policy.cost_of_capital
        ):
            expected_day = discount_days
            discount_taken = True
    if applicable:
        expected_day = None
        discount_taken = False
    facts.update({
        "po_term_days": po_days,
        "master_term_days": master_days,
        "po_net_order_value": po.get("net_order_value"),
        "validated_terms": validated,
        "expected_governing_term_days": expected_governing,
        "expected_term_source": expected_source,
        "expected_discount_taken": discount_taken,
        "expected_discount_rate": expected_rate,
        "expected_annualized_discount": expected_annualized,
        "applicable_reason_codes": sorted(applicable),
        "expected_day": expected_day,
        "observed_day": proposal.proposed_payment_day,
    })
    if applicable and proposal.reason_code not in applicable:
        reasons.append("reason code is not supported by independently queried facts")
    if not applicable and proposal.reason_code not in CLEAR_REASON_CODES:
        reasons.append("exception reason code is not supported by independently queried facts")
    if proposal.governing_term_days != expected_governing:
        reasons.append("governing term differs from independently queried facts")
    if proposal.term_source != expected_source:
        reasons.append("term source differs from independently queried facts")
    if proposal.proposed_payment_day != expected_day:
        reasons.append("payment day differs from independent recomputation")
    if proposal.discount_taken != discount_taken:
        reasons.append("discount decision differs from independent recomputation")
    if proposal.discount_rate != expected_rate:
        reasons.append("discount rate differs from the independently queried source")
    if (
        proposal.annualized_discount is None
        if expected_annualized is not None
        else proposal.annualized_discount is not None
    ):
        reasons.append("annualized discount presence differs from independent recomputation")
    elif expected_annualized is not None and proposal.annualized_discount is not None:
        if abs(proposal.annualized_discount - expected_annualized) > 1e-9:
            reasons.append("annualized discount differs from independent recomputation")
    return CheckerResult(
        agrees=not reasons,
        expected_day=expected_day,
        proposed_day=proposal.proposed_payment_day,
        facts=facts,
        reason="; ".join(reasons) or "independent facts and day agree",
    )


def apply_checker(proposal: Proposal, result: CheckerResult) -> Proposal:
    """Return the final strict proposal, clearing action fields on disagreement."""
    data = proposal.model_dump(mode="json")
    data["checker_agrees"] = result.agrees
    if not result.agrees:
        data["proposed_payment_day"] = None
        data["discount_taken"] = False
        if proposal.reason_code in CLEAR_REASON_CODES:
            data["reason_code"] = "NO_VALIDATED_TERM"
    return Proposal.model_validate(data)
