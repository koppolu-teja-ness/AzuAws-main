# Eval datasets

This folder backs the LangSmith evaluation harness (Phase 2/3 of the tracing +
eval work). `reference/*.json` is the source of truth; `scripts/create_langsmith_dataset.py`
(re)generates it and optionally pushes it to a versioned LangSmith dataset.

## Layout

```
evals/
  datasets/
    reference/*.json      # one file per example -- see schema below
    README.md             # this file
  fixtures/
    adversarial/*.json    # hand-written ARM JSON for synthetic (non-.bicep) examples
```

## Reference file schema

```jsonc
{
  "name": "keyvault",                 // matches the file name
  "example_key": "keyvault-v1",       // stable id used for idempotent LangSmith upserts
  "kind": "seed" | "adversarial",
  "inputs": {                         // regenerated every run -- do not hand-edit
    "all_resource_types": [...],
    "resource_types": [...],          // after Agent 1's noop/foldable filtering
    "unsupported_types": [...],
    "noop_types": [...],
    "foldable_types": [...],
    "source": "resources/keyvault/main.bicep",
    "cnr": { ... }                    // CloudNeutralRepresentation, secrets stripped
  },
  "expected": {                       // hand-reviewed -- preserved across regeneration
    "expected_aws_resource_types": [...],
    "expected_parameter_names": [...],
    "must_be_noecho": [...],
    "forbidden_patterns": [...],
    "expected_min_resource_count": 0,
    "notes": "..."
  }
}
```

`inputs` is produced deterministically by the same code Agents 1-2 use
(`orchestrator/bicep_compiler.py`, `orchestrator/cloud_neutral.py`,
`orchestrator/resource_extractor.py`) -- no LLM call, no AWS call, no human
`input()` prompt. Any `securestring`/`@secure()` parameter default is replaced
with a `**STRIPPED-FOR-DATASET**` placeholder, then the whole structure is
passed through `orchestrator/observability.py::redact()` before it's written
to disk or uploaded, so a real secret can never end up in this folder or in
LangSmith.

`expected` is the part you review and edit by hand. Re-running the generator
only touches `inputs`; it reads any existing file first and carries its
`expected` block over untouched. A brand-new example gets a `TODO` stub
instead of an empty regeneration.

## Regenerating / pushing

```powershell
# Rewrite evals/datasets/reference/*.json from the seed sources; never touches the network.
python scripts/create_langsmith_dataset.py --local-only

# Also push to LangSmith (skipped automatically if LANGSMITH_API_KEY isn't set).
python scripts/create_langsmith_dataset.py --dataset-name bicep-to-cfn-migration-v1
```

Pushing is idempotent: examples are matched by `example_key` in dataset
metadata, so re-running updates existing examples in place instead of
duplicating them. Bump `DATASET_VERSION` in the script (and `example_key`
follows automatically) when you want a new dataset version instead of
overwriting the current one.

CI (`.github/workflows/ci.yml`) runs `--local-only` on every push/PR and fails
if that produces a diff -- i.e. if you change a seed `.bicep`/knowledge_base
doc, regenerate and commit `reference/*.json` in the same change. On pushes to
`main`, CI also pushes to LangSmith if `LANGSMITH_API_KEY` is configured as a
repo secret.

## Adding an example for a new knowledge-base resource type

1. Add the new Azure type -> doc mapping to `knowledge_base/index.json` and
   write the mapping doc (see `bicep-to-cloudformation.md` for the expected
   shape: resource mapping, parameter mapping, property mapping tables).
2. Add a minimal `.bicep` (or hand-written ARM `.json`) source that exercises
   the new type, under `resources/<name>/main.bicep` or
   `evals/fixtures/<name>.json` for synthetic/adversarial cases.
3. Register it in `SEED_SOURCES` in `scripts/create_langsmith_dataset.py`.
4. Run `python scripts/create_langsmith_dataset.py --local-only` -- this
   writes `evals/datasets/reference/<name>.json` with a generated `inputs`
   block and a `TODO` `expected` stub.
5. Fill in `expected` by hand, using the mapping doc as the source of truth
   (resource types, parameter names, which params must be `NoEcho`, any
   patterns that must never appear in generated output, minimum resource
   count).
6. Re-run without `--local-only` (with `LANGSMITH_API_KEY` set) to push it.

## Current examples

| Example | Kind | Covers |
|---|---|---|
| `keyvault` | seed | Key Vault -> Secrets Manager |
| `vpc` | seed | VNet/subnet -> VPC/Subnet |
| `functions` | seed | Function App -> Lambda + IAM role |
| `messaging` | seed | Storage Queue + Service Bus queue/topic/subscription -> SQS/SNS |
| `e2e_full_scope` | seed (+ unsupported-type adversarial) | Key Vault + VNet + Functions together, plus an intentionally out-of-scope `Microsoft.Network/expressRouteCircuits` resource that must be excluded, never mapped |
| `adversarial_missing_property` | adversarial | A Key Vault secret missing `properties.value` -- must not be hallucinated |
| `adversarial_secret_param` | adversarial | A `securestring` parameter with a literal default -- must be stripped before reaching the dataset and must stay `NoEcho` in any generated plan |
