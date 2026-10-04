"""LLM reasoning step: turns a cloud-neutral representation + mapping docs into a
MigrationPlan (structured JSON), NOT a final template. Template rendering is a
separate deterministic step (see cfn_generator.py) -- the LLM never emits
CloudFormation syntax directly, which keeps the reasoning reusable across target
generators.

The LLM call is isolated behind the Generator interface so the rest of the
pipeline (parsing, knowledge lookup, validation) works and is testable today,
before AWS Bedrock credentials are configured. Swap in a different backend by
implementing Generator.generate() (e.g. Azure OpenAI, OpenAI) without touching
pipeline.py.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import re

from .config import Config
from .observability import annotate_current_run, traceable
from .prompts.agent3_v1 import get_system_prompt


class GeneratorNotConfiguredError(RuntimeError):
    pass


@traceable(
    run_type="llm",
    name="bedrock_invoke_model",
    # `client` is a non-serializable boto3 object -- keep it out of traced inputs.
    process_inputs=lambda inputs: {k: v for k, v in inputs.items() if k != "client"},
)
def _invoke_bedrock(client, model_id: str, body: str) -> dict:
    """Isolated so @traceable sees a clean (model, request body) -> payload
    boundary; does not change the request/response shape `generate()` parses."""
    import json

    response = client.invoke_model(
        modelId=model_id, body=body, contentType="application/json", accept="application/json"
    )
    payload = json.loads(response["body"].read())
    usage = payload.get("usage") or {}
    # Bedrock/Nova usage keys are camelCase; fall back to snake_case defensively
    # for other model families.
    usage_metadata = {
        "input_tokens": usage.get("inputTokens", usage.get("input_tokens", 0)),
        "output_tokens": usage.get("outputTokens", usage.get("output_tokens", 0)),
        "total_tokens": usage.get("totalTokens", usage.get("total_tokens", 0)),
    }
    annotate_current_run(metadata={"ls_provider": "amazon_bedrock", "ls_model_name": model_id, "usage_metadata": usage_metadata})
    return payload


class Generator(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Return raw CloudFormation YAML text for the given user prompt."""


class BedrockGenerator(Generator):
    """Calls an AWS Bedrock model (e.g. an Anthropic Claude model) to generate YAML.

    Requires: `pip install boto3` and AWS credentials with `bedrock:InvokeModel`
    permission for `config.bedrock_model_id` in `config.aws_region`.
    """

    def __init__(self, config: Config, system_prompt: str | None = None):
        self.config = config
        # Override hook for the eval harness to compare prompt versions
        # (orchestrator/prompts/agent3_v2/...); the main pipeline never sets this.
        self._system_prompt_override = system_prompt
        try:
            import boto3  # noqa: F401
        except ImportError as exc:
            raise GeneratorNotConfiguredError(
                "boto3 is not installed. Run 'pip install boto3' to enable "
                "the Bedrock generator."
            ) from exc
        self._boto3 = boto3

    def _client(self):
        try:
            return self._boto3.client(
                "bedrock-runtime", region_name=self.config.aws_region
            )
        except Exception as exc:  # e.g. NoCredentialsError
            raise GeneratorNotConfiguredError(
                "Could not create a Bedrock client. Configure AWS credentials "
                "(aws configure / env vars) with bedrock:InvokeModel access."
            ) from exc

    @staticmethod
    def _normalize_text_output(text: str) -> str:
        """Strip markdown fences/BOM the model may wrap the JSON plan in."""
        cleaned = text.strip().replace("\ufeff", "")
        fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.IGNORECASE | re.DOTALL)
        if fenced:
            cleaned = fenced.group(1).strip()
        return cleaned

    def generate(self, prompt: str) -> str:
        import json

        client = self._client()
        # Amazon Nova request format:
        # - messages[*].content must be an ARRAY of content blocks.
        # - avoid provider-specific params that this model rejects.
        body = json.dumps(
            {
                "schemaVersion": "messages-v1",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": f"{self._system_prompt_override or get_system_prompt(self.config.prompt_source)}\n\n{prompt}",
                            }
                        ],
                    }
                ],
            }
        )
        payload = _invoke_bedrock(client, self.config.bedrock_model_id, body)

        # Common Bedrock/Nova response shape:
        # {"output": {"message": {"content": [{"text": "..."}]}}}
        blocks = payload.get("output", {}).get("message", {}).get("content", [])
        if isinstance(blocks, list):
            text_parts = [
                b.get("text", "")
                for b in blocks
                if isinstance(b, dict) and b.get("text")
            ]
            if text_parts:
                return self._normalize_text_output("\n".join(text_parts))

        # Fallback shape used by some providers/tooling:
        # {"content": [{"text": "..."}]}
        blocks = payload.get("content", [])
        if isinstance(blocks, list):
            text_parts = [
                b.get("text", "")
                for b in blocks
                if isinstance(b, dict) and b.get("text")
            ]
            if text_parts:
                return self._normalize_text_output("\n".join(text_parts))

        # Final text fallbacks
        for key in ("outputText", "completion", "generated_text", "text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return self._normalize_text_output(value)

        raise GeneratorNotConfiguredError(
            f"Unexpected Bedrock response format: {payload}"
        )
