"""Runtime configuration, sourced from environment variables / a local .env file.

No secrets are hard-coded here. AZURE_RESOURCE_GROUP / AZURE_SUBSCRIPTION_ID are
read from .env (never from CLI flags) so they never end up in shell history or
process argv listings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    aws_region: str
    bedrock_model_id: str
    max_fix_attempts: int
    azure_resource_group: str | None
    azure_subscription_id: str | None
    rag_enabled: bool
    embedding_model_id: str
    rag_top_k: int
    rag_hybrid_search: bool
    langsmith_tracing: bool
    langsmith_api_key: str | None
    langsmith_project: str
    langsmith_endpoint: str | None
    prompt_source: str  # "local" | "hub" -- see orchestrator/prompts/agent3_v1
    guardrail_scan_enabled: bool

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            bedrock_model_id=os.environ.get(
                "BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0"
            ),
            max_fix_attempts=int(os.environ.get("MAX_FIX_ATTEMPTS", "2")),
            azure_resource_group=os.environ.get("AZURE_RESOURCE_GROUP") or None,
            azure_subscription_id=os.environ.get("AZURE_SUBSCRIPTION_ID") or None,
            # RAG retrieval over knowledge_base docs for Agent 3's prompt only --
            # Agent 1's supported-type gate is always the plain index.json lookup.
            rag_enabled=os.environ.get("RAG_ENABLED", "true").strip().lower() not in ("false", "0", "no"),
            embedding_model_id=os.environ.get(
                "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
            ),
            rag_top_k=int(os.environ.get("RAG_TOP_K", "4")),
            # Dense (Bedrock embedding) + sparse (BM25) search fused via Reciprocal Rank
            # Fusion, scoped to the resource type's own doc; falls back to vector-only
            # if rank_bm25 isn't installed or scoring fails -- see orchestrator/rag.py.
            rag_hybrid_search=os.environ.get("RAG_HYBRID_SEARCH", "true").strip().lower() not in ("false", "0", "no"),
            # LangSmith tracing/evaluation -- strictly opt-in; see orchestrator/observability.py.
            langsmith_tracing=os.environ.get("LANGSMITH_TRACING", "false").strip().lower() in ("true", "1", "yes"),
            langsmith_api_key=os.environ.get("LANGSMITH_API_KEY") or None,
            langsmith_project=os.environ.get("LANGSMITH_PROJECT", "bicep-to-cfn-migration"),
            langsmith_endpoint=os.environ.get("LANGSMITH_ENDPOINT") or None,
            prompt_source=os.environ.get("PROMPT_SOURCE", "local").strip().lower(),
            # Guardrail security scan (checkov + custom secret/IAM/network checks) between
            # cfn-lint passing and deployment -- see orchestrator/guardrails.py. Disabling
            # this only skips the scan itself; the human gate it feeds is also then skipped.
            guardrail_scan_enabled=os.environ.get("GUARDRAIL_SCAN_ENABLED", "true").strip().lower()
            not in ("false", "0", "no"),
        )
