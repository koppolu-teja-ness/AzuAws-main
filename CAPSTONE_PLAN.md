# Plan: Agentic Azure→AWS IaC Migration Capstone (LangGraph)

## Use case / differentiation (for write-up)
Existing tools (Former2, cf-terraform, Azure Migrate) either reverse-engineer FROM a live
cloud account or move workloads WITHIN one cloud. None do source-code-level cross-cloud
IaC translation (Bicep→CFN) with self-correcting generation,
scoring, security guardrails, and gated autonomous deployment. Differentiators to write up:
1. Deterministic-plan / non-deterministic-reasoning split (LLM never emits template syntax
   directly — mirrors existing pilot's MigrationPlan JSON approach) → auditable, replayable.
2. Guardrail gate + human-in-the-loop approval before any real AWS mutation.
3. Self-extending knowledge base seeded from one human-authored doc (Key Vault), agent
   drafts new docs for VPC/Functions, human reviews before they become "trusted".
4. Secrets never hit disk/logs in plaintext — masked wrapper + redaction filter.

## Confirmed decisions (user, 2026-09-23)
- Scope: exactly 3 resources — Key Vault, VPC, Functions (Azure Functions → AWS Lambda).

> **Update (2026-10-04):** user asked to deepen VNet/Functions coverage (NSGs, route
> tables, NAT gateways; Lambda trigger types + packaging workflows) and then add new
> resource families incrementally — the "exactly 3 resources" scope above is being
> expanded, not held to going forward. First new family added: standalone Blob Storage
> (`Microsoft.Storage/storageAccounts/blobServices/containers` -> `AWS::S3::Bucket`,
> `knowledge_base/storage-to-cloudformation.md`), kept distinct from the pre-existing
> Functions-coupled storage account (deployment bucket, no resource created) via the
> container child type rather than reusing the already-registered bare account type.

- Fresh redesign of control flow as a multi-agent LangGraph graph — but REUSE the existing
  deterministic building blocks in orchestrator/ (bicep_compiler, resource_extractor,
  knowledge_base, cloud_neutral, migration_plan, cfn_generator, validator) as tool
  functions/nodes rather than rewriting them from scratch.
- Orchestration framework: LangGraph.
- Deployment: agent runs actual `aws cloudformation deploy`/boto3, but only after a human
  approval gate (interrupt) — not fully autonomous.

> **Update (2026-09-30):** in the existing pilot's `migrate_agents.py` graph (not this
> planned Phase D redesign), the deploy-confirmation gate (`deploy_gate`) and the
> interactive per-parameter `input()`/`getpass()` prompts were removed at the user's
> request — Agent 6 now deploys fully automatically once `plan_approval_gate` and
> `stack_check_gate` pass, resolving parameter values from `--params-file` /
> `CFN_PARAM_<NAME>` env vars / a matching source Key Vault secret / template `Default` /
> (naming-param heuristic only) the source resource group or vault name — see README.md's
> [Non-interactive parameter resolution](README.md#non-interactive-parameter-resolution).
> `stack_check_gate` is unchanged (still interactive). Whoever picks up Phase D
> (`orchestrator/deploy.py`, "human-approval gated") should decide whether the redesigned
> graph keeps that requirement or adopts the pilot's fully-automatic model before building it,
> to avoid rebuilding a gate that was deliberately removed.
- Deliverable framing: academic/portfolio capstone — needs strong evaluation section
  (metrics, calibration, differentiation narrative), not just working code.
- Secrets: agent MAY transport actual secret values end-to-end, but must mask them in all
  logs/state dumps and never write plaintext to disk; only human-approved deploy step
  unwraps the real value at the AWS API boundary.
- Manual markdown doc precedent exists only for Key Vault
  (bicep-to-cloudformation.md) — VPC and Functions docs are agent-drafted, human-reviewed
  before being trusted as knowledge-base entries (this asymmetry is itself a capstone
  talking point: "seed-and-generalize" KB growth).

## Architecture — LangGraph state graph

**State (MigrationState, single dataclass/TypedDict)**: bicep_path, arm_json, resource_types,
kb_coverage, cnr, migration_plan, cfn_yaml, lint_findings,
guardrail_findings, human_decision, deploy_result, verification_result,
run_id, redacted_secret_refs{} (real values held in a separate in-memory-only
`SecretVault` object keyed by ref, never serialized into state/log).

**Nodes** (each a function wired into the graph; solid arrows = normal path, dashed = retry):
1. `compile_node` — wraps `bicep_compiler.py` (existing, deterministic).
2. `extract_node` — wraps `resource_extractor.py` + `knowledge_base.py`; on unmapped type,
   routes to `kb_draft_node` instead of failing hard (new behavior vs today's hard exit).
3. `kb_draft_node` (new) — LLM drafts a candidate mapping doc for an unmapped resource type
   using the Key Vault doc as a few-shot template; writes to
   `knowledge_base/drafts/<type>.md`; routes to a human-review interrupt before promoting
   the draft into `knowledge_base/index.json`.
4. `cnr_node` — wraps `cloud_neutral.build_cnr()` (existing, deterministic).
5. `plan_node` — wraps `generator.py` + `migration_plan.py`.
6. `render_node` — wraps `cfn_generator.generate_cloudformation()` (existing, deterministic,
   no LLM) + new `_scrub_secret_literals()` guard that hard-fails if a real secret value
   leaked into rendered YAML instead of a `!Ref`/`NoEcho` parameter.
7. `validate_node` (extended) — existing `cfn-lint` call + NEW `checkov`/`cfn_nag` security
   scan + custom guardrail checks (see Guardrails below). Produces `lint_findings` +
   `guardrail_findings`.
8. **Retry edge**: if validate_node fails and attempts < `max_fix_attempts`, loop back to
   `plan_node` with the failure feedback appended (same self-correction pattern as today).
9. `guardrail_gate` (new) — if any guardrail is hard-blocking, force
  `human_review_node`; otherwise still route through it if `--auto` not passed
  (default: always require approval per user decision).
10. `human_review_node` (LangGraph `interrupt()`) — renders a CLI table (rich):
  validator/guardrail status, diff of generated YAML, estimated AWS resources
    to be created. Human answers approve / reject / edit-and-retry.
11. `deploy_node` (new, `orchestrator/deploy.py`) — boto3 `cloudformation.create_stack`/
    `update_stack` (not `deploy` CLI subprocess, to avoid secret values touching argv/shell
    history); pulls real values from `SecretVault` only at this boundary; masked in any
    logging via a logging filter (`orchestrator/secrets_handling.py`).
12. `verify_node` (new, `orchestrator/verify.py`) — post-deploy smoke tests per resource type:
    Key Vault → `describe-secret`/`get-secret-value` existence check (value itself never
    logged); VPC → `describe-vpcs`/`describe-subnets` reachability check; Functions →
    `lambda invoke` with a no-op test payload, check `StatusCode`.
13. `report_node` (new, `orchestrator/evaluation.py`) — appends run outcome to
  `output/runs/history.jsonl` and writes a
    human-readable evaluation report (used for the capstone metrics section).

**Graph shape**: linear spine (1→2→4→5→6→7) with two loop-back edges
(7/8 → 5 for self-correction; 3 → human review → 2 for KB draft approval), then a gate
(9) before the "real world" tail (10→11→12→13).

```mermaid
flowchart TD
    A[compile_node] --> B[extract_node]
    B -- unmapped type --> C[kb_draft_node]
    C --> HR1{{human review:\napprove KB draft}}
    HR1 -- approved --> B
    B -- all types mapped --> D[cnr_node]
    D --> E[plan_node\nLLM migration plan]
    E --> F[render_node\nscrub secret literals]
    F --> G["validate_node\ncfn-lint + checkov + guardrails"]
    G -- fail / attempts left --> E
    G -- pass or attempts exhausted --> I{guardrail_gate\nblocking finding?}
    I -- yes --> J[[human_review_node\ninterrupt: approve/reject/edit]]
    I -- no --> J
    J -- reject --> Z[Stop]
    J -- approve --> K[deploy_node\nboto3 create/update stack]
    K --> L[verify_node\nper-resource smoke test]
    L --> M[report_node\nhistory.jsonl + evaluation report]

    style C fill:#e2e3ff,stroke:#5b5bd6
    style E fill:#e2e3ff,stroke:#5b5bd6
    style HR1 fill:#fff3cd,stroke:#b8860b
    style J fill:#fff3cd,stroke:#b8860b
    style Z fill:#f8d7da,stroke:#c0392b
    style M fill:#d4edda,stroke:#2e7d32
```

## Guardrails (`orchestrator/guardrails.py`, new)

> **Update (2026-10-04):** this module now exists and is wired into the existing pilot's
> graph (`orchestrator/graph.py`'s `guardrail_scan_gate`, between `agent5_validate_cfn` and
> `stack_check_gate`) — see README.md's
> [Guardrail security scan](README.md#guardrail-security-scan). It implements `checkov`
> (chosen over `cfn_nag` for pure-Python/no-Ruby install, per the recommendation below)
> plus the hardcoded-secret, IAM-wildcard, and open-network-ingress custom checks described
> here. Severities are `HIGH`/`CRITICAL` (blocking -- forces the human gate) vs.
> `MEDIUM`/`LOW` (advisory, matching this doc's `block`/`warn` vocabulary). Not yet built:
> the `DeletionPolicy` check below, and this doc's broader Phase D redesign
> (`deploy_node`, `verify_node` as separate new nodes) --
> the pilot's existing `agent6_deploy`/`_verify_secrets` already cover some of that ground
> differently.

- Static: cfn-lint (existing), checkov or cfn_nag security scan (new dependency).
- Custom AST/regex checks on rendered YAML:
  - No literal secret values outside `NoEcho` parameters / `!Ref`.
  - No IAM policy statement with `Action: "*"` or `Resource: "*"` for Functions execution
    role.
  - No security group / NACL rule with `0.0.0.0/0` on a non-HTTP(S) port for VPC.
  - No `DeletionPolicy` missing on stateful resources (Secrets, potentially S3 for Lambda
    artifacts) — warn if not `Retain`/`Snapshot` where relevant.
- Guardrail severities: `block` (forces human_review + surfaced prominently) vs `warn`
  (shown but doesn't force gate).

## Secrets handling (`orchestrator/secrets_handling.py`, new)
- `SecretValue` wrapper class: `__repr__`/`__str__` return `"***"`; `.reveal()` explicit
  method only called inside `deploy_node`.
- Logging filter installed on the root logger that redacts any string matching known
  secret refs before it reaches a handler (belt-and-suspenders in addition to the wrapper).
- Values sourced via `getpass`/env var at runtime, never via CLI flags (avoids shell
  history / process list exposure) — deploy uses boto3 parameter dict, not
  `aws cloudformation deploy --parameter-overrides` subprocess string.

> **Update (2026-09-30):** the pilot dropped `getpass` entirely (see the Phase D note
> above) — non-secret and secret parameter values are now both sourced non-interactively
> (`--params-file` / `CFN_PARAM_<NAME>` / source Key Vault / template Default / source
> name), still never via CLI flags for the same shell-history/process-list reason.

## New source content required (parallel with graph build)
- `resources/vpc/main.bicep` (Microsoft.Network/virtualNetworks + subnets + NSG) — target
  AWS::EC2::VPC + Subnets + SecurityGroup.
- `resources/functions/main.bicep` (Microsoft.Web/sites kind=functionapp + serverfarms +
  storage account) — target AWS::Lambda::Function + IAM::Role + (+API Gateway if HTTP
  trigger) + S3 bucket for deployment package.
- `knowledge_base/vpc-to-cloudformation.md`, `knowledge_base/functions-to-cloudformation.md`
  — agent-drafted (via kb_draft_node), human-reviewed, following the existing Key Vault doc
  structure (concept diff table, resource/param/property mapping, commands, gotchas).
- Update `knowledge_base/index.json` with the two new ARM type → doc mappings once approved.

## Tech stack
- Orchestration: LangGraph (Python) — StateGraph + `interrupt()` for human-in-the-loop.
- LLM: keep AWS Bedrock via existing `Generator` ABC (`orchestrator/generator.py`), reused
  as a LangGraph node/tool call, not replaced.
- Guardrail scanning: `checkov` (or `cfn_nag`) added to requirements.txt alongside existing
  `cfn-lint`.
- Deploy/verify: `boto3` (cloudformation, secretsmanager, ec2, lambda clients) — already a
  dependency.
- CLI review UX: `rich` (new dependency) for the human-review table/diff.
- Persistence: flat files — `output/runs/<run_id>/` per-run artifacts + evaluation report,
  `output/runs/history.jsonl` for run history. No DB needed at this scale.
- Testing: `pytest` for deterministic nodes/guardrails; recorded-response
  fixtures for the LLM plan node to test the self-correction loop without live Bedrock calls.

## Work assignment (2 engineers: Charan, Saurav)

Split along the natural Phase A/B (content + graph core) vs. Phase C/D (guardrails,
secrets, deploy) boundary so each person can work mostly independently once Phase B step 3
lands. Phase E is shared (each owns the half of the evaluation/report data driven by their
own track).

| Owner | Track | Phases |
|---|---|---|
| **Charan** | Content + Graph Core | Phase A (steps 1–2), Phase B (steps 3–5) |
| **Saurav** | Guardrails, Secrets, Deploy | Phase C (steps 6–7), Phase D (steps 8–10) |
| **Both** | Evaluation & Docs | Phase E (steps 12–14) — Saurav drives `evaluation.py` + sample runs (12–13, needs his deploy/verify code); Charan drives the README/architecture doc update (14) |

## Phases / steps
**Phase A — Content (parallel with Phase B)** — Owner: **Charan**
1. Author `resources/vpc/main.bicep` and `resources/functions/main.bicep`.
2. Run `kb_draft_node` flow (or manually bootstrap) to produce the two new KB docs; human
   review; update `index.json`.

**Phase B — Graph core** (*depends on nothing from Phase A to start scaffolding*) — Owner: **Charan**
3. Build `orchestrator/graph.py`: MigrationState + node wrappers around existing
   bicep_compiler/resource_extractor/knowledge_base/cloud_neutral/migration_plan/
   cfn_generator/validator modules (thin adapters, no logic rewrite).
4. Add `kb_draft_node` + human-review interrupt for new resource types (*depends on 3*).
5. Extend `migration_plan.py` schema + `PLAN_JSON_SCHEMA_HINT` as needed for guardrail-driven retries (*depends on 3*).

**Phase C — Guardrails** (*depends on Phase B step 3*) — Owner: **Saurav**
6. `orchestrator/guardrails.py` — checkov integration + custom checks; wire into
   `validate_node`.
7. `guardrail_gate` node wiring guardrail severity → forced human review.

**Phase D — Secrets & Deploy** (*depends on Phase C*) — Owner: **Saurav**
8. `orchestrator/secrets_handling.py` — SecretValue wrapper + logging redaction filter.
9. `orchestrator/deploy.py` — boto3-based create/update stack. **Note (2026-09-30):** the
    pilot's `agent6_deploy` no longer gates on human approval or interactive parameter
    entry (see the update note under "Confirmed decisions" above) — confirm with the team
    whether this redesign should keep a human-approval gate or match the pilot's
    fully-automatic model before implementing.
10. `orchestrator/verify.py` — per-resource-type post-deploy smoke tests.

**Phase E — Evaluation & Docs** (*depends on D; can start report scaffolding earlier*) — Owner: **Both**
12. `orchestrator/evaluation.py` — run-history logging + report generation (pass rate,
    calibration/Brier score, human-intervention rate, time-to-migrate). *(Saurav)*

> **Update (2026-10-05):** step 12 now exists in the pilot — `agent7_report` (the real
> graph's universal terminal node, not a separate Phase D node) calls
> `orchestrator/evaluation.py`'s `log_run_outcome()`/`write_calibration_report()` after
> every real run, appending to `output/runs/history.jsonl` and regenerating
> `output/runs/calibration_report.md` (pass rate, human-intervention rate, deploy success
> rate, mean/median time-to-migrate, Brier score). The Brier score compares actual outcomes
> against `predict_success_probability()` — a standalone
> historical-success-rate proxy (mean outcome of past runs sharing a resource type,
> defaulting to an uninformative 0.5 prior). See README.md's
> [Run history & calibration report](README.md#run-history--calibration-report).

13. Run N end-to-end migrations across the 3 resource types to populate real evaluation
    data. *(Saurav, with Charan's VPC/Functions content from Phase A)*
14. Update README.md (new architecture, new resources) + write the differentiation/use-case
    section using the framing above; keep bicep-to-cloudformation.md as the single manual
    reference doc as originally planned. *(Charan)*

## Verification

| # | Check | Owner |
|---|---|---|
| 1 | `pytest` covering: guardrail custom checks (unit), cfn_generator secret-literal scrub (unit), migration_plan schema validation (unit) | Split: Charan writes the `cfn_generator`/`migration_plan` unit tests, Saurav writes the guardrail unit tests |
| 2 | Dry-run style check (no LLM/AWS calls) for all 3 resource types: compile→extract→CNR only, confirming KB coverage after Phase A | Charan |
| 3 | Full graph run (LLM + guardrails, no deploy) for each resource type; confirm guardrail_gate triggers human_review as expected on a deliberately bad plan fixture (e.g. inject an IAM `*` action) to prove the gate works | Saurav |
| 4 | One real gated deploy + verify + rollback (`delete-stack`) cycle per resource type in a sandbox AWS account, with explicit user confirmation before running (deploy is a shared/costly action) | Saurav |
| 5 | Evaluation report reviewed for calibration sanity (predicted success probability vs actual outcome across the sample runs) | Both |

## Further considerations
1. Repo/folder is still named `az_key_vault` though scope now spans 3 services — cosmetic,
   left as-is unless you want it renamed (rename is a manual/destructive-ish action, would
   ask separately before doing).
2. Functions HTTP-trigger → API Gateway mapping adds meaningful scope (auth, CORS, stages).
   Recommend starting with a simple timer/queue-triggered function (Lambda-only, no API
   Gateway) for the MVP resource, and treating HTTP-trigger+API-Gateway as a stretch goal.
3. `checkov` vs `cfn_nag`: recommend `checkov` (pure Python, easier install on Windows,
   already ecosystem-aligned with pyyaml/boto3 stack) unless you have a reason to prefer
   `cfn_nag` (Ruby-based).
