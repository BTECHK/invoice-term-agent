"""Policy values read from policy/*.yaml and integration/agent-tools.yaml, with safe defaults."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any

from . import REPO_ROOT, miniyaml

PRECEDENCE = (
    "supplier-or-remittance-mismatch",
    "missing-or-wrong-po",
    "po-not-approved-or-receipted",
    "over-60-days",
    "no-validated-term",
    "discount-eligibility",
    "value-outside-tolerance",
)
UNREADABLE_ROUTES = {
    "invoice_id": "hard_stop",
    "po_reference": "missing-or-wrong-po",
    "vendor_name": "supplier-or-remittance-mismatch",
    "vendor_address": "supplier-or-remittance-mismatch",
    "company_code": "supplier-or-remittance-mismatch",
    "payment_terms": "no-validated-term",
    "invoice_value": "value-outside-tolerance",
}


@dataclass(frozen=True)
class RoleCaps:
    max_steps: int
    max_cost_usd: float
    max_reflection_cycles: int = 3


@dataclass(frozen=True)
class Policy:
    """Everything the rules and the runner read from policy files."""

    rule_version: str = "1.0.0"
    cost_of_capital: float = 0.05
    max_days_without_agreement: int = 60
    day_count_basis: int = 365
    price_variance_pct: float = 0.02
    price_variance_abs: float = 25.0
    total_variance_pct: float = 0.01
    min_confidence: float = 0.80
    never_accelerate: frozenset[str] = frozenset({"V31204", "V58871", "V37729"})
    precedence: tuple[str, ...] = PRECEDENCE
    unreadable_routes: dict[str, str] = field(default_factory=lambda: dict(UNREADABLE_ROUTES))
    po_currency: str = "EUR"
    calls_per_minute: int = 400
    max_wait_seconds: float = 120
    max_invoices_per_run: int = 1000
    max_invoices_per_supplier_per_run: int = 100
    max_pages: int = 10
    max_file_bytes: int = 10_000_000
    caps: dict[str, RoleCaps] = field(default_factory=lambda: {
        "extractor": RoleCaps(3, 0.05),
        "orchestrator": RoleCaps(15, 0.50),
        "checker": RoleCaps(8, 0.25),
    })


def _read(path: Path) -> dict[str, Any]:
    return miniyaml.load(path.read_text(encoding="utf-8"))


def load_policy(root: Path = REPO_ROOT) -> Policy:
    """Read the policy files under `root`; any value that is missing keeps its default."""
    rules = _read(root / "policy" / "term-rules.yaml")
    tools = _read(root / "policy" / "tool-policy.yaml")
    service = _read(root / "integration" / "agent-tools.yaml").get("service", {})
    base = Policy()
    tol = rules.get("tolerances", {})
    caps = dict(base.caps)
    for role, spec in (tools.get("roles") or {}).items():
        if role in caps and isinstance(spec, dict):
            caps[role] = RoleCaps(int(spec.get("max_steps", caps[role].max_steps)),
                                  float(spec.get("max_cost_usd", caps[role].max_cost_usd)),
                                  int(spec.get("max_reflection_cycles", 3)))
    limits = tools.get("limits") or {}
    policy = Policy(
        rule_version=str(rules.get("version", base.rule_version)),
        cost_of_capital=float(rules.get("cost_of_capital", base.cost_of_capital)),
        max_days_without_agreement=int(rules.get("max_term_days_without_express_agreement", 60)),
        day_count_basis=int((rules.get("discount_rule") or {}).get("day_count_basis", 365)),
        price_variance_pct=float(tol.get("price_variance_pct", 2)) / 100,
        price_variance_abs=float(tol.get("price_variance_abs", 25)),
        total_variance_pct=float(tol.get("total_variance_pct", 1)) / 100,
        min_confidence=float(rules.get("min_field_confidence", base.min_confidence)),
        never_accelerate=frozenset((rules.get("vendor_exceptions") or {}).get("never_accelerate")
                                   or base.never_accelerate),
        precedence=tuple(rules.get("exception_precedence") or base.precedence),
        unreadable_routes=dict(rules.get("unreadable_field_routes") or base.unreadable_routes),
        calls_per_minute=int(limits.get("calls_per_minute", service.get("client_throttle_calls_per_minute", base.calls_per_minute))),
        max_wait_seconds=float(limits.get("max_wait_seconds", base.max_wait_seconds)),
        max_invoices_per_run=int(limits.get("max_invoices_per_run", base.max_invoices_per_run)),
        max_invoices_per_supplier_per_run=int(limits.get("max_invoices_per_supplier_per_run", base.max_invoices_per_supplier_per_run)),
        max_pages=int(limits.get("max_pages", base.max_pages)),
        max_file_bytes=int(limits.get("max_file_bytes", base.max_file_bytes)),
        caps=caps,
    )
    if not 0 < policy.calls_per_minute < 1000:
        raise ValueError("calls_per_minute must be positive and below the local client ceiling of 1000")
    if not math.isfinite(policy.max_wait_seconds) or policy.max_wait_seconds < 0:
        raise ValueError("maximum wait must be finite and nonnegative")
    if min(policy.max_invoices_per_run, policy.max_invoices_per_supplier_per_run,
           policy.max_file_bytes, policy.max_pages) <= 0:
        raise ValueError("input caps must be positive")
    return policy
