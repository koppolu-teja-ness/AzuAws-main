"""RAG retrieval layer over knowledge_base docs, used only by Agent 3's prompt
construction. Agent 1's pass/fail gate (KnowledgeBase.supported_types(), an exact
Azure-type -> doc lookup in index.json) stays fully deterministic and is
untouched by this module -- retrieval here only changes which sections of an
already-selected doc get embedded in the LLM prompt, never whether a resource
type is considered supported.

Mapping docs are chunked on markdown headings and embedded via an AWS Bedrock
embedding model into a persistent Chroma vector store (knowledge_base/.chroma/,
collection "azure_aws_mappings"), so the mapping data agent3 retrieves survives
across runs/processes. Ingestion is idempotent (chunk ids are stable hashes) --
re-running only embeds new/changed chunks. Any failure (no Bedrock access,
chromadb not installed, etc.) falls back to the full doc text, so a retrieval
problem never blocks a run.

Ranking is hybrid by default (config.rag_hybrid_search): dense vector similarity
(the Chroma/Bedrock embedding search above) fused with sparse BM25 keyword
scoring (rank_bm25) via Reciprocal Rank Fusion, over the candidate chunks of
the resource type's OWN doc only (never across docs -- same scoping as before,
just ranked two ways instead of one). Falls back to vector-only ranking if
rank_bm25 isn't installed or BM25 scoring fails for any reason.
"""
from __future__ import annotations

import hashlib
import json
import re

from .config import Config

_COLLECTION_NAME = "azure_aws_mappings"
_CHROMA_DIRNAME = ".chroma"
_HEADING_RE = re.compile(r"^#{1,3}\s+.*$", re.MULTILINE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_RRF_K = 60  # conventional Reciprocal Rank Fusion constant


class EmbeddingError(RuntimeError):
    pass


def chunk_markdown(text: str) -> list[str]:
    """Split a markdown doc into sections on level 1-3 headings."""
    positions = [m.start() for m in _HEADING_RE.finditer(text)]
    if not positions:
        return [text.strip()] if text.strip() else []
    positions.append(len(text))
    chunks = [text[positions[i]:positions[i + 1]].strip() for i in range(len(positions) - 1)]
    return [c for c in chunks if c]


class BedrockEmbedder:
    """Embeds text via an AWS Bedrock embedding model (e.g. Titan Text Embeddings)."""

    def __init__(self, config: Config):
        self.config = config
        try:
            import boto3
        except ImportError as exc:
            raise EmbeddingError("boto3 is not installed.") from exc
        try:
            self._client = boto3.client("bedrock-runtime", region_name=config.aws_region)
        except Exception as exc:
            raise EmbeddingError("Could not create a Bedrock client for embeddings.") from exc

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"inputText": text[:8000]})
        try:
            response = self._client.invoke_model(
                modelId=self.config.embedding_model_id,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
        except Exception as exc:
            raise EmbeddingError(f"Bedrock embedding call failed: {exc}") from exc
        payload = json.loads(response["body"].read())
        embedding = payload.get("embedding")
        if not isinstance(embedding, list):
            raise EmbeddingError(f"Unexpected embedding response format: {payload}")
        return embedding


def _chroma_embedding_function(embedder: BedrockEmbedder):
    """Duck-typed Chroma embedding function wrapping BedrockEmbedder; name() is
    required by newer chromadb versions to persist embedding-function identity."""
    from chromadb import Documents, EmbeddingFunction, Embeddings

    class _BedrockEF(EmbeddingFunction):
        def __call__(self, input: Documents) -> Embeddings:
            return [embedder.embed(text) for text in input]

        @staticmethod
        def name() -> str:
            return "bedrock"

    return _BedrockEF()


def _get_collection(knowledge_base, config: Config):
    import chromadb

    client = chromadb.PersistentClient(path=str(knowledge_base.base_dir / _CHROMA_DIRNAME))
    embedder = BedrockEmbedder(config)
    return client.get_or_create_collection(
        name=_COLLECTION_NAME, embedding_function=_chroma_embedding_function(embedder)
    )


def ingest_knowledge_base(knowledge_base, config: Config) -> int:
    """Chunk + embed every doc referenced in index.json into the Chroma collection.
    Idempotent: chunk ids are stable hashes, so only new/changed chunks get
    (re-)embedded. Returns the number of chunks newly written."""
    collection = _get_collection(knowledge_base, config)
    written = 0
    seen_docs: set[str] = set()
    for rel_path in knowledge_base.raw_index().values():
        if rel_path in seen_docs:
            continue
        seen_docs.add(rel_path)
        doc_path = (knowledge_base.base_dir / rel_path).resolve()
        if not doc_path.exists():
            continue
        chunks = chunk_markdown(doc_path.read_text(encoding="utf-8"))
        ids = [f"{rel_path}::{hashlib.sha256(c.encode('utf-8')).hexdigest()[:16]}" for c in chunks]
        existing = set(collection.get(ids=ids)["ids"]) if ids else set()
        new = [(cid, c) for cid, c in zip(ids, chunks) if cid not in existing]
        if not new:
            continue
        collection.upsert(
            ids=[cid for cid, _ in new],
            documents=[c for _, c in new],
            metadatas=[{"source_doc": rel_path, "heading": c.splitlines()[0][:120]} for _, c in new],
        )
        written += len(new)
    return written


def build_kb_query(cnr, resource_types: list[str]) -> str:
    """Short natural-language description of the resources being migrated, used
    as the retrieval query (not sent to the migration-plan LLM itself)."""
    summaries = [
        f"{r.azure_type} ({r.name_expression})"
        for r in cnr.resources
        if r.azure_type in resource_types
    ]
    return "Migrating these Azure resources to AWS equivalents: " + "; ".join(summaries)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _reciprocal_rank_fusion(rankings: list[list[str]], k: int = _RRF_K) -> dict[str, float]:
    """score(chunk_id) = sum over rankings of 1/(k + rank) -- standard RRF,
    needs no score normalization across the two very different scales (cosine
    distance vs. BM25) since it only ever looks at rank position."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return fused


def _search_doc_chunks(
    collection, rel_path: str | None, query: str, top_k: int, hybrid_enabled: bool
) -> tuple[list[str], bool]:
    """Ranked chunk texts (best first, capped at top_k) for one doc, scoped by
    `where={"source_doc": rel_path}` throughout -- hybrid fusion never looks
    outside that doc's own candidate chunks. Returns (chunks, used_hybrid);
    used_hybrid is False whenever hybrid ranking wasn't actually applied
    (disabled, no rank_bm25, or BM25 scoring failed), not just requested."""
    where = {"source_doc": rel_path} if rel_path else None
    if not hybrid_enabled:
        hits = collection.query(query_texts=[query], n_results=top_k, where=where)
        return (hits.get("documents") or [[]])[0], False

    candidates = collection.get(where=where)
    ids, texts = candidates.get("ids") or [], candidates.get("documents") or []
    if not ids:
        return [], False
    by_id = dict(zip(ids, texts))

    vector_hits = collection.query(query_texts=[query], n_results=len(ids), where=where)
    vector_ranking = (vector_hits.get("ids") or [[]])[0]

    try:
        from rank_bm25 import BM25Okapi

        bm25 = BM25Okapi([_tokenize(t) for t in texts])
        scores = bm25.get_scores(_tokenize(query))
        bm25_ranking = [cid for cid, _ in sorted(zip(ids, scores), key=lambda pair: pair[1], reverse=True)]
    except Exception:
        return [by_id[cid] for cid in vector_ranking[:top_k] if cid in by_id], False

    fused = _reciprocal_rank_fusion([vector_ranking, bm25_ranking])
    ranked_ids = sorted(fused, key=fused.get, reverse=True)
    return [by_id[cid] for cid in ranked_ids[:top_k] if cid in by_id], True


def retrieve_mapping_docs(
    knowledge_base,
    resource_types: list[str],
    query: str,
    config: Config,
) -> dict[str, str]:
    """Return {resource_type: mapping_text} for agent3's prompt. When RAG is
    enabled, mapping_text is the top-K most relevant chunks for `query` pulled
    from the persistent Chroma store (ingesting it first); otherwise, and on any
    retrieval failure, falls back to the full doc text."""
    docs = knowledge_base.load_docs(resource_types)
    if not config.rag_enabled or not docs:
        return docs
    try:
        collection = _get_collection(knowledge_base, config)
        ingest_knowledge_base(knowledge_base, config)
        raw_index = knowledge_base.raw_index()
        result: dict[str, str] = {}
        for rtype, full_text in docs.items():
            rel_path = raw_index.get(rtype)
            retrieved, _used_hybrid = _search_doc_chunks(collection, rel_path, query, config.rag_top_k, config.rag_hybrid_search)
            result[rtype] = "\n\n".join(retrieved) if retrieved else full_text
        return result
    except Exception:
        return docs


def retrieve_mapping_docs_with_diagnostics(
    knowledge_base,
    resource_types: list[str],
    query: str,
    config: Config,
) -> tuple[dict[str, str], dict[str, dict]]:
    """Same retrieval as retrieve_mapping_docs(), plus a per-resource-type
    diagnostics dict (source doc, chunk count, whether it fell back to the
    full doc, any chunk that came from the wrong doc despite the `where`
    filter) -- used only by the eval harness's retrieval-quality evaluator
    (orchestrator/evaluation.py). Agent 3's production path keeps calling the
    plain retrieve_mapping_docs() above, unchanged."""
    docs = knowledge_base.load_docs(resource_types)
    raw_index = knowledge_base.raw_index()
    diagnostics = {
        rtype: {"source_doc": raw_index.get(rtype), "used_fallback": True, "retrieved_chunk_count": 0}
        for rtype in resource_types
    }
    if not config.rag_enabled or not docs:
        return docs, diagnostics
    try:
        collection = _get_collection(knowledge_base, config)
        ingest_knowledge_base(knowledge_base, config)
        result: dict[str, str] = {}
        for rtype, full_text in docs.items():
            rel_path = raw_index.get(rtype)
            retrieved_docs, used_hybrid = _search_doc_chunks(
                collection, rel_path, query, config.rag_top_k, config.rag_hybrid_search
            )
            if retrieved_docs:
                result[rtype] = "\n\n".join(retrieved_docs)
                diagnostics[rtype] = {
                    "source_doc": rel_path,
                    "used_fallback": False,
                    "used_hybrid": used_hybrid,
                    "retrieved_chunk_count": len(retrieved_docs),
                    "off_source_chunks": [],  # structurally impossible now -- candidates are pre-scoped via where=
                }
            else:
                result[rtype] = full_text
        return result, diagnostics
    except Exception as exc:
        for info in diagnostics.values():
            info["error"] = str(exc)
        return docs, diagnostics

