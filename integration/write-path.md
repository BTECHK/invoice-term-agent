# Write path: from an approved decision to SAP

Status: partly built, partly design. Built: the proposal writer saves worklist records and audit events as local files (`worklist/`, `audit/`), and the mock ERP data service in `mock_erp/server.py` refuses the three write tools. Design: batch ingestion into the BI platform, the worklist, the approval form, the approved ERP action and the payment run release. Nothing here has run against an SAP system (SAP ECC, the ERP Central Component). Each design step below is tested in a sandbox client first.

## The rule

The agent prepares data. It stages proposals and exception records and never posts to the ERP. The model writes nothing to SAP and has no tool that can (architecture decision record ADR-003). An invoice is untrusted input, so a change to SAP happens only after a person approves it, and only through an approved ERP action that is deterministic, logged and permissioned.

The three write tools of the ERP data service (Trigger ERP Action, Start Workflow, Resume Workflow) are denied to every role in every environment. They are listed in `policy/tool-policy.yaml` under `denied_everywhere` and in `.claude/settings.json` under `deny`. The mock server refuses each call.

## The path

| Step | Who or what | What happens | State | Control |
|---|---|---|---|---|
| 1 | Proposal writer | Writes a worklist record with status `open`, and the proposal, as local files in the sandbox. At volume the BI platform ingests them in batch into the staging tables `invoice_extraction` and `agent_worklist` (see Landing below) | Local files built; ingestion Design | Only Write holder; paths limited by policy |
| 2 | Accounts payable (AP) clerk | Reads the evidence in the worklist and decides. The worklist is the BI platform's worklist with the evidence added | Design | Exceptions are always human |
| 3 | Approval workflow | Sends an approval form to the Head of AP or a delegate by a link. The form shows the three terms, the evidence and the proposed term or baseline date | Design | The form is the approval record. The link expires (7 days is the assumed default) |
| 4 | Approval workflow | A submitted form resumes the workflow. A rejection ends it and nothing changes in SAP | Design | The model never resumes a run |
| 5 | Approved ERP action | Calls SAP through its standard change interface (a remote function call, RFC, to a BAPI or function module) with the approved values only | Design | Deterministic. Sandbox tested first |
| 6 | BI platform outcome tracking | The payment outcome of each invoice is compared with the proposed payment day, and the cash effect is read against the baseline | Design | Rate based: baseline rate minus current rate, times volume, times impact |
| 7 | Authorized person | Edits the F110 payment proposal (the SAP payment run transaction) and releases every run | Human process, outside this repository | The payment run is a second gate. Release is always human |

## What the approved ERP action can change

| Change | Path | State | Notes |
|---|---|---|---|
| Baseline date of a posted invoice | An RFC-enabled function module that changes the document's baseline date, custom built or vendor supplied | Not built. Needs the module in the SAP system | The action runs on a selected, approved invoice. It runs as a technical SAP user restricted to that module and the company codes in scope (T-25) |
| Payment term on the purchase order (PO) | The standard Business Application Programming Interface (BAPI) `BAPI_PO_CHANGE`, field "Terms of Payment Key" | Standard SAP interface, not built here | Changes the PO. It does not by itself move the due date of an invoice already posted. Test in the sandbox |
| Term of a posted invoice | A generic RFC or the function module above | Not prebuilt | Depends on the SAP document change rules, see Q-6 |
| Payment block | Not used by this design | Out of scope | A duplicate-invoice flow in another tool may set a block. This design does not |
| Vendor master payment term | Never | Out of scope | Procurement owns who may set or change it (Q-2, assumed) |
| Payment run release | Never | Out of scope | A person edits the proposal and releases the run |

The commit mode of the function call (commit, commit and wait, no commit) is set per action. The sandbox test uses no commit first.

## Approval is a workflow control

The form approval is not an SAP authorization. SAP roles stay as they are, and the approved ERP action writes as a technical SAP user. Segregation of duties therefore lives in the workflow: who may approve on the form, the workflow run log and the audit trace.

Recommended while the trust ladder is on shadow or propose with approval: the person who decides in the worklist and the person who approves on the form are different people for any change above a value threshold set with the Head of AP. Running the action as the approver's own SAP user may not be available on every platform. Check before relying on it.

## Test in the sandbox before any write

| Test | Pass when |
|---|---|
| Form approval | A submitted form resumes the run and a rejection ends it with no SAP call |
| Baseline date change on a test invoice | The date changes and the due date follows from the payment terms |
| Reversal | The original date is restored by a second approved run |
| Document change rules (Q-6) | The change is allowed or refused exactly as the rules say, and a refusal returns a clear error to the worklist |
| PO term change | The PO shows the new key and the open invoice is checked separately |
| Commit modes | No commit leaves SAP unchanged. Commit and wait returns the result |
| Idempotency | Running the same approved change twice leaves one change |
| Denied tools | A call to any of the three write tools from the model is refused and appears in the audit trace. Built: the mock server refuses them (FR-027) |

## Landing proposals and records in the BI platform

| | Sandbox (built) | At volume (Design) |
|---|---|---|
| Method | Local files in `worklist/` and `audit/` | Batch ingestion into staging tables: CSV (comma separated values) at first, Parquet files (delivered in the style of S3 object storage) at volume |
| Why | Five golden invoices and one injection case; local files are the simplest thing that works | Only exceptions and proposals land, not every PO line, so a batch fits the daily cadence (ADR-007) |
| Load | A few records | N invoices a day, in batches bounded by the per-run caps in `policy/tool-policy.yaml` (1000 invoices a run by default) |
| Key | `invoice_id` | `invoice_id`. An identical delivery is skipped. A delivery with changed inputs is rejected as a conflict. A second record is never added (ADR-006) |

The staging tables are `invoice_extraction` (the reader's output) and `agent_worklist` (worklist records), both described in `integration/agent-tools.yaml`. Proposals and exception records are keyed on `invoice_id`; how the proposal fields map onto these tables is Design. The validated term table is owned by Procurement and the agent only reads it (Q-2).

## Authentication and limits

| Item | Rule |
|---|---|
| Service access | OAuth client credentials with a read scope (`erp-read.tools:execute`) |
| People | Authorization code with PKCE (Proof Key for Code Exchange) |
| Static keys | Never used here |
| Secrets | In environment variables (`ERP_MCP_URL`, `ERP_TOKEN`), never in a file. `.env.example` holds placeholders only |
| Rate | The client throttles at 400 calls a minute as a local policy, under a local ceiling of 1000. The limits of a live service are verified before any live use |
| Endpoint tools | The service exposes only the five read tools to this client. This is the guard for runtimes that have no hooks |

## Open questions

| ID | Question | How it closes |
|---|---|---|
| Q-6 | Can the baseline date of a posted invoice be changed under the organization's SAP document change rules for line items (transaction OB32)? | Sandbox test before the first write, with the AP and IT leads |
| Q-2 | Who owns term approval in the organization? Procurement is assumed | Ask before the write path is built |
| Unnumbered | Which function module changes the baseline date of a posted invoice in the organization's SAP release, custom built or vendor supplied, and who transports it? | Decide before the write path is built; the build waits on it |

## What this design does not claim

- That SAP cannot compare PO, vendor and invoice terms. SAP, and comparison tools built on it where installed, may already do so on the SAP side (see the [ledger](../docs/redundancy-ledger.md)). The new input is the printed term.
- That the payment run selects only by due date. The next run date, tolerance days, discount limits and blocks decide.
- That the agent calculates due dates. SAP does. The agent proposes a term or a baseline date.
- That it adds a worklist. It feeds the existing one.
- That it releases payment. It never does.
