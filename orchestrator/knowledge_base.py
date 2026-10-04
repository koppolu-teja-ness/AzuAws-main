"""Looks up existing Azure->AWS mapping docs by ARM resource type.

The knowledge base is deliberately simple: a flat folder of markdown docs
(one per resource-type family, e.g. knowledge_base/bicep-to-cloudformation.md
for Key Vault) plus an index.json that maps each Azure resource `type` string
to the doc that documents its AWS equivalent. When a resource type has no
entry, the pipeline stops and flags it for a human to research and add a new
doc + index entry (see knowledge_base/bicep-to-cloudformation.md for the
expected doc shape).
"""
from __future__ import annotations

import json
from pathlib import Path


class KnowledgeBase:
    def __init__(self, index_path: Path):
        self.index_path = index_path
        self.base_dir = index_path.parent
        with open(index_path, "r", encoding="utf-8") as f:
            self._index: dict[str, str] = json.load(f)

    def doc_path_for(self, resource_type: str) -> Path | None:
        rel_path = self._index.get(resource_type)
        if rel_path is None:
            return None
        return (self.base_dir / rel_path).resolve()

    def missing_types(self, resource_types: list[str]) -> list[str]:
        return [t for t in resource_types if t not in self._index]

    def supported_types(self) -> list[str]:
        """All Azure resource types with a knowledge-base mapping doc."""
        return list(self._index.keys())

    def raw_index(self) -> dict[str, str]:
        """{resource_type: doc_relative_path} as written in index.json -- the stable
        identifier used to tag/filter vector-store entries per source doc (see rag.py)."""
        return dict(self._index)

    def load_docs(self, resource_types: list[str]) -> dict[str, str]:
        """Return {resource_type: doc_markdown} for every type that has a doc."""
        docs: dict[str, str] = {}
        for resource_type in resource_types:
            path = self.doc_path_for(resource_type)
            if path and path.exists():
                docs[resource_type] = path.read_text(encoding="utf-8")
        return docs
