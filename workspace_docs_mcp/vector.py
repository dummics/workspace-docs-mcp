from __future__ import annotations

import json
import uuid
from typing import Any

from .config import LocatorConfig
from .local_bge_backend import BgeM3LocalBackend, lexical_weights_to_qdrant_sparse


class VectorIndex:
    DENSE_VECTOR = "dense"
    SPARSE_VECTOR = "sparse"

    def __init__(self, config: LocatorConfig, backend: BgeM3LocalBackend | None = None):
        self.config = config
        self.backend = backend

    def available(self) -> tuple[bool, str | None]:
        try:
            from qdrant_client import QdrantClient  # type: ignore

            client = QdrantClient(url=self.config.data["index"]["qdrant_url"])
            client.get_collections()
            return True, None
        except Exception as exc:
            return False, f"qdrant_unavailable: {exc}"

    def rebuild_from_sqlite(self, conn) -> dict[str, Any]:
        from qdrant_client import QdrantClient  # type: ignore
        from qdrant_client.http import models  # type: ignore

        backend = self.backend or BgeM3LocalBackend.from_locator_config(self.config)
        backend.load_embedding_model()

        client = QdrantClient(url=self.config.data["index"]["qdrant_url"])
        docs_collection = self.config.data["index"]["qdrant_collection_docs"]
        chunks_collection = self.config.data["index"]["qdrant_collection_chunks"]
        vector_config = {self.DENSE_VECTOR: models.VectorParams(size=1024, distance=models.Distance.COSINE)}
        sparse_config = {self.SPARSE_VECTOR: models.SparseVectorParams()}
        for collection in [docs_collection, chunks_collection]:
            self.ensure_collection(client, models, collection, vector_config, sparse_config)

        docs = conn.execute("SELECT * FROM documents").fetchall()
        doc_texts = [self.document_card_text(row) for row in docs]
        doc_encoded = backend.encode_passages(doc_texts, return_sparse=True) if doc_texts else {"dense": [], "sparse": []}
        expected_doc_ids = {str(uuid.uuid5(uuid.NAMESPACE_URL, row["path"])) for row in docs}
        if docs:
            client.upsert(
                collection_name=docs_collection,
                points=[
                    models.PointStruct(
                        id=str(uuid.uuid5(uuid.NAMESPACE_URL, row["path"])),
                        vector=self.point_vectors(doc_encoded["dense"][index], doc_encoded["sparse"][index]),
                        payload=self.document_payload(row, text),
                    )
                    for index, (row, text) in enumerate(zip(docs, doc_texts))
                ],
            )
        self.delete_stale_points(client, models, docs_collection, expected_doc_ids)

        rows = conn.execute("SELECT * FROM chunks").fetchall()
        expected_chunk_ids = {str(uuid.uuid5(uuid.NAMESPACE_URL, row["chunk_id"])) for row in rows}
        batch_size = 16
        upserted = 0
        for offset in range(0, len(rows), batch_size):
            batch = rows[offset : offset + batch_size]
            texts = [row["text_for_embedding"] for row in batch]
            encoded = backend.encode_passages(texts, return_sparse=True)
            client.upsert(
                collection_name=chunks_collection,
                points=[
                    models.PointStruct(
                        id=str(uuid.uuid5(uuid.NAMESPACE_URL, row["chunk_id"])),
                        vector=self.point_vectors(encoded["dense"][index], encoded["sparse"][index]),
                        payload=self.chunk_payload(row),
                    )
                    for index, row in enumerate(batch)
                ],
            )
            upserted += len(batch)
        self.delete_stale_points(client, models, chunks_collection, expected_chunk_ids)
        return {
            "enabled": True,
            "documents": len(docs),
            "chunks": upserted,
            "collections": [docs_collection, chunks_collection],
            "embedding_model": self.config.embedding_model,
            "embedding_backend": self.config.embedding_backend,
            "embedding_dim": 1024,
            "reranker_model": self.config.reranker_model,
            "reranker_backend": self.config.reranker_backend,
        }

    def ensure_collection(self, client: Any, models: Any, collection: str, vector_config: dict[str, Any], sparse_config: dict[str, Any]) -> None:
        try:
            client.get_collection(collection_name=collection)
            return
        except Exception:
            client.create_collection(
                collection_name=collection,
                vectors_config=vector_config,
                sparse_vectors_config=sparse_config,
            )

    def delete_stale_points(self, client: Any, models: Any, collection: str, expected_ids: set[str]) -> None:
        stale: list[str] = []
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=collection,
                limit=256,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            for point in points:
                point_id = str(point.id)
                if point_id not in expected_ids:
                    stale.append(point_id)
            if offset is None:
                break
        if stale:
            client.delete(
                collection_name=collection,
                points_selector=models.PointIdsList(points=stale),
                wait=True,
            )

    def point_vectors(self, dense: list[float], sparse_weights: dict[int, float] | dict[str, float] | None) -> dict[str, Any]:
        sparse = lexical_weights_to_qdrant_sparse(sparse_weights)
        vectors: dict[str, Any] = {self.DENSE_VECTOR: dense}
        if sparse is not None:
            vectors[self.SPARSE_VECTOR] = sparse
        return vectors

    def document_card_text(self, row: Any) -> str:
        aliases = ", ".join(json.loads(row["aliases_json"] or "[]"))
        canonical = ", ".join(json.loads(row["canonical_for_json"] or "[]"))
        return (
            f"Title: {row['title']}\nPath: {row['path']}\nStatus: {row['status']}\nDoc type: {row['doc_type']}\n"
            f"Repo area: {row['repo_area']}\nAliases: {aliases}\nCanonical for: {canonical}\n"
        )

    def document_payload(self, row: Any, text: str) -> dict[str, Any]:
        return {
            "document_id": row["document_id"],
            "path": row["path"],
            "title": row["title"],
            "status": row["status"],
            "doc_type": row["doc_type"],
            "repo_area": row["repo_area"],
            "authority": row["authority"],
            "heading_path": [],
            "anchor": "",
            "line_start": 1,
            "line_end": 1,
            "text_for_embedding": text,
            "text_for_rerank": text,
            "embedding_model": self.config.embedding_model,
            "embedding_backend": self.config.embedding_backend,
            "embedding_dim": 1024,
            "reranker_model": self.config.reranker_model,
            "reranker_backend": self.config.reranker_backend,
            "content_hash": row["content_hash"],
            "git_commit": row["git_commit"],
            "last_modified": row["last_modified"],
            "text_preview": text[:1000],
        }

    def chunk_payload(self, row: Any) -> dict[str, Any]:
        heading_path = json.loads(row["heading_path_json"] or "[]")
        text_for_rerank = f"{row['title']}\n{' > '.join(heading_path)}\n{row['text'] or ''}"
        return {
            "chunk_id": row["chunk_id"],
            "document_id": row["document_id"],
            "path": row["path"],
            "title": row["title"],
            "status": row["status"],
            "doc_type": row["doc_type"],
            "repo_area": row["repo_area"],
            "authority": row["authority"],
            "heading_path": heading_path,
            "anchor": row["anchor"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "text_for_embedding": row["text_for_embedding"],
            "text_for_rerank": text_for_rerank,
            "embedding_model": self.config.embedding_model,
            "embedding_backend": self.config.embedding_backend,
            "embedding_dim": 1024,
            "reranker_model": self.config.reranker_model,
            "reranker_backend": self.config.reranker_backend,
            "content_hash": row["content_hash"],
            "git_commit": row["git_commit"],
            "last_modified": row["last_modified"],
            "text_preview": (row["text"] or "")[:1000],
        }

    def search_documents(self, query: str, limit: int = 50, include_sparse: bool = True) -> list[dict[str, Any]]:
        return self.search_collection(self.config.data["index"]["qdrant_collection_docs"], query, limit, include_sparse, id_key="document_id")

    def search_chunks(self, query: str, limit: int = 50, include_sparse: bool = True) -> list[dict[str, Any]]:
        return self.search_collection(self.config.data["index"]["qdrant_collection_chunks"], query, limit, include_sparse, id_key="chunk_id")

    def search_collection(self, collection: str, query: str, limit: int = 50, include_sparse: bool = True, id_key: str = "chunk_id") -> list[dict[str, Any]]:
        from qdrant_client import QdrantClient  # type: ignore

        backend = self.backend or BgeM3LocalBackend.from_locator_config(self.config)
        encoded = backend.encode_queries([query], return_sparse=True)
        dense = encoded["dense"][0]
        sparse = lexical_weights_to_qdrant_sparse(encoded["sparse"][0] if encoded.get("sparse") else None)
        client = QdrantClient(url=self.config.data["index"]["qdrant_url"])

        hits: dict[str, dict[str, Any]] = {}
        ranks: dict[str, dict[str, int]] = {}
        dense_response = client.query_points(
            collection_name=collection,
            query=dense,
            using=self.DENSE_VECTOR,
            limit=limit,
            with_payload=True,
        )
        for rank, hit in enumerate(dense_response.points, start=1):
            payload = hit.payload or {}
            chunk_id = str(payload.get(id_key) or payload.get("chunk_id") or payload.get("document_id") or hit.id)
            hits.setdefault(chunk_id, {"payload": payload, "dense_score": 0.0, "sparse_score": 0.0})
            hits[chunk_id]["dense_score"] = max(float(hit.score), hits[chunk_id]["dense_score"])
            ranks.setdefault(chunk_id, {})["dense"] = rank

        if include_sparse and sparse is not None:
            sparse_response = client.query_points(
                collection_name=collection,
                query=sparse,
                using=self.SPARSE_VECTOR,
                limit=limit,
                with_payload=True,
            )
            for rank, hit in enumerate(sparse_response.points, start=1):
                payload = hit.payload or {}
                chunk_id = str(payload.get(id_key) or payload.get("chunk_id") or payload.get("document_id") or hit.id)
                hits.setdefault(chunk_id, {"payload": payload, "dense_score": 0.0, "sparse_score": 0.0})
                hits[chunk_id]["sparse_score"] = max(float(hit.score), hits[chunk_id]["sparse_score"])
                ranks.setdefault(chunk_id, {})["sparse"] = rank

        fused: list[dict[str, Any]] = []
        for key, item in hits.items():
            item["generator_ranks"] = ranks.get(key, {})
            item["score"] = rrf_score(ranks.get(key, {}).values())
            fused.append(item)
        return sorted(fused, key=lambda item: item["score"], reverse=True)[:limit]


def rrf_score(ranks, k: int = 60) -> float:
    values = list(ranks)
    if not values:
        return 0.0
    raw = sum(1.0 / (k + int(rank)) for rank in values)
    return min(1.0, raw / (len(values) / (k + 1)))

