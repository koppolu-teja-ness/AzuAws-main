# Evaluation Components Guide

This document explains every evaluation component in the project, including:

- dataset preparation
- evaluation targets
- evaluator metrics
- scoring behavior
- output artifacts
- real-run calibration metrics

The implementation lives primarily in:

- `evals/run_eval.py`
- `orchestrator/evaluation.py`
- `scripts/create_langsmith_dataset.py`


## 1) Evaluation Layers

The project has two evaluation layers:

1. **Harness evaluation (benchmark style)**
   - Runs curated examples from `evals/datasets/reference/*.json`
   - Produces per-example metric scores
   - Used to compare model, prompt, and RAG behavior

2. **Run-history calibration (production run analytics)**
   - Logs outcomes from real `migrate_agents.py` runs
   - Computes aggregate metrics like pass rate and Brier score
   - Used to track historical reliability over time


## 2) Dataset Components

### 2.1 Reference examples

Each reference file contains:

- `inputs`: deterministic data (resource types, ARM template, CNR)
- `expected`: human-reviewed expectations for scoring

Key `expected` fields:

- `expected_aws_resource_types`
- `must_be_noecho`
- `forbidden_patterns`

### 2.2 Dataset generation

`scripts/create_langsmith_dataset.py` regenerates `inputs` deterministically from seed Bicep/ARM sources.

Important behavior:

- no LLM call
- no AWS deployment call
- secret stripping and redaction before writing
- preserves human-authored `expected` blocks on reruns


## 3) Target Components (What Gets Evaluated)

The harness can evaluate two targets.

### 3.1 `target_agent3`

Built by `build_target_agent3(...)`.

Flow:

1. reconstruct CNR from dataset input
2. build KB query
3. retrieve mapping docs (with retrieval diagnostics)
4. build prompt
5. call generator (Bedrock)
6. parse migration plan
7. return plain output dict for evaluators

Returned fields include:

- `migration_plan`
- `migration_plan_raw`
- `error`
- `latency_ms`
- `rag_diagnostics`

### 3.2 `target_pipeline`

Built by `build_target_pipeline(...)` + `build_eval_graph(...)`.

This uses a trimmed Agent 1 to Agent 5 graph with retry loop, but **never includes deploy-adjacent nodes**.

Key safety design:

- `agent6_deploy`, `stack_check_gate`, and `agent7_report` are not added to the eval graph
- plan gate is auto-approved in eval mode (no human prompt)

Returned fields include:

- `stopped`, `stop_reason`
- `lint_passed`
- `fix_attempts`
- `validation_history`
- `mapping_table`
- `migration_plan`
- `node_path`


## 4) Evaluator Components (Metrics)

All evaluator functions consume `outputs` and sometimes `reference_outputs`.

### 4.1 `plan_schema_valid`

Purpose:

- checks that a migration plan exists and has valid `resources` shape

Score:

- `1.0` if valid
- `0.0` otherwise

Failure examples:

- no plan produced
- `resources` is not a list
- missing `logical_id` or `aws_type`


### 4.2 `resource_type_accuracy`

Purpose:

- checks coverage of expected AWS resource types

Score formula:

- `|expected ∩ actual| / |expected|`

Behavior:

- if no expected types are declared, score is `1.0`


### 4.3 `parameter_hygiene`

Purpose:

- validates secret-parameter safety and parameter-default correctness

Checks:

- required `NoEcho` parameters exist
- required `NoEcho` parameters are actually `NoEcho`
- `NoEcho` parameters do not have non-empty literal defaults
- defaults satisfy their own constraints

Score:

- `1.0` when no violations
- `0.0` when any violation exists


### 4.4 `forbidden_patterns_absent`

Purpose:

- ensures output does not contain forbidden regex patterns

Input:

- `reference_outputs.forbidden_patterns`

Score:

- `1.0` if no pattern matches
- `0.0` if any pattern matches


### 4.5 `cfn_lint_clean`

Purpose:

- checks lint pass/fail for pipeline target outputs

Score:

- `1.0` when `lint_passed = true`
- `0.0` when `lint_passed = false`
- `None` when not applicable (for Agent 3-only target)


### 4.6 `lint_attempts`

Purpose:

- exposes retry behavior as two sub-metrics

Produces:

- `first_try_lint_pass` (`1.0` or `0.0`)
- `fix_attempts_used` (numeric count)

Applicability:

- returns `None` scores when no validation history exists


### 4.7 `trajectory_valid`

Built by `build_trajectory_valid(max_fix_attempts)`.

Purpose:

- enforces structural rules for eval pipeline trajectories

Checks:

- node path never reaches deploy-adjacent nodes
- all visited nodes are in an allowlist
- retry count does not exceed `max_fix_attempts`

Score:

- `1.0` if trajectory is valid
- `0.0` if any rule is violated
- `None` when not applicable


### 4.8 `latency_ms`

Purpose:

- records generation latency from Agent 3 target

Score:

- numeric latency in milliseconds
- `None` if latency is not recorded


### 4.9 `retrieval_quality`

Purpose:

- scores RAG retrieval quality independently of generation quality

Uses `rag_diagnostics` and checks:

- retrieval errors
- fallback usage
- off-source chunk contamination
- zero retrieved chunk count when not in fallback

Scoring behavior:

- `None` if diagnostics are missing
- `0.0` for hard violations
- otherwise `1.0 - fallback_rate`, rounded to 3 decimals

Interpretation:

- high score means targeted retrieval worked
- lower non-zero score usually means fallbacks were used
- zero score means retrieval quality failure


### 4.10 `mapping_fidelity` (LLM judge)

Built by `build_mapping_fidelity(...)`.

Purpose:

- asks a separate judge model how faithfully the plan follows mappings/source

Judge output contract:

- JSON with score in `{0, 0.5, 1}` and rationale

Score handling:

- returns numeric score if parsed successfully
- returns `None` (not `0`) if judge is unavailable or output unparseable

Design intent:

- avoid penalizing offline/CI environments for judge availability issues


## 5) Evaluator Bundles by Target

### 5.1 Agent 3 evaluator bundle

`build_agent3_evaluators(...)` includes:

- `plan_schema_valid`
- `resource_type_accuracy`
- `parameter_hygiene`
- `forbidden_patterns_absent`
- `latency_ms`
- `retrieval_quality`
- `mapping_fidelity`

### 5.2 Pipeline evaluator bundle

`build_pipeline_evaluators(...)` includes:

- `plan_schema_valid`
- `resource_type_accuracy`
- `parameter_hygiene`
- `forbidden_patterns_absent`
- `cfn_lint_clean`
- `lint_attempts`
- `trajectory_valid`


## 6) Runner Components (`evals/run_eval.py`)

### 6.1 Execution modes

- `--local-only`
  - fully offline against `evals/datasets/reference/*.json`
  - no LangSmith dependency

- online mode
  - requires dataset name and LangSmith API key
  - uses `langsmith.evaluate(...)`

### 6.2 Controls

- `--target {agent3,pipeline}`
- `--prompt-version` (affects Agent 3 target)
- `--model-id`
- `--rag/--no-rag`
- `--top-k`
- `--repetitions`
- `--limit`

### 6.3 Result serialization

For each row/repetition, runner stores:

- example id/name
- outputs dict
- flattened evaluator results


## 7) Summary Components

The runner writes two files under `output/evals/<experiment>/`:

- `results.jsonl`: per-row detailed results
- `summary.md`: aggregate report

Summary calculations:

- per-row fail if any metric score is numeric and `< 1.0`
- pass rate = passed rows / total rows
- per-metric mean score over all scored rows
- sample of first 10 failing rows with failure details


## 8) Real-Run Calibration Components (Production Analytics)

These are distinct from harness evals and are used for real `migrate_agents.py` runs.

### 8.1 `predict_success_probability`

Purpose:

- predicts current run success probability from historical runs

Method:

- mean `actual_outcome` of prior runs sharing at least one resource type
- defaults to `0.5` if no matching history exists


### 8.2 `log_run_outcome`

Purpose:

- appends one JSON line per completed real run

Fields logged include:

- run metadata and resource types
- completion and stop reason
- lint/deploy outcomes
- fix attempts
- human intervention info
- predicted vs actual outcome
- duration


### 8.3 `build_calibration_report`

Aggregates run history into:

- run count
- pass rate
- human-intervention rate
- deploy success rate (attempted deploys only)
- mean and median time-to-migrate
- Brier score

Brier score formula:

- mean of `(predicted_success_probability - actual_outcome)^2`


### 8.4 `render_calibration_report_markdown` and `write_calibration_report`

Purpose:

- generate and write `output/runs/calibration_report.md`

Behavior:

- if no history exists, emits a clear no-data message


## 9) Prompt Evaluation Specifics

Prompt comparison is done by running Agent 3 target with different prompt modules (`v1`, `v2`, etc.) while keeping dataset and eval metrics fixed.

How it is wired:

- prompt module selected by `--prompt-version`
- selected module provides both:
  - `SYSTEM_PROMPT`
  - `build_migration_plan_prompt(...)`

For fair comparison:

- use same dataset
- use same model and RAG settings
- use same repetitions


## 10) Interpretation Guidelines

- **Safety regressions are highest priority**: `parameter_hygiene` and `forbidden_patterns_absent` should remain perfect.
- **Structural validity next**: `plan_schema_valid` should remain perfect.
- **Mapping quality then**: `resource_type_accuracy`, `mapping_fidelity`.
- **Retrieval health is diagnostic**: `retrieval_quality` helps isolate RAG issues from generation issues.
- **Latency is optimization, not correctness**: use `latency_ms` as a trade-off metric after quality/safety checks.


## 11) Where to Look in Code

- Target builders: `build_target_agent3`, `build_target_pipeline`
- Eval graph: `build_eval_graph`
- Metric functions: `plan_schema_valid`, `resource_type_accuracy`, `parameter_hygiene`, `forbidden_patterns_absent`, `cfn_lint_clean`, `lint_attempts`, `build_trajectory_valid`, `latency_ms`, `retrieval_quality`, `build_mapping_fidelity`
- Bundles: `build_agent3_evaluators`, `build_pipeline_evaluators`
- Real-run calibration: `predict_success_probability`, `log_run_outcome`, `build_calibration_report`, `write_calibration_report`
