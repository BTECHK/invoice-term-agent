"""Compose the rules into one decision: clear or exception, governing term, discount, reason code.

Pure: facts in, a Decision out. Every figure in the evidence comes from here, not from a model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import rules
from .guard import mask_account_numbers
from .models import EvidenceItem
from .policy import Policy

SKILL_CODE = {
    "supplier-or-remittance-mismatch": "SUPPLIER_OR_REMITTANCE_MISMATCH",
    "missing-or-wrong-po": "MISSING_OR_WRONG_PO",
    "po-not-approved-or-receipted": "PO_NOT_APPROVED_OR_RECEIPTED",
    "over-60-days": "OVER_60_DAYS",
    "no-validated-term": "NO_VALIDATED_TERM",
    "discount-eligibility": "DISCOUNT_ELIGIBILITY",
    "value-outside-tolerance": "VALUE_OUTSIDE_TOLERANCE",
}


@dataclass(frozen=True)
class InvoiceFacts:
    invoice_id: str
    po_reference: str | None
    vendor_name: str | None
    company_code: str | None
    value: Decimal | None
    currency: str | None
    printed: rules.ParsedTerm | None
    terms_page: int | None = None
    terms_quote: str | None = None
    unreadable: tuple[str, ...] = ()
    flags: tuple[tuple[str, str, int | None], ...] = ()


@dataclass(frozen=True)
class PoFacts:
    found: bool
    vendor: str | None = None
    vendor_name: str | None = None
    company_code: str | None = None
    po_term_days: int | None = None
    master_term_days: int | None = None
    value: Decimal | None = None
    baseline_date: date | None = None
    invoice_before_po: bool = False
    goods_receipt: bool = True
    blocked_without_release: bool = False
    sap_payment_block: str | None = None


@dataclass(frozen=True)
class ValidatedTerm:
    days: int
    discount_pct: float | None = None
    discount_days: int | None = None
    express_agreement: bool = False


@dataclass
class Decision:
    route: str
    exception_types: list[str]
    triggers: dict[str, str]
    reason_code: str
    hold: bool
    governing_term_days: int | None
    term_source: str | None
    discount_rate: float | None
    annualized_discount: float | None
    discount_taken: bool
    proposed_day: int | None
    draft_day: int | None
    baseline_date: date | None
    candidate_dates: dict[str, date]
    terms: dict[str, int | None]
    evidence: list[EvidenceItem]
    money_amount: Decimal
    currency: str


@dataclass
class _Work:
    pol: Policy
    triggers: dict[str, str] = field(default_factory=dict)
    evidence: list[EvidenceItem] = field(default_factory=list)
    hold: bool = False
    gov: int | None = None
    source: str | None = None
    clear_codes: list[str] = field(default_factory=list)
    rate: float | None = None
    annualized: float | None = None
    taken: bool = False
    taken_day: int | None = None
    conditional_day: int | None = None

    def trigger(self, skill: str, note: str, hold: bool = False) -> None:
        self.triggers.setdefault(skill, note)
        self.hold = self.hold or hold

    def ev(self, source: str, query_id: str, value: str | float | int | bool | None) -> None:
        self.evidence.append(EvidenceItem(source=source, query_id=query_id, value=value))  # type: ignore[arg-type]


def _checks_on_fields(inv: InvoiceFacts, w: _Work) -> None:
    for kind, quote, page in inv.flags:
        w.trigger("supplier-or-remittance-mismatch", f"flag {kind}", hold=True)
        safe_quote = mask_account_numbers(quote)
        w.ev("invoice", "flags", f'{kind} (page {page}): "{safe_quote}". Quoted, not followed.')
    for name in inv.unreadable:
        route = w.pol.unreadable_routes.get(name, "no-validated-term")
        w.trigger(route, f"{name} unreadable", hold=True)
        w.ev("rules", "unreadable_field_routes", f"{name} is empty or below confidence {w.pol.min_confidence}")


def _legal_forms_differ(invoice_name: str | None, record_name: str | None) -> bool:
    """Both names carry a legal form and the forms are not the same (a missing form is not a difference)."""
    invoice_form, record_form = rules.legal_form(invoice_name), rules.legal_form(record_name)
    return invoice_form is not None and record_form is not None and invoice_form != record_form


def _checks_on_po(inv: InvoiceFacts, po: PoFacts, w: _Work) -> None:
    if po.invoice_before_po:
        w.trigger("missing-or-wrong-po", "invoice before PO")
        w.ev("rules", "data_rules.invoice_before_po", "invoice received before the PO was created; route to the buyer")
    if po.blocked_without_release:
        note = "control exception: blocked PO with goods receipt and no logged release" if po.goods_receipt \
            else "blocked PO, no release logged"
        w.trigger("po-not-approved-or-receipted", note)
        w.ev("rules", "data_rules.blocked_po_with_receipt_and_no_release", note)
    elif not po.goods_receipt:
        w.trigger("po-not-approved-or-receipted", "no goods receipt")
        w.ev("rules", "data_rules.blocked_po_with_receipt_and_no_release", "no goods receipt at invoice date")
    if po.sap_payment_block:
        w.trigger("po-not-approved-or-receipted", "SAP payment block")
        w.ev("erp", "po_activity_state", f"SAP payment block {po.sap_payment_block}")
    names = {rules.vendor_key(po.vendor), rules.vendor_key(po.vendor_name)} - {""}
    if inv.vendor_name and rules.vendor_key(inv.vendor_name) not in names:
        w.trigger("supplier-or-remittance-mismatch", "vendor differs from the PO header")
        w.ev("invoice", "vendor_name", f"invoice names {inv.vendor_name}")
        w.ev("erp", "po_terms", f"PO header vendor {po.vendor}")
    elif _legal_forms_differ(inv.vendor_name, po.vendor_name):
        w.trigger("supplier-or-remittance-mismatch", "legal form differs from the supplier record")
        w.ev("invoice", "vendor_name", f"invoice names {inv.vendor_name}")
        w.ev("erp", "vendor_record", f"supplier record names {po.vendor_name}")
    if inv.company_code and po.company_code and not rules.company_code_agrees(inv.company_code, po.company_code):
        w.trigger("supplier-or-remittance-mismatch", "company code differs from the PO header")
        w.ev("invoice", "company_code", f"invoice names {inv.company_code}")
        w.ev("erp", "po_terms", f"PO header company code {po.company_code}")
    if "supplier-or-remittance-mismatch" in w.triggers:
        w.ev("erp", "vendor_record", "address, bank and remit-to details: not available in the data model")
    if inv.value is not None and po.value is not None:
        if inv.currency != w.pol.po_currency:
            note = "currency is missing" if inv.currency is None else "currency differs"
            w.trigger("value-outside-tolerance", note)
            printed = inv.currency or "no currency"
            w.ev("invoice", "invoice_value", f"printed in {printed}; PO value is in {w.pol.po_currency}")
            return
        gap = rules.total_gap(inv.value, po.value, w.pol.total_variance_pct)
        if gap.exceeds:
            w.trigger("value-outside-tolerance", "gap above the total tolerance")
            w.ev("rules", "tolerances.total_variance_pct",
                 f"invoice {inv.value} vs PO {po.value}: gap {gap.amount} ({gap.pct:.2%}); limit {gap.limit}")


def _checks_on_terms(inv: InvoiceFacts, po: PoFacts, validated: list[ValidatedTerm], w: _Work) -> None:
    pol, cap = w.pol, w.pol.max_days_without_agreement
    printed = inv.printed.net_days if inv.printed else None
    o, m = po.po_term_days, po.master_term_days
    source_days = [d for d in (printed, o, m) if d is not None]
    vdays = [v.days for v in validated]
    longest_candidate = max((*source_days, *vdays), default=None)
    covering = [v for v in validated if v.days == longest_candidate]
    confirmed = {v.days for v in validated if v.express_agreement}
    agreed = None not in (printed, o, m) and printed == o == m
    covered_longest = longest_candidate is not None and bool(covering)
    w.ev("rules", "longest_validated_term",
         f"printed {printed}, PO {o}, vendor master {m}, validated {vdays or 'none on file'}")
    if agreed or covered_longest:
        w.gov = longest_candidate if covered_longest else printed
        w.source = "validated" if covered_longest else "po"
        w.clear_codes.append(
            "VALIDATED_LONGER_TERM" if covered_longest and w.gov != printed else "AGREED_TERM"
        )
    else:
        system = [d for d in (o, m) if d is not None]
        w.gov = max(system) if system else None
        w.source = "po" if w.gov is not None and w.gov == o else "vendor_master"
        if "payment_terms" not in inv.unreadable:
            w.trigger("no-validated-term", "terms differ and none is validated", hold=printed is None)
        if o is not None and m is not None and o < m:
            w.ev("rules", "data_rules.weaker_po_term_needs_approval",
                 f"PO term {o} is shorter than the vendor master term {m}: approval required")
    over = [d for d in (*source_days, *vdays) if rules.needs_agreement(d, confirmed, cap)]
    if over:
        w.trigger("over-60-days", f"{max(over)} days without a confirmed express agreement")
        w.ev("rules", "max_term_days_without_express_agreement",
             f"{max(over)} days is over {cap} and no express agreement is confirmed")
    _checks_on_discount(inv, po, validated, w)


def _checks_on_discount(inv: InvoiceFacts, po: PoFacts, validated: list[ValidatedTerm], w: _Work) -> None:
    pol, p = w.pol, inv.printed
    if p is None or p.discount_pct is None or p.discount_days is None:
        return
    w.rate = p.discount_pct / 100
    if p.net_days is not None:
        w.annualized = rules.annualized_discount(p.discount_pct, p.discount_days, p.net_days, pol.day_count_basis)
    beats = w.annualized is not None and w.annualized > pol.cost_of_capital
    w.ev("rules", "discount_rule",
         f"{p.discount_pct:g}/{p.discount_days} net {p.net_days}: annualized "
         f"{'n/a' if w.annualized is None else f'{w.annualized:.1%}'} against cost of capital {pol.cost_of_capital:.1%} (assumed, Q-1)")
    if {inv.vendor_name, po.vendor} & pol.never_accelerate:
        w.clear_codes.append("NEVER_ACCELERATE_VENDOR")
        w.ev("rules", "vendor_exceptions", "vendor is on the never accelerate list: no discount day, no earlier day")
        return
    eligible = w.gov == p.net_days and any(
        v.days == p.net_days and v.discount_pct == p.discount_pct and v.discount_days == p.discount_days
        for v in validated
    )
    base = w.gov
    day_ok = base is not None and base > p.discount_days
    if not eligible:
        w.trigger("discount-eligibility", "discount printed on the invoice only")
        if beats and day_ok:
            w.conditional_day = p.discount_days
    elif beats and day_ok:
        w.taken, w.taken_day = True, p.discount_days
        w.clear_codes.insert(0, "DISCOUNT_TAKEN")
    else:
        w.clear_codes.insert(0, "DISCOUNT_DECLINED")


def _money(primary: str | None, inv: InvoiceFacts, po: PoFacts | None, w: _Work) -> Decimal:
    value = inv.value or Decimal(0)
    pol = w.pol
    known = [d for d in ((inv.printed.net_days if inv.printed else None),
                         po.po_term_days if po else None, po.master_term_days if po else None) if d is not None]
    if primary == "value-outside-tolerance" and po and po.value is not None and inv.value is not None:
        return rules.money(inv.value - po.value)
    if primary == "over-60-days":
        return rules.timing_value(value, pol.cost_of_capital, max(known, default=0) - pol.max_days_without_agreement,
                                  pol.day_count_basis)
    if primary == "no-validated-term" and known:
        return rules.timing_value(value, pol.cost_of_capital, max(known) - min(known), pol.day_count_basis)
    if primary == "discount-eligibility" and inv.printed and inv.printed.discount_pct is not None and w.rate:
        early = (inv.printed.net_days or 0) - (inv.printed.discount_days or 0)
        paid = value * Decimal(str(1 - w.rate))
        return rules.money(value * Decimal(str(w.rate)) - rules.timing_value(paid, pol.cost_of_capital, early, pol.day_count_basis))
    return rules.money(value)


def decide(inv: InvoiceFacts, po: PoFacts | None, validated: list[ValidatedTerm], pol: Policy) -> Decision:
    """Apply the rules in the order of policy/term-rules.yaml and return one decision."""
    w = _Work(pol)
    _checks_on_fields(inv, w)
    if po is None or not po.found:
        w.trigger("missing-or-wrong-po", "no PO found for the reference", hold=True)
        w.ev("erp", "po_terms", f"no row for PO reference {inv.po_reference}")
    else:
        _checks_on_po(inv, po, w)
        if inv.printed is not None or "payment_terms" in inv.unreadable:
            _checks_on_terms(inv, po, validated, w)
        else:
            w.trigger("no-validated-term", "printed term could not be parsed", hold=True)
            w.ev("invoice", "payment_terms", f'could not parse "{inv.terms_quote}"; not guessed')
    order = {s: i for i, s in enumerate(pol.precedence)}
    types = sorted(w.triggers, key=lambda s: order.get(s, 99))
    baseline = po.baseline_date if po else None
    terms = {"printed": inv.printed.net_days if inv.printed else None,
             "po": po.po_term_days if po else None, "master": po.master_term_days if po else None}
    dates = {}
    if baseline:
        dates = {k: rules.due_after(baseline, v) for k, v in terms.items() if v is not None}
        if w.rate and inv.printed and inv.printed.discount_days is not None:
            dates["discount"] = rules.due_after(baseline, inv.printed.discount_days)
    base = w.gov
    clear = not types
    day = w.taken_day if w.taken_day is not None else base
    draft = None if (w.hold or base is None) else (w.conditional_day if w.conditional_day is not None else day)
    clear_code = next((c for c in ("DISCOUNT_TAKEN", "NEVER_ACCELERATE_VENDOR", "DISCOUNT_DECLINED", "VALIDATED_LONGER_TERM",
                                   "AGREED_TERM") if c in w.clear_codes), "AGREED_TERM")
    return Decision(
        route="clear" if clear else "exception", exception_types=types, triggers={s: w.triggers[s] for s in types},
        reason_code=clear_code if clear else SKILL_CODE[types[0]], hold=w.hold,
        governing_term_days=base if not w.hold else None, term_source=w.source if not w.hold else None,
        discount_rate=w.rate, annualized_discount=w.annualized, discount_taken=bool(clear and w.taken),
        proposed_day=day if clear else None, draft_day=draft, baseline_date=baseline,
        candidate_dates=dates, terms=terms, evidence=w.evidence,
        money_amount=_money(types[0] if types else None, inv, po, w), currency=inv.currency or pol.po_currency)
