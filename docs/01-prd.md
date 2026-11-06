# Product requirements

This repository is a proof of concept (PoC) for accounts payable (AP) data preparation ahead of ingestion into an enterprise resource planning (ERP) system, such as SAP ERP Central Component (ECC), and into a business intelligence (BI) system. It covers the payment-term step: it checks the term printed on a supplier invoice against the purchase order (PO) term and the vendor master term, then writes schema-bound, ingestion-ready records.

The agent reads through a read-only ERP data service (a Model Context Protocol (MCP) server, mocked locally in `mock_erp/`). It writes payment-day proposals, exception worklist records and hashed audit events. A person approves anything that changes the ERP; the agent never approves, never posts to the ERP and never schedules or releases a payment.

Identifiers (IDs) in this file point into the other documents: US user story, BR business rule, UC use case, FR functional requirement, NFR non-functional requirement, ADR architecture decision record, T threat, Q open question. See [README.md](README.md).

The sandbox replays steps 1 to 4 of the flow below from source-grounded curated extraction fixtures, against a mock ERP data service, with a deterministic checker and local worklist records. Steps 5 and 6 are design.

## 1. The flow

| Step | What happens | Status here |
|---|---|---|
| 1 Read the invoice | A reader returns seven fields from the invoice file, each with value, page, quote and confidence. Production Design uses a vision model for varied PDF (portable document format), image or e-invoice layouts | Replayed from curated fixtures; the production reader remains Design |
| 2 Compare it in the ERP data | The fields are compared with the PO, goods receipt and supplier record, next to the match SAP already ran | Read tools against the mock ERP data service |
| 3 Check the term | Printed, PO and vendor master terms agree, or a validated term is on file: clear. Otherwise an exception | Runnable in the sandbox; production remains Design |
| 4 Propose the payment date | Longest validated term, never over 60 days without express agreement, discount when it beats the cost of capital, reason code on every proposal | Runnable in the sandbox; production remains Design |
| 5 Approve, then update SAP | The Head of AP or a delegate approves in the approval workflow. An approved ERP action changes the baseline date or term in SAP | Design |
| 6 Release and track | An authorized person releases every payment run. The BI platform compares each invoice's payment outcome with the proposal | Design |

## 2. Problem and value levers

Three sources can hold three different payment terms for one invoice: the invoice, the PO and the vendor master. When nobody sees them side by side, a payment can go out earlier than the term allows, on a term nobody agreed, or with a discount nobody offered. A clerk who does compare them opens several screens for each invoice.

The proof of concept shows that this preparation can be rule-based, cited and audited before the data reaches the ERP and the BI system. Each lever below is stated as a measure with parameters and carries no figure, because figures come from a deployment's own data and from shadow evaluation.

| Lever | What goes wrong without the agent | What the agent does | Measure |
|---|---|---|---|
| Working capital | An invoice is paid before its longest valid term | Proposes the day on the longest agreed or validated term, never over 60 days without agreement | Days gained = proposed payment day minus observed payment day. Cash held longer = days gained times payable value per day |
| Discounts | A discount is taken that costs more than the money is worth, or one nobody agreed | Takes a discount only when it is validated and its annualized rate beats the cost of capital | Discounts taken below the cost of capital: 0. Invoice-only discounts taken: 0 |
| Fewer manual checks | A clerk compares three terms by hand on every invoice | Compares the three terms and passes clear cases with a reason code | Clear-path rate. Checks avoided = clear-path invoices times minutes per manual check (a deployment input, see Q-4) |
| No unagreed terms | A printed term differs from the PO or the vendor master and the invoice is paid anyway | Routes every disagreement to a person and holds terms over 60 days until express agreement | False clear count: 0. Over-60-day terms applied without a recorded agreement: 0 |
| Evidence | A decision cannot be rebuilt later | Cites every figure and writes one hashed trace per invoice | Decisions with a complete trace: 100% |

SAP and tools built on it may already compare the PO, vendor and posted invoice terms; coverage is unverified here (see the [redundancy ledger](redundancy-ledger.md)). The new input is the printed term, plus a path from evidence to an approved change. Whether invoices are paid earlier than their term is a hypothesis for each deployment until the clearing date is compared with the baseline date plus the term days (Q-5).

| Case type in the labelled set (five golden invoices and one injection case) | What it exercises |
|---|---|
| Printed, PO and vendor master terms that do not all agree, with no validated row | No term governs without a validated row, so a person decides. A PO term shorter than the vendor master term also needs approval from the term owner (BR-7) |
| Printed term over 60 days, discount printed only on the invoice | Over-60-days hold and discount-eligibility; no discount is taken |
| All three terms equal, nothing else flagged | The one clear case: AGREED_TERM and a proposed payment day |
| All three terms equal, the document carries an instruction, a bank change and an urgent payment request | Routed as supplier-or-remittance-mismatch; the instruction is never followed |

One of the five golden invoices is a clear case, and the injection case is an exception. Synthetic fixtures in `tests/` exercise the validated longer term, discounts and the never-accelerate list; `evals/grade.py` does not cover them.

## 3. Goals

| Goal | Lever | Measure | Checked by |
|---|---|---|---|
| Put the printed, PO and vendor master terms side by side on every invoice | Fewer manual checks | Share of invoices with three parsed terms on the record, 100% | FR-008 |
| Pass clear cases with a reason code and a projected day | Fewer manual checks | Clear-path rate (below) | FR-009, FR-021 |
| Pay on the longest valid term | Working capital | Days gained per clear invoice, measured on deployment data | FR-017, FR-020 |
| Take a discount only when validated and above the cost of capital | Discounts | Discounts below the cost of capital, 0; invoice-only discounts, 0 | FR-018, FR-019 |
| Never call an exception clear | No unagreed terms | False clear count 0 | NFR-005 |
| Hold terms over 60 days until express agreement is recorded | No unagreed terms | Over-60-day terms applied without agreement, 0 | FR-011 |
| Give the clerk a cited recommendation for every exception | Fewer manual checks | Exceptions with a cited record, 100% | FR-022, FR-025 |
| Keep a person on every write and every payment release | No unagreed terms | Agent writes to SAP, 0 | NFR-001 |
| Leave evidence for every decision | Evidence | Decisions with a complete trace, 100% | FR-030, NFR-004 |
| Read value against a defined baseline | All levers | Measured cash outcome in the BI platform, rate based: (baseline rate minus current rate) times invoice volume times impact per invoice | BI platform, outside this repo |

Clear-path rate is the share of invoices read that pass UC-1 with no worklist record. "Touchless" has no single industry definition, so this file states its own and quotes no market number. Targets other than 0 and 100% come from shadow evaluation scores, not from a guess here.

## 4. Binding non-goals

These hold at every trust rung. A change to a row needs a recorded decision from the Head of AP and a new ADR (architecture decision record), not a pull request.

| The agent never | Why | Enforced by |
|---|---|---|
| Releases, schedules or edits a payment run (F110, the SAP automatic payment program) | The run is a second gate and stays with an authorized person | FR-029 |
| Writes to SAP | Only an approved ERP action does, behind a human form | FR-027, FR-029 |
| Calls an ERP write tool (Trigger ERP Action, Start Workflow, Resume Workflow) | The invoice is untrusted input | FR-027 |
| Writes its own query | A model written query can widen a read | FR-005 |
| Re-runs the three way match | SAP matches at invoice entry; the agent reads the result | FR-006 |
| Calculates the due date | SAP does; the agent proposes a baseline date or term change | FR-020 |
| Builds a competing worklist | It feeds the existing AP exception worklist | FR-025 |
| Edits vendor master data | Procurement owns terms (Q-2). An approved ERP action may change a PO term | FR-029 |
| Updates remit-to or bank details from an invoice, or stores a full bank number | Verified by phone on the number on file | FR-014 |
| Treats invoice text as an instruction | Everything in a document is data from the sender | FR-032 |
| Takes a discount only the invoice offers, or pays a never-accelerate vendor early | Not validated; unreliable delivery | FR-019, FR-016 |
| Pays past 60 days without a person confirming express agreement | German Civil Code (BGB) 271a(1) | FR-011 |
| Replaces SAP's own term handling, a comparison tool built on SAP or the invoice capture product | They own the SAP-side comparison and the reading at scale | none |

## 5. Claims

| We say | We do not say |
|---|---|
| The agent reads the printed term and compares it with the PO and vendor master terms SAP already holds | SAP cannot compare PO, vendor and invoice terms |
| The agent proposes a baseline date or term change with evidence | The agent calculates due dates |
| The payment run selects by next run date, tolerance days, discount limits and blocks | F110 selects only by due date |
| Production Design feeds the existing worklist; local files are a stand-in | The sandbox installs a live worklist integration |
| Approval is a workflow form and a workflow control | The approval is an SAP authorization |
| The write goes through an approved ERP action after a sandbox test; it is not turnkey | The write is a switch |
| A person edits and releases the payment run | The agent releases payment |
| The 60 day cap cites BGB 271a(1) in full: expressly agreed and not grossly unfair | A validated term proves agreement |
| Value is measured in the BI platform against a defined baseline | A savings or touchless number before shadow evaluation |

The [redundancy ledger](redundancy-ledger.md) separates native capabilities from agent value and local stand-ins. Its removal candidates retain all current controls until a verified replacement exists.

## 6. Personas

| Persona | Role | Needs | Touches |
|---|---|---|---|
| AP clerk | Works the exception worklist | A cited recommendation, fast | Worklist record |
| Head of AP or delegate | Approves term and date changes | The rule, dates and money on the form | Approval form |
| Term owner (Procurement assumed, Q-2) | Decides which term governs | The vendor's term history | Worklist record |
| Buyer | Fixes PO problems | A nudge naming the PO and the gap | Worklist notification |
| Authorized person | Edits and releases F110 | Proposals that show their reasons | Payment run |
| Controller or internal audit | Tests the control | One trace per invoice | Audit trace |
| ERP data service owner | Runs the read service, reads value | Read-only tools, a rate limit, value views | ERP data service, BI platform |
| Maintainer, including artificial intelligence (AI) coding agents | Changes this repo | Rules and gates that fail loudly | `docs/README.md` rules |

## 7. User stories

| ID | Story | UC | FR |
|---|---|---|---|
| US-1 | As an AP clerk, I want the printed, PO and vendor master terms side by side, so that I see the conflict without opening three screens | UC-1 | FR-008 |
| US-2 | As an AP clerk, I want a cited recommendation for each exception, so that I decide fast and can show why | UC-2 | FR-022, FR-025 |
| US-3 | As an AP clerk, I want a doubtful field named instead of guessed, so that I do not pay on a wrong value | UC-1 | FR-003 |
| US-4 | As the Head of AP, I want the form to show the rule, the dates and the money, so that I approve with evidence | UC-3 | FR-021, FR-028 |
| US-5 | As the Head of AP, I want clear cases to carry a reason code, so that I can sample them | UC-1 | FR-021 |
| US-6 | As the Head of AP, I want clear cases to auto-apply only after shadow evaluation proves the score, so that trust is earned | UC-3 | FR-033 |
| US-7 | As the Head of AP, I want a discount taken only when the vendor agreed it and it beats the cost of capital, so that we do not chase discounts that cost more than they earn | UC-1 | FR-018, FR-019 |
| US-8 | As the term owner, I want a weaker PO term sent to me, so that buyers do not shorten terms by accident | UC-2 | FR-010 |
| US-9 | As a buyer, I want an invoice that arrived before its PO routed to me with vendor and value, so that I can raise the PO | UC-2 | FR-012 |
| US-10 | As an authorized person, I want the agent never to release or schedule a payment, so that the run stays my decision | UC-3 | FR-029 |
| US-11 | As a controller, I want one trace per invoice, so that I can rebuild any decision | UC-1 | FR-030 |
| US-12 | As a controller, I want terms over 60 days held until express agreement is confirmed, so that we meet BGB 271a(1) | UC-2 | FR-011 |
| US-13 | As the ERP data service owner, I want the agent limited to read tools and a rate limit, so that it cannot write or crowd out other users | UC-1 | FR-024, FR-027 |
| US-14 | As a security lead, I want invoice text treated as data, so that a hostile PDF cannot steer the agent | UC-1 | FR-032 |
| US-15 | As a maintainer, I want every change to run the eval gate, so that a prompt or model change cannot lower quality unseen | UC-1 | FR-033, FR-034 |

## 8. Rollout

| Stage | What | Gate |
|---|---|---|
| Shadow | The agent computes; nothing reaches approvers; compare with what clerks decide | Score above `evals/thresholds.json` on the labelled set |
| Wave 1 | A small set of vendors chosen with AP, propose with approval | Exception rate under an agreed threshold; capacity fits (ADR-010) |
| Wave 2 | All vendors, propose with approval | Same |
| Later | Clear cases auto-apply under thresholds with sampled review | Head of AP sign-off |

Exceptions and payment run release stay with a person at every stage (ADR-003).

## 9. Dependencies

| Dependency | Needed for |
|---|---|
| An ERP data service that exposes the five read tools, with an OAuth client | Steps 2 and 3 |
| PO, receipt, supplier and invoice data in the data catalog | Steps 2 and 3 |
| A BI platform view of payment outcomes against the baseline date and term days | Step 6 tracking |
| A SAP sandbox | Sandbox test of the write path (Q-6) |
| A validated term table kept by the term owner | The clear path via validated terms |

## 10. Open questions

| ID | Question | Assumed here | Who can answer |
|---|---|---|---|
| Q-1 | What cost of capital does the discount rule use? | 5% | Finance |
| Q-2 | Who owns term approval at the deploying organization? | Procurement | Organization leadership |
| Q-3 | How many people work the worklist, and how fast? | Nothing assumed; ADR-010 shows illustrations | Head of AP |
| Q-4 | How many minutes does one exception touch take? | 8 in the ADR-010 illustration only | A time study of AP clerks |
| Q-5 | What is the exact late balance by term? | Nothing assumed | A read query against the ERP reporting layer during the build |
| Q-6 | Can the baseline date of a posted invoice change under the organization's SAP document change rules (OB32)? | Unknown; the PO term path is the fallback | Sandbox test |
| Q-7 | Which capture tool does the organization license (SAP Document Information Extraction, OpenText Vendor Invoice Management (VIM), none)? | No capture product assumed; the sandbox replays curated extraction fixtures; a production vision reader remains Design | AP and information technology |
