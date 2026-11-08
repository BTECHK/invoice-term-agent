# Diagrams

Sources are the `.mmd` files in this folder. GitHub renders the blocks below directly. More diagrams (system context, containers, data model, clear-case and exception sequences, deployment, eval loop) live in [the architecture document](../04-architecture.md), and the trust boundary is in [the threat model](../06-threat-model.md).

## Harness: where each control sits

```mermaid
flowchart TB
  subgraph CTX[Context: read by every coding agent, not enforcement]
    A1[AGENTS.md<br/>shared by CLAUDE.md and GEMINI.md<br/>anti-rationalization table]
    A2[skills/*/SKILL.md<br/>seven exception playbooks;<br/>runtime reads only their queries line]
    A3[.claude/agents<br/>extractor: no MCP, 3 turns<br/>checker: read-only MCP, 8 turns]
  end
  subgraph POL[Policy: one source of truth]
    P1[policy/tool-policy.yaml<br/>roles, allow and deny lists,<br/>denied_everywhere, write paths, caps]
    P2[policy/term-rules.yaml<br/>payment-term rules]
    P3[policy/models.yaml<br/>tier routing, different-family checker]
  end
  subgraph CC[Enforcement, coding-agent path]
    H1[.claude/hooks/pre-tool-use.py<br/>deny by default, fails closed,<br/>casefolded paths, hash-audited]
    H2[.claude/settings.json<br/>five read tools allowed;<br/>three writes and the nested<br/>model denied]
    H4[mock_erp/server.py<br/>declared query ids only,<br/>fullmatch on variables,<br/>writes always refused]
  end
  subgraph PY[Enforcement, Python runner path]
    H3[erp_client.py<br/>validated read methods only,<br/>variable patterns, throttle, retries]
  end
  subgraph RUN[Runtime checks]
    R1[guard.py<br/>injection scan after Unicode fold,<br/>account masking]
    R2[schemas + models.py<br/>additionalProperties false]
    R3[propose.py checker<br/>independent recompute,<br/>legal-form rule]
    R4[worklist.py<br/>journaled atomic commit,<br/>refuses output with a full IBAN]
  end
  subgraph GATE[Gates before merge]
    G1[scripts/check.py<br/>policy vs settings vs contract vs agents,<br/>hook wired, skill mirrors, pins, secrets]
    G2[evals/grade.py<br/>golden and injection cases,<br/>six gates at 1.0, reason-code enum]
    G3[tests/<br/>78 behaviour, hardening and reader tests:<br/>CLI, MCP, hook, guard, grader, OCR]
    G4[CI: tests, check, gitleaks over history,<br/>zizmor, SHA-pinned actions, hash-locked deps]
  end
  P1 --> H1
  P1 --> H2
  P1 --> G1
  H2 --> G1
  A3 --> G1
  P2 --> R3
  H1 --> H4
  H3 --> H4
  R2 --> G2
  R3 --> G3
  R1 --> G3
  R4 --> G3
  G1 --> G4
  G2 --> G4
  G3 --> G4
```

## Pipeline: one invoice through the sandbox

```mermaid
flowchart TD
  A[Invoice PDF or image] --> R[Local reader: text layer or RapidOCR, transcript with reading]
  R --> B[Validate source hash, fields and envelope]
  B -->|Valid| C[Read ERP facts through fixed queries]
  B -->|Invalid| X[Audit the technical stop and quarantine the invoice]
  C --> D[Apply deterministic payment-term rules]
  D --> E[Checker re-reads facts and recomputes]
  E -->|Clear and agrees| F[Stage checked proposal and audit event]
  E -->|Hold or disagreement| G[Stage null payment day and open a worklist record]
  F --> L[(Ingestion-ready records for ERP and BI)]
  G --> L
  G -. Design .-> H[Person resolves the issue and rechecks]
  L -. Design .-> I[AP approval, approved ERP update, payment release]
  H -. Design .-> I
```

## Runtime: one process, one MCP subprocess

```mermaid
flowchart TD
  IN[Invoice file] --> RD["Local reader (reader.py)"]
  subgraph R[Python runner: one process]
    L[Replay loader and validator] --> D[Rules and proposal]
    D --> C[Separate checker implementation]
    C --> E[Exception resolver when held]
    C --> W[Bundle writer: lock, journal, idempotency]
    E --> W
  end
  RD --> L
  D <-->|Fixed queries via adapter| M
  C <-->|Re-query via adapter| M
  E <-->|Declared evidence queries| M
  M[Mock ERP data service: MCP over stdio] <--> Y[Fixtures + tool allowlist; writes refused]
  W --> O[Proposals, worklist records, audit events, run summary]
```

## Target process: where the agent fits in accounts payable

```mermaid
flowchart LR
  classDef auto fill:#F4F5F7,stroke:#C9CDD4,color:#1F2937
  classDef human fill:#E8F1EC,stroke:#2F7D57,color:#1F2937
  classDef poc fill:#FFFFFF,stroke:#2F7D57,stroke-width:2px,color:#1F2937
  classDef erp fill:#FFFFFF,stroke:#6B7280,color:#1F2937

  A([Invoice arrives<br/>PDF, image or e-invoice]):::auto
  B[1  Read the invoice<br/>seven fields with<br/>quotes and page cites]:::poc
  C[2  Match in the ERP data<br/>order, goods receipt,<br/>vendor master]:::auto
  D{3  Do the terms agree,<br/>or is a validated<br/>term on file?}:::auto
  E[4  Propose the date<br/>longest valid term;<br/>discount only when it beats<br/>the cost of capital]:::poc
  F[Exception agent<br/>pulls vendor history and<br/>past decisions, drafts a<br/>recommendation with evidence]:::poc
  G[AP worklist<br/>a person decides]:::human
  H[Approval form<br/>Head of AP or delegate]:::human
  I[Approved ERP action<br/>updates the SAP<br/>payment term or baseline date]:::erp
  J([Payment run<br/>released by an<br/>authorized person]):::human

  A --> B --> C --> D
  D -- "clear case<br/>(rules decide)" --> E --> H
  D -- "ambiguous" --> F --> G --> H
  H --> I --> J
```
