# Security policy

This repository is a proof of concept, not a deployed product. It holds no production credentials and no real supplier data. The invoices under `evals/golden/` and the ERP rows under `mock_erp/fixtures/` are test fixtures, used only to test the design.

## Reporting a problem

Report privately to the repository owner, for example through a private security advisory on the hosting platform. Do not post details in a public place.

A good report names the file, what you saw, how to reproduce it and what you expected. Do not include real invoices, bank details, tokens or personal data. Describe their shape instead.

## In scope

| Area | Examples |
|---|---|
| Policy bypass | A role that can call a denied tool, or write outside the output store |
| Injection | Invoice text or ERP rows that make the agent act, or hide a flag |
| Data exposure | A secret, a full bank number or personal data in a file, trace or log |
| Supply chain | An unpinned dependency or action, or a workflow permission wider than read |
| False claims | A document that states a control the code does not enforce |

Out of scope: flaws in SAP, in a vendor's ERP data service or BI platform, or in the model providers themselves (report those to the vendor), social engineering of staff, and load against your own infrastructure.

## Controls in one table

The full list is in [docs/06-threat-model.md](docs/06-threat-model.md), where T-# is a threat identifier.

| Control | Enforced in | Threat |
|---|---|---|
| Deny by default tool policy, hook and settings allowlist; the hook has an automated test | `policy/tool-policy.yaml`, `.claude/settings.json`, `.claude/hooks/pre-tool-use.py`, `tests/test_hardening.py` | T-8, T-22, T-23 |
| The three ERP write tools denied in every environment | `policy/tool-policy.yaml`, `integration/agent-tools.yaml` | T-22 |
| Invoice text handled as data, reader without tools; the guard strips default-ignorable, format and combining characters, normalizes Unicode and folds Cyrillic and Greek look-alike letters before it matches instruction, bank-change and remit-to patterns. It is a keyword heuristic for common English phrasings, not a general detector | `src/invoice_agent/extract.py`, `src/invoice_agent/guard.py`, `schemas/extraction.json` | T-4 |
| Supplier name and company code compared with the supplier record through one shared normalizer; a changed legal form routes to a person | `src/invoice_agent/decide.py`, `src/invoice_agent/propose.py` | T-1 |
| For a `.txt` source every field quote must be non-empty and occur in the source, and the value must lie inside its quote, or the invoice is quarantined; PDF and image quotes are bound by hash only | `src/invoice_agent/extract.py` | T-6 |
| Fixed query ids, no model written query text | `integration/agent-tools.yaml` | T-7 |
| Hook path checks are case-insensitive, reads of any `.env` file except `.env.example` are denied, and development mode keeps the write tools denied | `.claude/hooks/pre-tool-use.py`, `tests/test_hardening.py` | T-8, T-16, T-22 |
| Repository gate: `scripts/check.py` verifies the hook is wired in `.claude/settings.json`, all four denied tools are in the deny list, and each subagent's tool list and turn cap fit its policy role | `scripts/check.py`, `.github/workflows/tests.yml` | T-8, T-23 |
| Append only audit trace | `src/invoice_agent/audit.py` | T-9 |
| Secret scan over full history, placeholders only in `.env.example` | `.gitleaks.toml`, `.pre-commit-config.yaml` | T-16 |
| Hash-pinned dependencies and continuous integration (CI) actions, read-only workflow permissions | `requirements.lock`, `.github/workflows/` | T-10 |

## Secrets

- Never commit `.env`. Keep placeholders in `.env.example`.
- Use an OAuth client-credentials grant with a read-only scope and short-lived tokens. Keep secrets in a managed store and grant only the data-service permissions needed. A scope name alone does not make a connection read-only; the tools the service exposes and its server-side permissions must enforce that boundary. See the production requirements below.
- If a secret reaches a file, treat it as exposed. Revoke it at the source first, then remove it from history.

## Data handling

- Real invoices do not belong in this repo. Add labelled cases from described, anonymized shapes only.
- Account numbers in extracted fields and flag quotes are masked to the last four characters before results are written, and the writer refuses to commit any output that contains a full IBAN. Masking is pattern based (IBAN shapes and long digit runs), so an account number in another shape can pass.
- Traces hold digests, not raw bodies. Extracted quotes are capped in length.
- A future live reader would send the invoice image to a model endpoint. Approve the provider, region, retention terms and allowed data before enabling it; the sandbox makes no such call.

## Development mode

A person who changes this repository with a coding agent sets `AP_AGENT_DEV=1` before launching it; an agent never sets it, and no unattended or production run uses it. The mode opens the built-in tools so the repository can be edited, and the audit line records the role `person`. The three ERP write tools, the nested model tool and the hook log stay denied, and `tests/test_hardening.py` checks that.

## Supported versions

Only the current contents of the repository. There are no patch releases.

## Known limitations

Residual risks are listed in section 3 of [docs/06-threat-model.md](docs/06-threat-model.md). The main ones are a mock server that differs from a live ERP data service, a small golden set, curated quotes on PDF and image sources that are bound by hash but not verified against the page, payment-change text the guard misses (a paraphrase, another language, or a different payee or IBAN printed without change wording), and unverified production approval and SAP authorization controls. An approval form sign-off in the approval workflow does not grant SAP authorization or prove segregation of duties.

## Production requirements, Design only

These are deployment requirements, not claims that the sandbox implements a production service. Limits, quotas, configuration and contracts for the chosen ERP data service, workflow platform and model provider still need validation before rollout; the figures below are defaults to check, not guarantees. All three model-facing write tools remain denied.

| Area | Required production control and evidence | Sandbox boundary and reference |
|---|---|---|
| OAuth and secrets | Use an OAuth client-credentials grant with a read-only scope, least data-service permissions, short-lived access tokens and managed secret storage with rotation. Verify token lifetime and the published read-only tool inventory. | No credentials or live authentication. A scope name alone does not make a connection read-only; check the grant, the scope and the separate per-service permission in the chosen service's documentation. |
| Egress and privacy | Allow only approved ERP data service and model endpoints; minimize invoice images and evidence; approve region, retention, training use and access terms before data leaves the boundary. | No network calls by the replay runtime. This is a project requirement, not an assurance about any provider contract (T-15, T-17). |
| Shared service rate | Budget all clients against the call limit the chosen service documents for one endpoint. The ERP client treats 1000 calls a minute as a local ceiling and refuses a local cap at or above it. That value is a local default, not a service limit; the chosen service's limit is verified before any live use. Coordinate a shared queue and honor server throttling. Reserve capacity for other consumers. | Local 400 calls/minute is a client limit, not a distributed quota. The ERP client uses a 120-second maximum wait and at most three total attempts for retryable reads. T-19. |
| Input volume | Tune ingestion ceilings with accounts payable (AP) before rollout; meter across workers and time windows. Authenticate supplier identity upstream where available. | Defaults: `max_invoices_per_run: 1000`, `max_invoices_per_supplier_per_run: 100`. The case-folded claimed supplier name is an untrusted bucket, not authentication; the global cap bounds one run even if that label is spoofed. Repeated runs need production admission control (T-20, T-21). |
| Model limits | Enforce per-request input/output token limits, request rates, timeout, concurrent requests and spend caps per invoice and per environment; fail closed when the budget cannot be established. | Model request, token and spend enforcement remains Design. Declared role budgets are not proof of provider-side enforcement (T-18). |
| Approval workflow limits | Record the workflow platform's operation, execution, concurrency, scheduling and payload limits before sizing; configure and alert on deactivation after repeated errors. Validate the configured threshold and recovery owner. | No approval workflow runs. Numerical runtime limits are unverified here and must not be inferred from the read-side call limit. |
| SAP load | Agree Remote Function Call (RFC) concurrency, queue depth, timeout and retry ceilings with SAP operations; measure effects on shared workers and document locks in a test system. | No SAP calls. No universal SAP throughput claim is made; connector details for the chosen integration remain unverified. |
| Write failure handling | Use reviewed error handlers in the approval workflow for approved writes. Preserve the approved values and correlation key; reconcile ambiguous timeouts with SAP before retrying, and verify the result by reading it back. | No write path is enabled. Handler behavior for the selected SAP module, transaction boundaries and rollback require validation. |
| Audit durability | Retain immutable or access-controlled durable evidence linking proposal, identity, approval, workflow execution and SAP change record; test crash recovery, export, retention and replay deduplication. | Local audit, locks and journal are stand-ins. They are not cross-system exactly-once delivery or tamper-proof storage; a workflow platform's own execution history does not by itself prove durability. [SAP change documents][changes]; T-9, T-12. |
| Segregation of duties | Authenticate approvers, separate submitter/approver/releaser, restrict the SAP technical user by function and company code, and verify SAP authorization independently. | Approval form sign-off is not SAP authorization. The approval form and the write integration remain Design. T-3, T-25. |

[changes]: https://help.sap.com/saphelp_gbt10/helpdata/en/48/d1c0eff6c96745e10000000a421937/content.htm?no_cache=true
