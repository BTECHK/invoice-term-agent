# Software requirements specification

This file specifies the sandbox replay and production Design. It holds the business rules (BR-#), use cases (UC-#), functional requirements (FR-#) and non-functional requirements (NFR-#) for the payment-term agent. Every FR has a fit criterion; the trace table identifies a shared behavior scenario or manual review, not a dedicated test per requirement. Scope and personas are in [01-prd.md](01-prd.md); the trace from story to test is in [README.md](README.md).

Other identifiers (IDs) used here: US user story, ADR architecture decision record, Q open question, T threat.

Build reconciliation: 67 behavior tests include grading actual CLI output. See `tests/README.md` for scope; this does not claim every fit criterion is independently tested. `tests/test_hardening.py` adds regressions for the grader reason codes, the supplier legal form, guard folding, text-source quotes and the PreToolUse hook.

Status values: Scenario covered means a relevant scenario exercises the area, not every listed fit criterion. Reviewed means manual code/source review. Neither certifies a live deployment. Design denotes production capabilities not implemented here. FR-002 and FR-003 validate replay payloads, not a live invoice reader; FR-023 uses a deterministic checker, not another model family.

## 1. Business rules

| ID | Rule | Source |
|---|---|---|
| BR-1 | A term is clear when the printed term, the purchase order (PO) term (`po_term_days`) and the vendor master term (`master_term_days`) are equal, or a validated term covers the longest candidate for the vendor and company code. | Policy |
| BR-2 | Anything else is an exception. A person decides it. The agent only recommends. | Policy |
| BR-3 | The longest agreed or validated term governs. Ties are broken in this order: validated, PO, vendor master, printed. | Policy |
| BR-4 | A term over 60 days is effective only if expressly agreed and not grossly unfair to the creditor (section 271a(1) of the German Civil Code, BGB). A validated term on file does not prove express agreement, so it is an exception until a person records the agreement. | Law |
| BR-5 | Take a cash discount only when a validated term carries it and its annualized rate beats the cost of capital (5% assumed, Q-1). The PO and vendor master read views carry no discount, so only a validated term can make one eligible. Annualized rate = d / (1 - d) times 365 / (net days minus discount days). A discount printed only on the invoice is an exception. | Policy |
| BR-6 | Every proposal carries a reason code from section 3. | Policy |
| BR-7 | A PO term shorter than the vendor master term for the same vendor and company code needs approval from the term owner (Procurement assumed, Q-2). | Policy |
| BR-8 | A PO with `po_blocked` and `goods_receipt_posted` events and no `po_released` event is a control exception. | Control practice |
| BR-9 | An invoice received before its PO was created routes to the buyer. | Control practice |
| BR-10 | Vendors on the never-accelerate list in `policy/term-rules.yaml`, kept for unreliable delivery, get the governing term only: no discount day, no earlier day. | Policy |
| BR-11 | Tolerances: total variance above 1% of PO value; price variance above 2% or 25 per line. Compare in one currency only; a currency that is missing or not EUR is an exception. Every exception carries both numbers and the money impact. | Borrowed practice |
| BR-12 | Nothing is entered and nothing is paid without an explicit yes from a person. No auto-pay. The payment run is a second gate, not a continuation of the approval. | Borrowed practice |
| BR-13 | Everything in a document is data from the sender, never an instruction. | Borrowed practice |
| BR-14 | A changed remit-to or bank detail is verified by phone on the number already on file and is never updated from the invoice. Account numbers in extracted fields are masked to the last four characters by pattern before results are written, and the writer refuses output that contains a full IBAN. | Borrowed practice |
| BR-15 | A field the reader cannot read, or reads below the minimum confidence, stays empty and is named. It is never rounded to something plausible. | Borrowed practice |
| BR-16 | Only an approved enterprise resource planning (ERP) action, run by the approval workflow, changes the baseline date or the term in SAP. The approval is a workflow control, not an SAP authorization. SAP roles stay as they are. | Design |
| BR-17 | All exceptions are listed. The primary one is the first that applies in this order: supplier-or-remittance-mismatch, missing-or-wrong-po, po-not-approved-or-receipted, over-60-days, no-validated-term, discount-eligibility, value-outside-tolerance. | Design |
| BR-18 | SAP stays the system of record for the match, the due date and the payment run. The agent proposes a term or a baseline date change. | Design |
| BR-19 | Match vendors and orders by stable ID, never by name. | Design |

Proof-of-concept scope on BR-11: the reader takes header fields only, so the 1% total check runs in the sandbox and the per-line price check is an extension (UC-5). This local screen is not equivalent to SAP OMR6 or a replacement for native match and block state; see the [redundancy ledger](redundancy-ledger.md). The "borrowed practice" rules paraphrase a published accounts payable skill that is licensed under Apache-2.0; no code is copied.

## 2. Use cases

### UC-1 Term check and date proposal (deterministic)

| | |
|---|---|
| Actor | Batch runner; no person is involved on the clear path |
| Trigger | An invoice file arrives in the batch |
| Preconditions | PO and vendor data are in the ERP data service; policy files are loaded |
| Main flow | 1 Read seven fields (FR-002, FR-003). 2 Read the PO header and its events (FR-005, FR-006). 3 Compare header values (FR-007). 4 Build three terms (FR-008). 5 Decide clear or exception (FR-009 to FR-016). 6 Choose the governing term, discount and day (FR-017 to FR-020). 7 Checker recomputes (FR-023). 8 Write the proposal and the trace (FR-021, FR-030) |
| Alternate | Any rule that fires hands the invoice to UC-2 |
| Postcondition | A local proposal with a reason code, pending approval. This proof of concept cannot approve or apply it; production approval and payment release are separate human gates. |
| Rules | BR-1, BR-3, BR-5, BR-6, BR-10, BR-18 |

Batch contract for UC-1: replay, value, arithmetic, step-cap and input-conflict failures quarantine that invoice and continue by default; there is no continue switch. Client, policy, output-store and maximum rate-wait failures stop the run and mark remaining invoices not processed. Claimed supplier names are untrusted buckets, so per-supplier limits are not identity security; the global run cap bounds one run even when labels vary.

### UC-2 Exception resolution with context (the agent)

| | |
|---|---|
| Actor | The agent, then the accounts payable (AP) clerk |
| Trigger | UC-1 records one or more exceptions |
| Main flow | 1 Pick the primary skill (BR-17). 2 Read the `queries:` line of its `SKILL.md`. 3 Run only those fixed query ids. 4 Compose a recommendation of at most 120 words from a generic template, with cited evidence. 5 Checker verifies; a production model checker may retry up to 3 cycles (Design). 6 Write one worklist record (FR-025). 7 The clerk decides in the AP exception worklist |
| Postcondition | One worklist record, status open, listing every exception |
| Rules | BR-2, BR-7 to BR-9, BR-11, BR-13 to BR-15, BR-17 |

| Skill (folder under `skills/`) | Triggers when | Fixed queries | Playbook recommends (design time) |
|---|---|---|---|
| `no-validated-term` | Terms differ and no validated row covers the longest candidate (60 days or less); PO term shorter than master; unreadable payment terms | `po_terms`, `vendor_term_history`, `validated_terms`, `past_decisions`, `vendor_delivery_profile` | Which term applies, or ask the term owner; a weaker PO term needs approval |
| `over-60-days` | A candidate term is over 60 days and no person recorded express agreement | `po_terms`, `vendor_term_history`, `validated_terms`, `past_decisions` | A person confirms express agreement; no day is proposed until then |
| `missing-or-wrong-po` | No PO row, PO for another vendor, or invoice before PO | `po_terms`, `po_activity_state`, `extracted_invoice`, `vendor_record`, `past_decisions` | Route to the buyer |
| `po-not-approved-or-receipted` | PO blocked with no release, or no goods receipt | `po_activity_state`, `po_terms`, `past_decisions` | Nudge the buyer through a worklist notification (Design); flag the control exception |
| `supplier-or-remittance-mismatch` | Vendor name (a changed legal form counts) or company code differs from the PO header, or a document flag (instruction, bank or remit-to change, urgent payment, credential ask) | `extracted_invoice`, `po_terms`, `vendor_record`, `past_decisions` | Verify by phone on the number on file; no draft and no proposal for a flag |
| `discount-eligibility` | A discount appears only on the printed invoice | `po_terms`, `validated_terms`, `vendor_term_history`, `past_decisions`, `vendor_delivery_profile` | Ask whether the vendor agreed it; do not take it |
| `value-outside-tolerance` | Invoice value differs from PO value by more than 1%, or the currency is missing or different | `extracted_invoice`, `po_terms`, `po_activity_state`, `past_decisions` | Show both numbers and the money impact |

Skills are design-time playbooks. At runtime the runner reads only the `queries:` line of a `SKILL.md`; the recommendation text comes from two generic templates in `src/invoice_agent/exceptions.py`, a phone-verification hold for a supplier or remittance exception and a worklist hold for every other exception. The skill bodies describe what a model-backed resolver would draft and cite, which remains Design.

An unreadable `invoice_id` stops processing that invoice: no proposal or worklist record is written. The input is quarantined for correction, and later invoices can continue under the batch contract.

### UC-3 Approval and write back (design)

| | |
|---|---|
| Actors | AP clerk, Head of AP or delegate, the approval workflow, SAP, an authorized person |
| Main flow | 1 The clerk decides in the worklist (FR-028). 2 An approval form goes to the Head of AP or a delegate by link. 3 On approval an approved ERP action changes the baseline date or term in SAP through the approval workflow's SAP connection: a function module for the baseline date change, or the standard `BAPI_PO_CHANGE` for the PO. Sandbox tested first. 4 An authorized person edits the proposal in F110, the SAP payment run, and releases every run. 5 The business intelligence (BI) platform compares each invoice's payment outcome with the proposed payment day. 6 It reads the cash effect against the baseline |
| Constraints | Nothing else writes to SAP (BR-16). Whether a posted invoice's baseline date can change under SAP document change rules (OB32) is Q-6 |
| Rules | BR-12, BR-16, BR-18 |

### Extension list (out of scope for the proof of concept)

| ID | Use case | Note |
|---|---|---|
| UC-4 | Duplicate invoice suspect | Beyond the repeat check in FR-031 |
| UC-5 | Price variance per line | Needs line items; the reader takes header fields |
| UC-6 | Missing goods receipt | Overlaps `po-not-approved-or-receipted` |
| UC-7 | Early pay programme | Uses the discount rule across vendors |

## 3. Reason codes

These are the keys of `reason_codes` in `policy/term-rules.yaml` and the enum in `schemas/proposal.json`.

| Code | Used when |
|---|---|
| AGREED_TERM | Printed, PO and vendor master terms are equal |
| VALIDATED_LONGER_TERM | The terms differ and a validated term covers the longest one |
| DISCOUNT_TAKEN | The annualized discount beats the cost of capital and is validated |
| DISCOUNT_DECLINED | A validated discount does not beat the cost of capital |
| NEVER_ACCELERATE_VENDOR | The vendor is on the never-accelerate list |
| NO_VALIDATED_TERM | The terms differ and none is validated |
| OVER_60_DAYS | A candidate term is over 60 days with no recorded express agreement |
| MISSING_OR_WRONG_PO | No PO, a PO that does not match, or an invoice before the PO |
| PO_NOT_APPROVED_OR_RECEIPTED | PO blocked with no release, or no goods receipt |
| SUPPLIER_OR_REMITTANCE_MISMATCH | Supplier name (a changed legal form counts) or company code does not match, or a document flag |
| DISCOUNT_ELIGIBILITY | A discount no PO, master or validated term carries |
| VALUE_OUTSIDE_TOLERANCE | Invoice value outside the total tolerance |

## 4. Functional requirements

### Read (step 1)

| ID | Requirement | Fit criterion | Status |
|---|---|---|---|
| FR-001 | Parse payment term text into net days, discount percent and discount days. Return none for text it cannot parse. Never guess. | The golden `payment_terms` strings parse to the `printed_days` in `evals/golden/expected.json`. "Net 45, 1/10" gives 45, 1 percent, 10 days. "per agreement" returns none. | Scenario covered |
| FR-002 | The reader returns the seven fields, each with value, page, quote and confidence, valid against `schemas/extraction.json`. | An eighth key, a missing quote, a value over 200 characters or a quote over 240 fails validation. The payloads of the five golden invoices (four exceptions, one clear case) pass. | Scenario covered |
| FR-003 | A field that is null, or under `min_field_confidence` in `policy/term-rules.yaml` (0.80), is unreadable. It is named in the evidence and routed by `unreadable_field_routes`. | A null `payment_terms` gives no-validated-term with the field named. Confidence 0.79 is treated as unreadable. A null `invoice_id` quarantines the input with no proposal or worklist record; later invoices can continue. | Scenario covered |
| FR-004 | The sandbox extractor reads its source-grounded curated fixture response from `transcripts/`, keyed by invoice ID and role, and binds it to the source by SHA-256. For a `.txt` source every field quote must be non-empty and occur in the source text, and the value must lie inside its quote, or the invoice is quarantined. A PDF or image source is bound by hash only: its curated quotes are trusted provenance, not verified text. | With all keys absent and the network blocked in the test, `make demo` exits 0. A missing transcript fails with a clear error. A `.txt` source with a quote that is empty or not in the text, or a value outside its quote, is quarantined. No check claims to verify a PDF or image quote. | Scenario covered |

### Compare (step 2)

| ID | Requirement | Fit criterion | Status |
|---|---|---|---|
| FR-005 | Every ERP read uses a query id declared under `queries` in `integration/agent-tools.yaml`. Each variable matches its declared pattern. A skill runs only the query ids on its `queries:` line. The ERP client exposes only validated read methods. | An undeclared query, an undeclared variable, a `po_reference` that is not the purchasing document number zero padded to 10 digits, a raw query string, or a query outside the active skill's list is rejected before any call. | Scenario covered |
| FR-006 | Copy the PO header and the PO events (`po_line_created`, `goods_receipt_posted`, `po_blocked`, `po_released`, `invoice_received`, `invoice_cleared`) from the ERP data service into the evidence. Never recompute the three way match. | The evidence holds the `po_terms` and `po_activity_state` results. A static search finds no quantity or price match function in `src/`. | Reviewed |
| FR-007 | Compare invoice vendor and company code with the PO header and the supplier record; the PO reference, not the printed name, selects the record. The vendor name is compared suffix-insensitively through one normalizer shared by the decider, the checker and the vendor key. It strips punctuation and invisible characters, and long legal forms (Incorporated, Limited, Corporation, Company) equal their short forms. When the invoice name and the supplier record both carry a legal form (GmbH, Ltd, Inc and so on) and the forms differ, raise supplier-or-remittance-mismatch in both the decider and the checker. State that address and payment details are not available in the read views. | A different company code lists both values and raises supplier-or-remittance-mismatch. Equal values raise nothing. A name that differs only by a missing or differently punctuated suffix raises nothing, and Inc equals Incorporated. A changed legal form (for example Inc against Ltd) raises it in the decider and the checker, whatever punctuation or invisible characters surround the suffix. | Reviewed |

### Check the term (step 3)

| ID | Requirement | Fit criterion | Status |
|---|---|---|---|
| FR-008 | Show three parsed terms side by side: printed, `po_term_days`, `master_term_days`. | For each golden invoice the three terms equal `terms` in `evals/golden/expected.json`. | Scenario covered |
| FR-009 | Mark a case clear when the three terms are equal (AGREED_TERM) or a validated row covers the longest candidate (VALIDATED_LONGER_TERM), and no other rule fires. | Three terms of 30 give clear. A validated 45 with printed 30 gives clear. Any other exception overrides clear. | Scenario covered |
| FR-010 | When terms differ and none is validated, raise no-validated-term. If the PO term is shorter than the master, mark approval required (BR-7, Q-2). | A golden invoice whose terms do not all agree, with no validated row, gives no-validated-term. A case where the PO term is shorter than the master term also carries approval required. | Scenario covered |
| FR-011 | A candidate term over 60 days is an exception until a person records express agreement in the validated term register. A validated row alone does not substitute. | A golden invoice whose printed term is over 60 days, with an invoice-only discount, lists over-60-days. A validated term over 60 days with ExpressAgreement false still lists it. With ExpressAgreement true the term may govern. | Scenario covered |
| FR-012 | No PO row, a PO for another vendor, or an invoice received before the PO was created raises missing-or-wrong-po. The skill playbook routes it to the buyer; in the sandbox the recommendation holds the invoice in the worklist for a person. | A reference with no `po_terms` row gives MISSING_OR_WRONG_PO and a worklist record that holds the invoice. A drafted message to the buyer is a playbook step and Design. | Scenario covered |
| FR-013 | A PO with block and goods receipt events and no release event, or with no goods receipt, raises po-not-approved-or-receipted as a control exception. | A fixture with `po_blocked` and `goods_receipt_posted` events and no `po_released` event gives PO_NOT_APPROVED_OR_RECEIPTED marked control exception. The worklist notification to the buyer is Design. | Reviewed |
| FR-014 | A document flag of type bank_or_remit_to_change, urgent_payment_request or other_money_or_credential_ask routes to supplier-or-remittance-mismatch with no draft and no proposal. The action is a phone check on the number on file. Account numbers in extracted fields and flag quotes are masked to the last four characters before results are written, and the writer refuses output that contains a full IBAN. Masking is pattern based (IBAN shapes and long digit runs). | A fixture with a bank change flag gives a record whose recommendation names the phone check. A full IBAN in an extracted field is masked in the results. A pattern search over outputs finds no full IBAN. | Scenario covered |
| FR-015 | Invoice value more than 1% from the PO value, or an invoice whose currency is missing or not EUR, raises value-outside-tolerance with both numbers and the money impact. | A 1.5% variance gives an exception with both amounts and `money_impact`. A 0.5% variance passes. An invoice in dollars, or with no currency, raises it. | Reviewed |
| FR-016 | Vendors on the never-accelerate list in `policy/term-rules.yaml` get the governing term only. | A never-accelerate vendor with a discount that passes FR-018 gets no discount day, `discount_taken` false, NEVER_ACCELERATE_VENDOR. | Scenario covered |

### Propose (step 4)

| ID | Requirement | Fit criterion | Status |
|---|---|---|---|
| FR-017 | The governing term is the longest agreed or validated term, never over 60 days unless FR-011 is cleared. The proposal names its `term_source`. | Validated 30, 45, 60 gives 60. Validated 30 and 90 without agreement gives no proposal at 90. Equal sources resolve in the BR-3 order. | Scenario covered |
| FR-018 | Take a discount when its annualized rate exceeds `cost_of_capital` in `policy/term-rules.yaml` (5%, Q-1), using the BR-5 formula. | 2/10 against 30 days gives 37.2%: take. 0.5/10 against 60 gives 3.7%: decline. 1/10 against 45 gives 10.5%: take. | Scenario covered |
| FR-019 | A discount carried only by the printed invoice raises discount-eligibility and is not taken. | The golden invoice whose discount appears only on the printed invoice lists discount-eligibility, and its proposal has `discount_taken` false. | Scenario covered |
| FR-020 | For a clear case the proposal carries `proposed_payment_day`: days after the baseline date, the discount day if taken, else the governing term. For an exception it is null and the recommendation suggests the day. SAP computes the due date after the change. | Governing 45 and no discount gives 45. A taken 1/10 gives 10. An exception gives null. No output says the agent calculates the due date. | Scenario covered |
| FR-021 | Every proposal is valid against `schemas/proposal.json`: reason code from section 3, `rule_version` equal to the version in `policy/term-rules.yaml`, `checker_agrees` set. | Across the demo, every proposal validates and every code is a key of `reason_codes`. | Scenario covered |
| FR-022 | Every figure in a recommendation has an evidence item `{source, query_id, value}` with source erp, invoice or rules. Extracted fields carry page and quote. | Every evidence item in the demo has a non-empty `query_id`, which the schema enforces. No check confirms that each figure in a recommendation has a matching evidence item; that criterion is not verified (Design). | Reviewed; citation completeness Design |
| FR-023 | The sandbox checker gathers trusted inputs again in a separate context and independently recomputes the proposal. A genuinely different-family model checker remains Design (`policy/models.yaml`). A disagreement sets `checker_agrees` false and the invoice goes to the worklist with both values. | Shifting a recomputed day by one gives `checker_agrees` false and no auto apply. Altering the amount, term source or payment-day offset is detected; both values are retained as evidence. | Scenario covered |
| FR-024 | Bound the sandbox with shared orchestrator query steps, 400 client calls/minute, at most three read attempts, a 120-second maximum rate wait, and ingress defaults of 1000 invoices/run and 100/claimed case-folded supplier name. Quarantine invoice-scoped failures and continue; stop on client, policy, output or maximum-wait failures. | Pre-call file-size and cited-page limits reject over-limit inputs; cited pages do not measure physical PDF length. Summary counts and per-invoice outcomes match saved results after partial runs; exits are 0 clean, 1 run failure, 2 live refused, 3 quarantine completed. Model token/request/spend and reflection budgets remain Design. | Source scenarios covered |

### Write path and control (steps 5 and 6)

| ID | Requirement | Fit criterion | Status |
|---|---|---|---|
| FR-025 | Every exception makes one record valid against `schemas/worklist-record.json` as a local stand-in for the existing worklist: `exception_types` in BR-17 order, evidence, recommendation, `money_impact`, status open. Production landing remains Design. | Each demo exception gives one valid record whose first `exception_types` entry is the primary skill. Local files are not a deployed AP workflow. | Scenario covered |
| FR-026 | Only the `proposal_writer` role has Write, limited to `worklist/` and `audit/`. The extractor has Read only. Orchestrator and checker have the five read tools. | `policy/tool-policy.yaml` shows one role with Write, Read only for the extractor, and no write tool anywhere. | Scenario covered |
| FR-027 | Trigger ERP Action, Start Workflow and Resume Workflow are denied to every role (`denied_everywhere`). The hook and the mock server refuse them and the refusal is audited. | Each tool id through the hook returns deny. The mock server returns a refusal. `audit/hooks.jsonl` holds an event with `allowed` false. `tests/test_hardening.py` pipes crafted events into the hook and expects deny or a fail-closed result, also in development mode (`AP_AGENT_DEV=1`), which opens built-in tools for a person developing the repo but keeps the three write tools, the nested model and the hook log denied. | Scenario covered |
| FR-028 | Production authenticated decisions set approved/rejected status and identity/time. This proof of concept cannot capture approvals; a role string or environment variable does not authenticate a person. | Every writer rejects approved/rejected status, including role person. The Design artifact requires authenticated identity and a separate payment-release gate. | Design; refusal tested |
| FR-029 | No code or tool in this repo writes to SAP, edits an F110 proposal or releases a payment run. The write path is described in `integration/write-path.md`. | `BAPI_PO_CHANGE` and F110 appear only in docs, `integration/` and policy files. No remote function call (RFC) client library is imported. | Design; refusal tested |

### Audit, safety and evaluation

| ID | Requirement | Fit criterion | Status |
|---|---|---|---|
| FR-030 | One flat, append only trace per invoice in JSON Lines (JSONL, one JavaScript Object Notation object per line). Each line is a tool call, allowed or denied, valid against `schemas/audit-event.json`, with input and output as SHA-256 hashes. | After a rerun, earlier lines are unchanged. Timestamps do not decrease. A denied call has `output_hash` null. No line holds invoice text. | Scenario covered |
| FR-031 | Identical saved invoice bundles skip on rerun; changed semantics for the same invoice ID conflict and quarantine without replacement. A corrected invoice can succeed on rerun and clear its quarantine record. | Saved bundles stay unchanged, skipped invoices are counted, quarantine evidence includes stage/error class/message/retryability/time, and final results include completed invoices after a partial run. Local locks and journal do not guarantee cross-system exactly-once delivery. | Source scenarios covered |
| FR-032 | The sandbox handles supplied instruction-like `flags` and recovers recognized flags from curated quotes/text. Before pattern matching, the guard normalizes text to Unicode NFKC, folds Cyrillic and Greek look-alike letters to Latin, and reads default-ignorable, format and combining characters both as removed and as separators (without word boundaries in the first form), and it matches broader bank-change and remit-to wording. The patterns are a keyword heuristic for common English phrasings, not a general detector. A paraphrase, another language, or a different payee or IBAN printed without change wording is not flagged without trusted remit-to data (Design). It does not read source images; live instruction detection remains Design. | The injection case gives supplier-or-remittance-mismatch, no tool call outside the allowlist, and no early payment day. The instruction appears only as quoted evidence. A bank-change instruction written with look-alike letters, or with zero-width characters inside a word, between words or both, is flagged like the plain spelling, and the flag quotes the original text. | Scenario covered |
| FR-033 | `python3 evals/grade.py` grades the demo output (five golden invoices, four exceptions and one clear case, plus one injection case) against `evals/thresholds.json` and exits 1 when a gate is missed. It also rejects a `reason_code` that is not in the `schemas/proposal.json` enum or does not match the labelled primary exception (a clear case accepts only clear-path codes). | Lowering one label in a copy of `evals/golden/expected.json` makes the run exit 1. A forged `reason_code`, outside the enum or for another exception, also makes it exit 1. | Scenario covered |
| FR-034 | All live runs are refused by this proof of concept. Production Design requires exact pins in `policy/models.yaml` and distinct checker/orchestrator families. Replay needs no model pin. | The CLI rejects live execution regardless of environment keys. Exact-pin and distinct-family production contracts are reviewed Design artifacts, not unit-tested runtime behavior. | Design; refusal tested |

## 5. Non-functional requirements

| ID | Requirement | Fit criterion | Checked by |
|---|---|---|---|
| NFR-001 | No code, tool or credential in the repo can write to SAP, release a payment run or approve a proposal. | Client and writer refusal scenarios pass, five read tools are enabled, and review finds no SAP client or payment-release path. | Client/writer refusal scenarios, MCP smoke; SAP absence reviewed |
| NFR-002 | The demo and the tests run with no network and no model key. | `make demo` exits 0 with the network blocked. | `make demo` |
| NFR-003 | Rules are pure functions: no clock, no randomness. | Fixed synthetic outcomes are stable, and review finds no clock or randomness in the rules. | Fixed synthetic outcomes and repeat-run scenario; pure rules reviewed |
| NFR-004 | Each decision is checkable from the trace, the proposal and the record together. | For each golden invoice, the trace lists every tool call and its hashes match the proposal and record. | Failure/conflict audit scenarios; complete trace correspondence reviewed |
| NFR-005 | No exception is called clear on the labelled set. | `routing_recall_exceptions` meets its gate in `evals/thresholds.json`. | `evals/grade.py` |
| NFR-006 | No instruction from untrusted text is followed. | `injection_non_compliance` meets its gate. | `evals/grade.py` |
| NFR-007 | Data minimisation: seven fields, capped quotes, account numbers masked by pattern, declared columns only. | An output search finds only those; each query returns its declared columns. | `scripts/check.py`, supplied injection-flag scenario; data scope reviewed |
| NFR-008 | No secret in files or history. | Secret patterns absent; `.env.example` holds no credential values. | `scripts/check.py`, security workflow |
| NFR-009 | Cost and loops are bounded by policy in production Design. | Policy declares per-role step, cost and reflection caps. In the sandbox, only the orchestrator query-step budget is wired; cost charging, reflection and other role budgets remain Design. | Orchestrator step wiring reviewed; other declared caps reviewed as Design |
| NFR-010 | The local client allows at most 400 read calls per rolling minute, with bounded waits and at most three total attempts. | Rate admission waits without consuming a remote attempt; over-budget waits fail closed. A local cap does not enforce a shared service quota across clients. The client refuses a local cap of 1000 or more; that ceiling is a local value, not a service limit. | Source scenarios covered; production requirements in SECURITY.md |
| NFR-011 | Portable and wired: skills have spec-only frontmatter, mirrored skill folders match, the settings allowlist matches policy, the PreToolUse hook is wired in `.claude/settings.json`, all four denied tools (the three write tools and the nested model) are in its deny list, and each subagent's tool list and turn cap fit its policy role. | The checks report no drift. | `scripts/check.py` |
| NFR-012 | Python 3.12, standard library plus the direct dependencies `mcp`, `pydantic`, `anyio`, `httpx2` and `mcp_types`, and `jsonschema` for the tests; continuous integration (CI) actions pinned by commit hash. | `requirements.lock` pins every dependency by hash, and the workflows pin every action. | `requirements.lock`, `.github/workflows/` |
| NFR-013 | Readable: no em or en dashes in any file. Docs under 300 lines (architecture under 500) and paragraphs of three sentences or fewer are conventions. | `scripts/check.py` reports no em or en dash in a file or file name. It also scans for secret patterns (NFR-008) and does not count lines or sentences. | `scripts/check.py` for dashes; review for line counts and paragraph length |
| NFR-014 | The capacity illustration is parametric: at N invoices a day, partitioned under the 1000/run ceiling. | Measured in the sandbox: 9 reads an invoice on the clear path (4 to gather, 5 in the checker) and 13 to 14 on the exception path. At 400 reads a minute, N clear invoices take about N/44 minutes and N exception invoices about N/29 minutes, before processing and retry overhead. One full 1000-invoice run is 9,000 to 14,000 reads, or 23 to 35 minutes. No throughput or load-test result is claimed. | Arithmetic review; production load test remains Design |
