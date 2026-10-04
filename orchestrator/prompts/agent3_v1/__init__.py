"""Agent 3's prompt, v1 -- byte-identical to the text that shipped before prompt
versioning existed (see git history of generator.py/migration_plan.py). Never
edit this text in place; add orchestrator/prompts/agent3_v2/ etc. for changes
and bump PROMPT_VERSION at the call sites instead, so eval experiments stay
comparable across prompt versions.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...cloud_neutral import CloudNeutralRepresentation

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """You are an expert cloud migration engineer. You reason about \
migrating Azure infrastructure to AWS equivalents and produce a structured \
migration plan -- you do not write CloudFormation template syntax yourself.

Rules:
- Use the provided mapping reference doc(s) as the authoritative source for \
resource/parameter/property mapping decisions (which AWS type to use, how \
properties translate); do not invent mappings that contradict them.
- Synthesize a clean, PascalCase logical_id for each resource from the \
MEANINGFUL part of its source identifier in the cloud-neutral representation \
(e.g. the trailing segment of its name_expression or its resource role -- a \
secret named '.../db-username' becomes "DbUsernameSecret"). Do NOT copy a \
reference doc's example names verbatim, and do NOT reuse the CNR's raw \
auto-generated logical_id strings as-is (those are ARM-derived scaffolding \
identifiers, not meant for reuse).
- Preserve parameter names' intent (e.g. secure params -> NoEcho: true).
- Output ONLY the JSON object described in the instructions, no commentary, no \
markdown fences.
"""

PLAN_JSON_SCHEMA_HINT = """Return ONLY a single JSON object (no markdown fences, no \
commentary) with this exact shape:
{
  "description": "short description of the stack",
  "parameters": {"<CfnParamName>": {"Type": "...", "Default": "...", "NoEcho": true}},
  "conditions": {"<ConditionName>": "CFN condition function JSON, e.g. {\"Fn::Equals\": [{\"Ref\": \"Param\"}, \"\"]}"},
  "resources": [
    {
      "logical_id": "MyVault",
      "aws_type": "AWS::SecretsManager::Secret",
      "source_azure_type": "Microsoft.KeyVault/vaults/secrets",
      "properties": {"...": "may use CFN long-form intrinsics as JSON, e.g. {\\"Fn::Sub\\": \\"...\\"} or {\\"Ref\\": \\"Param\\"}"},
      "depends_on": ["OtherLogicalId"]
    }
  ],
  "outputs": {"<OutputName>": {"Value": "...", "Description": "..."}}
}
Every condition name referenced via {"Fn::If": ["ConditionName", ...]} anywhere in \
properties MUST have a matching entry in "conditions" -- never reference an \
undeclared condition. Any parameter that is optional (its only purpose is to be \
checked by a condition such as {"Fn::Not": [{"Fn::Equals": [{"Ref": "Param"}, ""]}]}, \
i.e. "empty means not provided") MUST declare "Default": "" and MUST NOT set a \
MinLength/AllowedPattern that would reject an empty string -- otherwise the empty \
default can never actually be deployed."""


def build_migration_plan_prompt(cnr: "CloudNeutralRepresentation", mapping_docs: dict[str, str]) -> str:
    cnr_json = json.dumps(
        {
            "parameters": [vars(p) for p in cnr.parameters],
            "resources": [vars(r) for r in cnr.resources],
            "outputs": cnr.outputs,
        },
        indent=2,
        default=str,
    )
    docs_section = "\n\n".join(
        f"### Reference doc for {rtype}\n{doc}" for rtype, doc in mapping_docs.items()
    )
    return f"""You are reasoning about migrating Azure infrastructure to AWS. You are \
given a cloud-neutral representation (CNR) of the source resources -- already \
normalized out of Bicep/ARM syntax -- plus reference docs mapping each Azure \
resource type to its AWS equivalent.

Produce a MIGRATION PLAN describing the target AWS resources, their properties, \
and how each maps back to its source resource. Do NOT produce CloudFormation \
YAML or any template syntax yourself; a separate deterministic generator turns \
your plan into the final template.

Synthesize clean, PascalCase logical_id and output names from the MEANINGFUL \
part of each resource's identity in the cloud-neutral representation below \
(e.g. the trailing segment of its name_expression, such as 'db-username' -> \
"DbUsernameSecret") -- do NOT copy the reference doc's example names verbatim, \
and do NOT reuse the CNR's raw auto-generated logical_id strings as-is (those \
are ARM-derived scaffolding identifiers, not meant for reuse).

## Cloud-neutral representation
```json
{cnr_json}
```

## Mapping reference docs
{docs_section}

{PLAN_JSON_SCHEMA_HINT}
"""


HUB_PROMPT_IDENTIFIER = "bicep-to-cfn-migration-agent3"


def get_system_prompt(prompt_source: str = "local") -> str:
    """Local text unless PROMPT_SOURCE=hub -- hub pull is best-effort and always
    falls back to the local (byte-identical) text on any failure, so an eval
    run never needs LangSmith Hub access to behave like a normal run."""
    if prompt_source != "hub":
        return SYSTEM_PROMPT
    try:
        from langsmith import Client

        prompt = Client().pull_prompt(HUB_PROMPT_IDENTIFIER)
        text = getattr(prompt, "template", None)
        return text if isinstance(text, str) and text.strip() else SYSTEM_PROMPT
    except Exception:
        return SYSTEM_PROMPT
