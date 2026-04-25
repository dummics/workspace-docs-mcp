from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "workspace": {
        "id": "workspace",
        "owner": "workspace",
    },
    "paths": {
        "docs_roots": ["docs", "documentation", "runbooks", "adr", "adrs", "catalog", ".workspace-docs"],
        "code_roots": ["src", "app", "server", "client", "tests"],
        "manifest_files": ["project.json", "package.json", "pyproject.toml", "README.md"],
        "navigation_files": ["docs/navigation.json"],
        "generated_index_files": ["catalog/generated/docs-index.jsonl"],
        "route_files": ["catalog/generated/agent-routes.json"],
        "alias_files": [".workspace-docs/topic-aliases.json"],
        "entity_sources": ["domain-definitions.json", "glossary.yml", "glossary.yaml", "docs/**/terms.md", "docs/**/standard-definitions.md"],
        "exclude": [".git", "node_modules", "bin", "obj", ".rag", "Library", "Temp", "dist", "build", "docs_old~", ".work"],
    },
    "index": {
        "sqlite_path": ".rag/catalog.sqlite",
        "qdrant_url": "http://localhost:6333",
        "qdrant_collection_docs": "workspace_document_cards",
        "qdrant_collection_chunks": "workspace_section_chunks",
        "chunker_version": "md-heading-v1",
        "max_chunk_tokens": 900,
        "min_chunk_tokens": 80,
    },
    "models": {
        "embedding_backend": "flagembedding_bgem3",
        "embedding_model": "BAAI/bge-m3",
        "reranker_backend": "flagembedding_reranker",
        "reranker_model": "BAAI/bge-reranker-v2-m3",
        "allow_model_fallback": False,
        "require_exact_model_names": True,
        "require_embedding_dimension": 1024,
        "require_reranker": True,
        "use_fp16": "auto",
        "query_max_length": 512,
        "passage_max_length": 2048,
        "max_model_length": 8192,
        "local_only": True,
        "device": "auto",
    },
    "retrieval": {
        "default_max_results": 8,
        "candidate_limit_dense": 40,
        "candidate_limit_lexical": 40,
        "rerank_candidates": 50,
        "final_top_k": 8,
        "include_historical_default": False,
    },
    "policy": {
        "authority": {
            "canonical": 1.0,
            "runbook": 0.9,
            "active": 0.8,
            "generated": 0.65,
            "support": 0.55,
            "historical": 0.3,
            "deprecated": 0.0,
            "archived": 0.0,
            "inferred": 0.45,
        },
        "exclude_by_default": ["deprecated", "archived"],
        "historical_requires_flag": True,
    },
    "confidence": {
        "high_min_score": 0.80,
        "medium_min_score": 0.55,
        "require_valid_citation_for_high": True,
    },
    "auto_index": {
        "enabled": True,
        "debounce_seconds": 600,
        "retry_after_seconds": 15,
        "max_changed_files": 20,
        "lock_path": ".rag/index.lock",
        "last_start_path": ".rag/index-worker-last-start.json",
        "log_path": ".rag/logs/index-worker.log",
    },
}


@dataclass
class LocatorConfig:
    root: Path
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULT_CONFIG))

    @property
    def sqlite_path(self) -> Path:
        return (self.root / self.data["index"]["sqlite_path"]).resolve()

    @property
    def chunker_version(self) -> str:
        return str(self.data["index"]["chunker_version"])

    @property
    def embedding_model(self) -> str:
        return str(self.data["models"]["embedding_model"])

    @property
    def reranker_model(self) -> str:
        return str(self.data["models"]["reranker_model"])

    @property
    def embedding_backend(self) -> str:
        return str(self.data["models"]["embedding_backend"])

    @property
    def reranker_backend(self) -> str:
        return str(self.data["models"]["reranker_backend"])

    @property
    def workspace_id(self) -> str:
        return str(self.data.get("workspace", {}).get("id", "workspace"))

    @property
    def owner(self) -> str:
        return str(self.data.get("workspace", {}).get("owner", "workspace"))

    def docs_roots(self) -> list[Path]:
        return [self.root / p for p in self.data["paths"]["docs_roots"]]

    def code_roots(self) -> list[Path]:
        return [self.root / p for p in self.data["paths"].get("code_roots", [])]

    def manifest_files(self) -> list[Path]:
        return [self.root / p for p in self.data["paths"].get("manifest_files", [])]

    def configured_files(self, key: str) -> list[Path]:
        return [self.root / p for p in self.data["paths"].get(key, [])]

    def glob_sources(self, key: str) -> list[Path]:
        out: list[Path] = []
        for pattern in self.data["paths"].get(key, []):
            pattern = str(pattern)
            if any(ch in pattern for ch in "*?["):
                out.extend(self.root.glob(pattern))
            else:
                out.append(self.root / pattern)
        return sorted(set(path.resolve() for path in out if path.exists()))

    def status_authority(self, status: str) -> float:
        return float(self.data["policy"]["authority"].get(status, self.data["policy"]["authority"]["inferred"]))


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_yaml_like(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def load_config(root: Path, config_path: Path | None = None) -> LocatorConfig:
    path = config_path or root / ".workspace-docs" / "locator.config.yml"
    data = deep_merge(DEFAULT_CONFIG, load_yaml_like(path))
    return LocatorConfig(root=root.resolve(), data=data)

