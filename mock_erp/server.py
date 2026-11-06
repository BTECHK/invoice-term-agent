"""Mock ERP data service over stdio.

Nine tools: five reads answered from fixtures, four refused. The refused four are the three write
tools and Call Assistant Model. Run Query takes a query id declared in integration/agent-tools.yaml
and its declared variables, nothing else.

    python3 mock_erp/server.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp.server import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from invoice_agent import miniyaml  # noqa: E402

FIXTURES = Path(os.environ.get("MOCK_ERP_FIXTURES", ROOT / "mock_erp" / "fixtures"))
CONFIG = Path(os.environ.get("MOCK_ERP_CONFIG", ROOT / "integration" / "agent-tools.yaml"))

server = MCPServer("mock-erp-data-service", log_level="WARNING",
                   instructions="Mock of the ERP data service. Read tools only; writes are refused.")


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _config() -> dict[str, Any]:
    if not CONFIG.is_file():
        raise ToolError(f"tool contract not found: {CONFIG.name}")
    return miniyaml.load(CONFIG.read_text(encoding="utf-8"))


def _po(ref: str) -> dict[str, Any] | None:
    return next((r for r in _fixture("purchase_orders.json")["records"] if r["po_line_key"] == ref), None)


def _timeline(po: dict[str, Any]) -> list[dict[str, Any]]:
    steps = [("po_line_created", po["created_at"]), ("goods_receipt_posted", po["goods_receipt_last_at"]),
             ("invoice_received", po["invoice_received_at"]), ("invoice_cleared", po["cleared_at"]),
             ("po_blocked", po["block_last_at"]), ("po_released", po["release_last_at"])]
    return sorted(({"event": e, "event_time": t} for e, t in steps if t), key=lambda row: row["event_time"])


def _rows(query_id: str, v: dict[str, str]) -> list[dict[str, Any]]:
    pos = _fixture("purchase_orders.json")["records"]
    if query_id in ("po_terms", "po_activity_state"):
        po = _po(v["po_reference"])
        return [po] if po else []
    if query_id == "extracted_invoice":
        return [r for r in _fixture("extracted_invoices.json")["records"] if r["invoice_id"] == v["invoice_id"]]
    if query_id == "vendor_term_history":
        groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for p in pos:
            if p["vendor"] == v["vendor"] and p["company_code"] == v["company_code"]:
                groups.setdefault((p["po_term_days"], p["master_term_days"]), []).append(p)
        return [{"po_term_days": k[0], "master_term_days": k[1], "line_count": len(g),
                 "net_order_value": round(sum(p["net_order_value"] for p in g), 2),
                 "avg_days_invoice_to_payment": round(sum(p["days_invoice_to_clearing"] for p in g) / len(g), 1)}
                for k, g in groups.items()]
    if query_id == "validated_terms":
        return [r for r in _fixture("validated_terms.json")["records"]
                if r["vendor"] == v["vendor"] and r["company_code"] == v["company_code"]]
    if query_id == "past_decisions":
        return [r for r in _fixture("past_decisions.json")["records"] if r.get("vendor") == v["vendor"]]
    vendors = [r for r in _fixture("vendors.json")["records"] if r["vendor"] == v["vendor"]]
    if query_id == "vendor_delivery_profile":
        return [r for r in vendors if r["late_share"] is not None]
    if query_id == "vendor_record":
        return vendors
    raise ToolError(f"query {query_id!r} has no fixture")


@server.tool(name="run_query", title="Run Query",
             description="Run one fixed query by id. Fill only its declared variables.")
def run_query(query: str, variables: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = _config()
    spec = next((q for q in cfg["queries"] if q["id"] == query), None)
    if spec is None:
        raise ToolError(f"Refused: {query!r} is not a declared query. Run Query runs fixed queries only (ADR-002).")
    given = variables or {}
    declared = list(spec["variables"])
    if set(given) != set(declared):
        raise ToolError(f"Refused: query {query} takes exactly {declared}, got {sorted(given)}.")
    for name, value in given.items():
        pattern = cfg["variable_types"][name]["pattern"]
        if not re.fullmatch(pattern, str(value)):
            raise ToolError(f"Refused: variable {name} fails its pattern.")
    columns = list(spec["returns"])
    rows = [{c: row.get(c) for c in columns} for row in _rows(query, given)][: int(spec["max_rows"])]
    return {"query": query, "columns": columns, "row_count": len(rows), "rows": rows}


@server.tool(name="get_data_catalog", title="Get Data Catalog",
             description="Attribute and KPI definitions of the data catalog, and the declared queries.")
def get_data_catalog() -> dict[str, Any]:
    catalog = _fixture("data_catalog.json")
    catalog["queries"] = [{"id": q["id"], "variables": q["variables"], "returns": q["returns"]}
                          for q in _config()["queries"]]
    return catalog


@server.tool(name="search_records", title="Search Records",
             description="Find a vendor or PO line by text. The search text is capped at 80 characters.")
def search_records(query: str, limit: int = 3) -> dict[str, Any]:
    if len(query) > 80:
        raise ToolError("Refused: search text is longer than 80 characters.")
    needle = re.sub(r"[^a-z0-9]", "", query.lower())
    if not needle:
        return {"query": query, "results": []}
    hits: list[dict[str, Any]] = []
    for p in _fixture("purchase_orders.json")["records"]:
        if needle in re.sub(r"[^a-z0-9]", "", (p["vendor"] + p["po_line_key"]).lower()):
            hits.append({"type": "po_line", "po_line_key": p["po_line_key"], "vendor": p["vendor"],
                         "company_code": p["company_code"], "net_order_value": p["net_order_value"]})
    return {"query": query, "results": hits[: max(1, min(limit, 3))]}


@server.tool(name="get_kpis", title="Get KPIs", description="Framed KPIs such as payment timing and term alignment.")
def get_kpis(topic: str) -> dict[str, Any]:
    topics = _fixture("kpis.json")["topics"]
    if topic not in topics:
        raise ToolError(f"Refused: unknown kpi {topic!r}. Known: {sorted(topics)}.")
    return {"topic": topic, "values": topics[topic]}


@server.tool(name="get_event_timeline", title="Get Event Timeline",
             description="Event timeline of one PO line.")
def get_event_timeline(po_reference: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]{10}", po_reference):
        raise ToolError("Refused: po_reference must be the 10 digit purchasing document number.")
    po = _po(po_reference)
    return {"po_reference": po_reference, "events": _timeline(po) if po else []}


def _refuse(name: str, why: str) -> None:
    raise ToolError(f"Refused: {name} {why}")


_WRITE = "is a write tool. The model never holds it (ADR-003); writes run through the approval workflow after a person approves."


@server.tool(name="trigger_erp_action", title="Trigger ERP Action", description="Write tool. Always refused here.")
def trigger_erp_action(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _refuse("Trigger ERP Action", _WRITE)
    return {}


@server.tool(name="start_workflow", title="Start Workflow", description="Write tool. Always refused here.")
def start_workflow(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _refuse("Start Workflow", _WRITE)
    return {}


@server.tool(name="resume_workflow", title="Resume Workflow",
             description="Write tool. Always refused here.")
def resume_workflow(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _refuse("Resume Workflow", _WRITE)
    return {}


@server.tool(name="call_assistant_model", title="Call Assistant Model", description="Off in the pilot. Always refused.")
def call_assistant_model(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _refuse("Call Assistant Model", "starts a second model with its own tools and is off in the pilot (policy/tool-policy.yaml).")
    return {}


if __name__ == "__main__":
    server.run()
