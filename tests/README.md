# Essential tests

`make test` runs 67 behavior tests in three files: 13 workflow tests, 15 control tests and 39 hardening tests. The suite includes the grader regression checks, so the normal `tests.yml` CI job runs the complete suite. No separate grader suite or duplicate evaluation workflow remains.

## Keep

- Real CLI results for the golden invoices and the injection case, including supplied injection-flag handling.
- Six synthetic business outcomes: equal and validated terms, discounts taken and declined, never-accelerate, and an over-60 hold; the reviewed mixed 75/90-day case must also hold. Validate the emitted proposal schema.
- Four corruptions of actual output: missing invoice, unsafe payment day, wrong amount and forged source hash. The grader must reject each.
- Missing/mismatched replay, a low-confidence ID and malformed validated terms fail with audit evidence. Missing PO, null or low-confidence terms and an out-of-tolerance total retain their real hold reasons.
- Repeat-run skip, changed-fact conflict, preservation of prior records and retained attempt audit.
- Independent checker disagreement, real MCP write/query/throttle denials, and one terminal event for a malformed response.
- Interrupted-bundle recovery, rejected audit/state/locks directory redirection, rejected forged approval, refused live execution, and package-root selection from a crafted working directory.

Expected payment outcomes are explicit constants, not computed by the runtime under test. The golden and workflow paths use the real local stdio service. Focused transport doubles and a controlled clock exercise malformed responses, transient failures, server retry delays and local rate waits without network traffic or real sleeping.

## Hardening checks

- A corrupt middle invoice quarantines while later invoices complete; a corrected rerun skips saved bundles and clears quarantine after success.
- A shared run failure preserves partial results, lists unprocessed invoices and returns a failing status; one invoice's query-step cap does not stop later invoices.
- Global and claimed-supplier ingestion caps, file size and transcript-cited-page limits reject excess before any ERP read. The cited-page test does not establish physical PDF page counting.
- Rate admission waits within budget and stops before an extra send when the wait exceeds budget. Transient attempts consume the call cap and stop after three total attempts; server delays support seconds and HTTP-date values.
- Deterministic denials and malformed calls do not retry. Output-parent symlinks are refused before creation; invoice identity survives logging while account numbers and IBAN values are masked.
- Omitted instruction flags are recovered from curated quote/text evidence. This checks deterministic replay-text screening, not live PDF or image understanding.

## Hardening regressions (`tests/test_hardening.py`)

- The grader rejects a reason code outside the schema enum, or a valid code that does not match the labelled primary exception, on otherwise genuine output.
- A supplier name whose legal form differs from the supplier record (Inc against Ltd) routes to supplier-or-remittance-mismatch in the decider and the checker; the same name with no suffix stays clear.
- Look-alike Cyrillic letters, a declarative remit-to change and a moved treasury account are flagged; benign payment text is not.
- A field quote absent from a `.txt` source quarantines the invoice; the untouched case is written.
- Crafted events piped into the PreToolUse hook are denied: a write tool, a secrets read (including `.ENV`, `.env.staging` and `.envrc`), a protected-path write in any letter case, a write outside the allowed paths, a server alias and malformed input; an allowed read passes through; development mode keeps the write tools, the nested model and the hook log denied.
- Zero-width, soft-hyphen, combining and enclosing characters, a ligature, small capitals and control characters inside an instruction, between its words or both are flagged in the tested spellings, and the quote is cut from the original text.
- A legal form wrapped in punctuation or invisible characters still counts; long spellings equal their short forms.
- `scripts/check.py` reports a settings file without the hook, a missing deny entry and a subagent whose tools exceed its role.
- An IBAN in an extracted free-text field is masked in every output while the PO reference survives; the writer refuses output carrying a full IBAN in any letter case, with any single separator, or glued to a label (check digits verified when glued); the tested hashes, UUID segments and reference numbers that only start like an account are left alone, though a token with a registered length and valid check digits would count as an account.
- A null quote, or a value absent from its own quote, quarantines a `.txt` case; a transcript directory with an unsafe name is quarantined and the batch continues.
- An undeclared query id or a bad variable is rejected client-side before any call; a client ceiling above 1000 calls a minute is refused.

## Not kept

One-file-per-FR organization, duplicated parsing/discount examples already exercised end to end, tests that inspect source code for function names, assertions that restate policy/schema declarations, a grader baseline not produced by the runtime, and extra grader input-format permutations. Tests are not packed into one parameter table to hold the count down.

## Limits and release checks

This is a bounded sandbox replay suite, not exhaustive coverage. Dedicated vendor/currency/tolerance boundary matrices, every policy-cap combination, physical PDF page counting, concurrent writers, additional grader branch permutations and checker amount/source mutation matrices, live OCR/model detection, supplier authentication, distributed rate/ingress quotas, and hostile-local-user filesystem races are not claimed tested. Golden labels and thresholds remain immutable.

Packaging is checked at release time: build an allowlisted ZIP, verify hashes/exclusions, unpack it, then run this same suite and strict checks. Visual copy changes receive render inspection, not new unit tests. The local application hook and production approval controls are separate concerns.

Add a test only for a distinct consequential failure that these scenarios cannot reveal. Prefer extending the relevant observable scenario over a new file or private-function assertion. Test count is not a quality score.
