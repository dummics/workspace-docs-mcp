from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .config import LocatorConfig
from .model import SearchResult
from .source_index import split_camel
from .vector import VectorIndex
from .local_bge_backend import BgeM3LocalBackend


TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text)]


def score(value: float | None) -> float | None:
    if value is None:
        return None
    return round(max(0.0, min(1.0, float(value))), 3)


def snippet(text: str, query: str, max_len: int = 180) -> str:
    lower = text.lower()
    for term in tokenize(query):
        pos = lower.find(term)
        if pos >= 0:
            start = max(0, pos - 120)
            end = min(len(text), pos + max_len)
            return text[start:end].replace("\n", " ").strip()
    return text[:max_len].replace("\n", " ").strip()


class Retriever:
    def __init__(self, config_or_context: LocatorConfig | Any):
        if hasattr(config_or_context, "config") and hasattr(config_or_context, "catalog"):
            self.context = config_or_context
            self.config = config_or_context.config
            self.catalog = config_or_context.catalog
            self.vector = config_or_context.vector
            self.backend = config_or_context.backend
        else:
            self.context = None
            self.config = config_or_context
            self.catalog = Catalog(config_or_context)
            self.vector = VectorIndex(config_or_context)
            self.backend = None

    def allowed_status_clause(self, include_historical: bool) -> tuple[str, list[str]]:
        excluded = set(self.config.data["policy"]["exclude_by_default"])
        if not include_historical and self.config.data["policy"].get("historical_requires_flag", True):
            excluded.add("historical")
        placeholders = ",".join("?" for _ in excluded)
        return (f"status NOT IN ({placeholders})", list(excluded)) if excluded else ("1=1", [])

    def search(self, query: str, repo_area: str | None = None, doc_type: str | None = None, include_historical: bool = False, max_results: int = 8, rerank: bool = True, dedupe_documents: bool = True, verbosity: str = "compact", mode: str = "sections", confidence_cap: str | None = None) -> dict[str, Any]:
        self.catalog.init()
        candidates: dict[str, SearchResult] = {}
        warnings: list[str] = []
        debug: dict[str, Any] = {"candidate_counts": {}, "excluded_counts": self.excluded_counts(include_historical), "active_filters": self.active_filters(repo_area, doc_type, include_historical)}
        try:
            dense_results = self.dense_candidates(query, repo_area, doc_type, include_historical, mode=mode)
        except Exception as exc:
            message = str(exc)
            if "BAAI/bge" in message or "Required embedding model" in message or "No fallback model is allowed" in message:
                raise
            warnings.append(f"vector_index_unavailable: {message}")
            dense_results = []
        debug["candidate_counts"]["vector"] = len(dense_results)
        self.merge_candidates(candidates, dense_results, "vector")
        generators = [
            ("entity", self.entity_candidates(query, repo_area, include_historical)),
            ("code_bridge", self.code_bridge_candidates(query, repo_area, doc_type, include_historical, mode=mode)),
            ("fts", self.lexical_search(query, repo_area, doc_type, include_historical, int(self.config.data["retrieval"]["candidate_limit_lexical"]), mode=mode)),
            ("alias", self.alias_and_exact_candidates(query, repo_area, include_historical, mode=mode)),
        ]
        for generator, generated in generators:
            debug["candidate_counts"][generator] = len(generated)
            self.merge_candidates(candidates, generated, generator)
        results = list(candidates.values())
        self.apply_scores(results, query)
        results.sort(key=lambda r: r.score, reverse=True)
        if rerank:
            rerank_warning = self.try_rerank(query, results)
            if rerank_warning:
                warnings.append(rerank_warning)
        results.sort(key=lambda r: r.score, reverse=True)
        if dedupe_documents:
            best_by_path: dict[str, SearchResult] = {}
            for result in results:
                if result.path not in best_by_path:
                    best_by_path[result.path] = result
            results = list(best_by_path.values())
        results = results[:max_results]
        confidence, reasons, suggested = self.confidence(results, query)
        if confidence_cap == "medium" and confidence == "high":
            confidence = "medium"
            reasons.append("confidence capped because index is usable_stale")
        if mode == "documents":
            self.attach_best_sections(results, query, include_historical)
        return {
            "query": query,
            "intent": "locate_doc",
            "search_mode": mode,
            "confidence": confidence,
            "confidence_reasons": reasons,
            "warnings": warnings,
            "results": [self.result_json(r, verbosity=verbosity) for r in results],
            "suggested_next_queries": suggested,
            **({"debug": {**debug, "index_state": self.index_state(), "recommended_fix": self.recommended_fix(results, debug)}} if verbosity == "full" else {}),
        }

    def merge_candidates(self, candidates: dict[str, SearchResult], generated: list[SearchResult], generator: str) -> None:
        for rank, result in enumerate(generated, start=1):
            key = result.path + str(result.line_start) + result.source_type
            result.generator_ranks[generator] = rank
            if key in candidates:
                current = candidates[key]
                current.dense_score = max(current.dense_score, result.dense_score)
                current.sparse_score = max(current.sparse_score, result.sparse_score)
                current.lexical_score = max(current.lexical_score, result.lexical_score)
                current.exact_score = max(current.exact_score, result.exact_score)
                current.why.extend(result.why)
                current.generator_ranks.update(result.generator_ranks)
            else:
                candidates[key] = result

    def active_filters(self, repo_area: str | None, doc_type: str | None, include_historical: bool) -> dict[str, Any]:
        excluded = list(self.config.data["policy"]["exclude_by_default"])
        if not include_historical:
            excluded.append("historical")
        return {"repo_area": repo_area or "any", "doc_type": doc_type or "any", "include_historical": include_historical, "excluded_statuses": sorted(set(excluded))}

    def excluded_counts(self, include_historical: bool) -> dict[str, int]:
        excluded = set(self.config.data["policy"]["exclude_by_default"])
        if not include_historical:
            excluded.add("historical")
        if not excluded:
            return {}
        placeholders = ",".join("?" for _ in excluded)
        with self.catalog.connect() as conn:
            rows = conn.execute(f"SELECT status, COUNT(*) count FROM documents WHERE status IN ({placeholders}) GROUP BY status", list(excluded)).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def lexical_search(self, query: str, repo_area: str | None, doc_type: str | None, include_historical: bool, limit: int, mode: str = "sections") -> list[SearchResult]:
        status_clause, params = self.allowed_status_clause(include_historical)
        filters = [status_clause]
        if repo_area and repo_area != "any":
            filters.append("repo_area=?")
            params.append(repo_area)
        if doc_type and doc_type != "any":
            filters.append("doc_type=?")
            params.append(doc_type)
        where = " AND ".join(filters)
        fts_query = " OR ".join(tokenize(query)) or query
        if mode == "documents":
            sql = f"""
            SELECT c.*, bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ? AND {where}
            GROUP BY c.path
            ORDER BY rank LIMIT ?
        """
        else:
            sql = f"""
            SELECT c.*, bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ? AND {where}
            ORDER BY rank LIMIT ?
        """
        out: list[SearchResult] = []
        with self.catalog.connect() as conn:
            try:
                rows = conn.execute(sql, [fts_query, *params, limit]).fetchall()
            except Exception:
                rows = conn.execute(f"SELECT * FROM chunks WHERE {where} LIMIT ?", [*params, limit]).fetchall()
            for row in rows:
                lexical = 1.0 / (1.0 + abs(float(row["rank"]))) if "rank" in row.keys() else 0.35
                out.append(self.row_to_result(row, query, lexical_score=lexical, why=["lexical match"]))
        return out

    def dense_candidates(self, query: str, repo_area: str | None, doc_type: str | None, include_historical: bool, mode: str = "sections") -> list[SearchResult]:
        limit = int(self.config.data["retrieval"].get("rerank_candidates", 50))
        hits = self.vector.search_documents(query, limit) if mode == "documents" else self.vector.search_chunks(query, limit)
        if not hits:
            return []
        status_excluded = set(self.config.data["policy"]["exclude_by_default"])
        if not include_historical:
            status_excluded.add("historical")
        out: list[SearchResult] = []
        with self.catalog.connect() as conn:
            for hit in hits:
                payload = hit["payload"]
                if payload.get("status") in status_excluded:
                    continue
                if repo_area and repo_area != "any" and payload.get("repo_area") != repo_area:
                    continue
                if doc_type and doc_type != "any" and payload.get("doc_type") != doc_type:
                    continue
                if mode == "documents":
                    row = conn.execute("SELECT * FROM chunks WHERE document_id=? ORDER BY line_start LIMIT 1", (payload.get("document_id"),)).fetchone()
                else:
                    row = conn.execute("SELECT * FROM chunks WHERE chunk_id=?", (payload.get("chunk_id"),)).fetchone()
                if not row:
                    continue
                out.append(self.row_to_result(row, query, why=["semantic match"], lexical_score=0.0, exact_score=0.0))
                out[-1].dense_score = float(hit.get("dense_score", hit.get("score", 0.0)))
                out[-1].sparse_score = float(hit.get("sparse_score", 0.0))
                out[-1].generator_ranks.update(hit.get("generator_ranks", {}))
        return out

    def alias_and_exact_candidates(self, query: str, repo_area: str | None, include_historical: bool, mode: str = "sections") -> list[SearchResult]:
        status_clause, params = self.allowed_status_clause(include_historical)
        out: list[SearchResult] = []
        q = query.strip().lower()
        query_terms = set(tokenize(query))
        with self.catalog.connect() as conn:
            alias_rows = conn.execute(
                f"""
                SELECT c.*, a.weight
                FROM aliases a JOIN chunks c ON c.document_id=a.document_id
                WHERE lower(a.alias)=? AND {status_clause}
                ORDER BY a.weight DESC, c.authority DESC LIMIT 20
                """,
                [q, *params],
            ).fetchall()
            for row in alias_rows:
                if repo_area and repo_area != "any" and row["repo_area"] != repo_area:
                    continue
                out.append(self.row_to_result(row, query, exact_score=0.95, why=["alias match"]))
            route_rows = conn.execute(
                "SELECT target_path, repo_area, topic FROM routes WHERE lower(topic)=? AND (? IS NULL OR ?='any' OR repo_area=? OR repo_area='any') ORDER BY priority LIMIT 20",
                [q, repo_area, repo_area, repo_area],
            ).fetchall()
            for route in route_rows:
                chunk_rows = conn.execute(
                    f"SELECT * FROM chunks WHERE path=? AND {status_clause} ORDER BY authority DESC, line_start ASC LIMIT 10",
                    [route["target_path"], *params],
                ).fetchall()
                for row in chunk_rows:
                    out.append(self.row_to_result(row, query, exact_score=0.95, why=["route alias match"]))
            title_rows = conn.execute(
                f"""
                SELECT *
                FROM chunks
                WHERE lower(title)=? AND {status_clause}
                ORDER BY authority DESC, line_start ASC LIMIT 30
                """,
                [q, *params],
            ).fetchall()
            for row in title_rows:
                if repo_area and repo_area != "any" and row["repo_area"] != repo_area:
                    continue
                out.append(self.row_to_result(row, query, exact_score=0.95, why=["title match"]))
            title_like_filters = " OR ".join("lower(title) LIKE ?" for _ in query_terms)
            title_like_rows = conn.execute(
                f"""
                SELECT *
                FROM chunks
                WHERE ({title_like_filters}) AND {status_clause}
                ORDER BY authority DESC, line_start ASC LIMIT 50
                """,
                [*[f"%{term}%" for term in query_terms], *params],
            ).fetchall()
            for row in title_like_rows:
                if repo_area and repo_area != "any" and row["repo_area"] != repo_area:
                    continue
                title_terms = set(tokenize(str(row["title"]) + " " + str(row["path"])))
                overlap = len(query_terms.intersection(title_terms)) / max(1, len(query_terms))
                if overlap >= 0.35:
                    out.append(self.row_to_result(row, query, exact_score=min(0.85, overlap), why=["partial title/path match"]))
            term_filters = " OR ".join("lower(a.alias) LIKE ?" for _ in query_terms)
            alias_like_rows = conn.execute(
                f"""
                SELECT c.*, a.alias, a.weight
                FROM aliases a JOIN chunks c ON c.document_id=a.document_id
                WHERE ({term_filters}) AND {status_clause}
                ORDER BY a.weight DESC, c.authority DESC LIMIT 30
                """,
                [*[f"%{term}%" for term in query_terms], *params],
            ).fetchall()
            for row in alias_like_rows:
                if repo_area and repo_area != "any" and row["repo_area"] != repo_area:
                    continue
                alias_terms = set(tokenize(str(row["alias"]) + " " + str(row["title"]) + " " + str(row["path"])))
                overlap = len(query_terms.intersection(alias_terms)) / max(1, len(query_terms))
                out.append(self.row_to_result(row, query, exact_score=max(0.55, min(0.85, overlap)), why=["partial alias match"]))
            path_rows = conn.execute(
                f"SELECT * FROM chunks WHERE lower(path) LIKE ? AND {status_clause} ORDER BY authority DESC LIMIT 20",
                [f"%{q}%", *params],
            ).fetchall()
            for row in path_rows:
                if repo_area and repo_area != "any" and row["repo_area"] != repo_area:
                    continue
                out.append(self.row_to_result(row, query, exact_score=0.85, why=["path match"]))
        return out

    def entity_candidates(self, query: str, repo_area: str | None, include_historical: bool) -> list[SearchResult]:
        q = query.strip().lower()
        terms = tokenize(query)
        if not terms:
            return []
        definition_intent = any(term in {"what", "what's", "cos", "cosa", "definition", "definizione", "define", "naming", "domain", "model", "term", "terms"} for term in terms)
        fts_query = " OR ".join(terms)
        out: list[SearchResult] = []
        with self.catalog.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.*, bm25(entities_fts) AS rank
                FROM entities_fts JOIN entities e ON e.entity_id=entities_fts.entity_id
                WHERE entities_fts MATCH ?
                ORDER BY rank LIMIT 30
                """,
                (fts_query,),
            ).fetchall()
            alias_rows = conn.execute(
                """
                SELECT e.*, a.weight, 0.0 AS rank
                FROM entity_aliases a JOIN entities e ON e.entity_id=a.entity_id
                WHERE lower(a.alias)=? OR lower(e.term)=?
                ORDER BY a.weight DESC LIMIT 20
                """,
                (q, q),
            ).fetchall()
            for row in [*alias_rows, *rows]:
                lexical = 1.0 / (1.0 + abs(float(row["rank"]))) if "rank" in row.keys() else 0.7
                exact = 0.95 if str(row["term"]).lower() == q else 0.75 if any(q == str(a).lower() for a in []) else 0.0
                boost = 0.12 if definition_intent else 0.0
                result = SearchResult(
                    path=row["source_path"],
                    title=row["term"],
                    status="canonical",
                    doc_type="definition",
                    repo_area=repo_area or "any",
                    authority=float(row["authority"]),
                    line_start=int(row["line_start"] or 1),
                    line_end=int(row["line_end"] or row["line_start"] or 1),
                    heading_path=[row["term"]],
                    anchor="",
                    snippet=snippet(row["definition"] or row["term"], query),
                    score=boost,
                    lexical_score=min(1.0, lexical + boost),
                    exact_score=exact,
                    authority_score=float(row["authority"]),
                    freshness_score=0.7,
                    why=["glossary/entity match"] + (["definition intent"] if definition_intent else []),
                    source_type="glossary",
                    text_for_rerank=f"Term: {row['term']}\nSource: {row['source_path']}\nDefinition:\n{row['definition']}",
                )
                out.append(result)
        return out

    def code_bridge_candidates(self, query: str, repo_area: str | None, doc_type: str | None, include_historical: bool, mode: str = "sections") -> list[SearchResult]:
        terms = tokenize(query)
        symbol_terms = [term for term in terms if self.looks_like_symbol(term)]
        if not symbol_terms:
            return []
        status_clause, params = self.allowed_status_clause(include_historical)
        matched_areas: set[str] = set()
        bridge_terms: set[str] = set(terms)
        with self.catalog.connect() as conn:
            for term in symbol_terms[:6]:
                rows = conn.execute(
                    """
                    SELECT symbol,repo_area,path FROM code_symbols
                    WHERE lower(symbol)=? OR lower(symbol) LIKE ?
                    UNION ALL
                    SELECT key AS symbol,repo_area,path FROM config_keys
                    WHERE lower(key)=? OR lower(key) LIKE ?
                    LIMIT 30
                    """,
                    (term, f"%{term}%", term, f"%{term}%"),
                ).fetchall()
                for row in rows:
                    area = str(row["repo_area"])
                    if repo_area and repo_area != "any" and area != repo_area:
                        continue
                    matched_areas.add(area)
                    bridge_terms.update(split_camel(str(row["symbol"])))
                    bridge_terms.update(tokenize(str(row["path"])))
            if not matched_areas:
                return []
            query_terms = [term for term in bridge_terms if len(term) > 2 and re.match(r"^[A-Za-z0-9_]+$", term)][:12]
            fts_query = " OR ".join(query_terms) or query
            area_filter = ""
            area_params: list[Any] = []
            if repo_area and repo_area != "any":
                area_filter = " AND c.repo_area=?"
                area_params.append(repo_area)
            else:
                area_filter = f" AND c.repo_area IN ({','.join('?' for _ in matched_areas)})"
                area_params.extend(sorted(matched_areas))
            doc_type_filter = ""
            doc_type_params: list[Any] = []
            if doc_type and doc_type != "any":
                doc_type_filter = " AND c.doc_type=?"
                doc_type_params.append(doc_type)
            try:
                rows = conn.execute(
                    f"""
                    SELECT c.*, bm25(chunks_fts) AS rank
                    FROM chunks_fts
                    JOIN chunks c ON c.chunk_id=chunks_fts.chunk_id
                    WHERE chunks_fts MATCH ? AND {status_clause}{area_filter}{doc_type_filter}
                    ORDER BY rank, c.authority DESC LIMIT 25
                    """,
                    [fts_query, *params, *area_params, *doc_type_params],
                ).fetchall()
            except Exception:
                rows = []
            out: list[SearchResult] = []
            for row in rows:
                lexical = 1.0 / (1.0 + abs(float(row["rank"]))) if "rank" in row.keys() else 0.35
                title_path = f"{row['title']} {row['path']}".lower()
                title_boost = 0.12 if any(term in title_path for term in terms if len(term) > 3) else 0.0
                result = self.row_to_result(row, query, lexical_score=min(1.0, lexical + title_boost), exact_score=0.50 + title_boost, why=["code symbol/config bridge", "repo-area match"])
                result.source_type = "document"
                out.append(result)
            return out

    def looks_like_symbol(self, term: str) -> bool:
        return bool(re.search(r"[A-Z_./-]", term)) or "_" in term or "/" in term or "." in term or term.lower().endswith(("controller", "handler", "service"))

    def row_to_result(self, row: Any, query: str, lexical_score: float = 0.0, exact_score: float = 0.0, why: list[str] | None = None) -> SearchResult:
        return SearchResult(
            path=row["path"],
            title=row["title"],
            status=row["status"],
            doc_type=row["doc_type"],
            repo_area=row["repo_area"],
            authority=float(row["authority"]),
            line_start=int(row["line_start"]),
            line_end=int(row["line_end"]),
            heading_path=json.loads(row["heading_path_json"] or "[]"),
            anchor=row["anchor"],
            snippet=snippet(row["text"], query),
            score=0.0,
            lexical_score=lexical_score,
            exact_score=exact_score,
            authority_score=float(row["authority"]),
            freshness_score=0.6,
            why=why or [],
            text_for_rerank=f"{row['title']}\n{' > '.join(json.loads(row['heading_path_json'] or '[]'))}\n{row['text'] or ''}",
        )

    def apply_scores(self, results: list[SearchResult], query: str) -> None:
        terms = set(tokenize(query))
        for r in results:
            route = 0.0
            if r.repo_area in terms or any(t in r.path.lower() for t in terms):
                route = 0.4
            r.route_match_score = route
            if r.status == "canonical":
                r.policy_adjustments.append("canonical_boost")
                policy = 0.08
            elif r.status == "runbook":
                policy = 0.05
            elif r.status == "generated":
                r.policy_adjustments.append("generated_lower_priority")
                policy = -0.04
            elif r.status == "historical":
                r.policy_adjustments.append("historical_suppressed")
                policy = -0.20
            else:
                policy = 0.0
            rrf = self.rrf_from_ranks(r.generator_ranks)
            r.score = max(
                0.0,
                min(
                    1.0,
                    0.28 * rrf
                    + 0.20 * r.lexical_score
                    + 0.20 * r.exact_score
                    + 0.13 * r.authority_score
                    + 0.10 * r.route_match_score
                    + 0.05 * r.freshness_score
                    + policy,
                ),
            )

    def rrf_from_ranks(self, ranks: dict[str, int]) -> float:
        if not ranks:
            return 0.0
        raw = sum(1.0 / (60 + int(rank)) for rank in ranks.values())
        return min(1.0, raw / (len(ranks) / 61.0))

    def try_rerank(self, query: str, results: list[SearchResult]) -> str | None:
        limit = min(int(self.config.data["retrieval"]["rerank_candidates"]), 100, len(results))
        if not limit:
            return None
        backend = self.backend or BgeM3LocalBackend.from_locator_config(self.config)
        pairs = [(query, r.text_for_rerank or f"{r.title}\n{' > '.join(r.heading_path)}\n{r.snippet}") for r in results[:limit]]
        scores = backend.rerank_pairs(pairs, normalize=True)
        for r, score in zip(results[:limit], scores):
            r.reranker_score = float(score)
            r.score = min(1.0, 0.65 * float(score) + 0.35 * r.score)
            if "reranker match" not in r.why:
                r.why.append("reranker match")
        return None

    def confidence(self, results: list[SearchResult], query: str) -> tuple[str, list[str], list[str]]:
        if not results:
            return "low", ["no semantic candidates found"], ["retry find_docs after index_status is fresh"]
        top = results[0]
        second = results[1].score if len(results) > 1 else 0.0
        margin = top.score - second
        reasons: list[str] = []
        if top.status in {"canonical", "runbook", "active"}:
            reasons.append(f"top result is {top.status}")
        if top.lexical_score > 0.2 and (top.exact_score > 0.0 or top.reranker_score is not None):
            reasons.append("lexical signal agrees with another signal")
        if top.exact_score >= 0.85:
            reasons.append("strong exact/title/alias match")
        if top.line_start > 0 and top.line_end >= top.line_start:
            reasons.append("valid line citation")
        if margin > 0.12:
            reasons.append("clear top-result margin")
        if top.reranker_score is not None and top.reranker_score >= 0.55:
            reasons.append("reranker score is strong")
        if top.dense_score > 0.0:
            reasons.append("dense vector signal present")
        if top.sparse_score > 0.0:
            reasons.append("sparse lexical vector signal present")
        if top.status in {"canonical", "runbook", "active"} and top.reranker_score is not None and top.reranker_score >= 0.55 and "valid line citation" in reasons:
            return "high", reasons, []
        if top.status in {"canonical", "runbook", "active"} and top.exact_score >= 0.85 and "valid line citation" in reasons:
            return "high", reasons, []
        if top.score >= float(self.config.data["confidence"]["high_min_score"]) and top.status in {"canonical", "runbook", "active"} and "valid line citation" in reasons:
            return "high", reasons, []
        if top.score >= float(self.config.data["confidence"]["medium_min_score"]):
            return "medium", reasons or ["plausible hybrid match"], []
        return "low", reasons or ["weak or ambiguous retrieval signals"], ["add alias/frontmatter or narrow the query"]

    def result_json(self, r: SearchResult, verbosity: str = "compact") -> dict[str, Any]:
        signals = {
            "reranker": score(r.reranker_score),
            "dense": score(r.dense_score),
            "sparse": score(r.sparse_score),
            "lexical": score(r.lexical_score),
            "exact": score(r.exact_score),
        }
        compact = {
            "source_type": r.source_type,
            "path": r.path,
            "title": r.title,
            "status": r.status,
            "doc_type": r.doc_type,
            "repo_area": r.repo_area,
            "score": score(r.score),
            "signals": signals,
            "why": sorted(set(r.why + (["canonical document"] if r.status == "canonical" else []))),
            "section": {"heading": " > ".join(r.heading_path), "line_start": r.line_start, "line_end": r.line_end},
            "citation": r.citation,
        }
        if r.snippet:
            compact["snippet"] = r.snippet[:220]
        if verbosity == "full":
            compact["authority"] = score(r.authority)
            compact["final_score"] = score(r.score)
            compact["signals"] = {
                **signals,
                "qdrant": score(max(r.dense_score, r.sparse_score)),
                "authority": score(r.authority_score),
                "route": score(r.route_match_score),
                "freshness": score(r.freshness_score),
            }
            compact["policy_adjustments"] = r.policy_adjustments
            compact["best_sections"] = [{"heading": " > ".join(r.heading_path), "anchor": r.anchor, "line_start": r.line_start, "line_end": r.line_end}]
        elif hasattr(r, "best_sections"):
            compact["best_sections"] = getattr(r, "best_sections")
        return compact

    def attach_best_sections(self, results: list[SearchResult], query: str, include_historical: bool) -> None:
        if not results:
            return
        status_clause, params = self.allowed_status_clause(include_historical)
        with self.catalog.connect() as conn:
            for result in results:
                rows = conn.execute(
                    f"SELECT * FROM chunks WHERE path=? AND {status_clause} ORDER BY line_start LIMIT 3",
                    [result.path, *params],
                ).fetchall()
                sections = []
                for row in rows:
                    sections.append({"heading": " > ".join(json.loads(row["heading_path_json"] or "[]")), "anchor": row["anchor"], "line_start": row["line_start"], "line_end": row["line_end"]})
                setattr(result, "best_sections", sections)

    def recommended_fix(self, results: list[SearchResult], debug: dict[str, Any]) -> str | None:
        if results:
            return None
        counts = debug.get("candidate_counts", {})
        if not counts.get("entity"):
            return "add glossary/entity alias or authority rule for this term"
        if not counts.get("vector"):
            return "rebuild semantic index and verify Qdrant collections"
        return "add aliases, frontmatter status, or narrower heading text"

    def index_state(self) -> dict[str, Any]:
        try:
            from .freshness import IndexFreshnessService

            status = IndexFreshnessService(self.config).status(allow_auto_start=False)
            return {"state": status.get("state"), "safe_to_use": status.get("safe_to_use"), "warnings": status.get("warnings", [])[:5]}
        except Exception as exc:
            return {"state": "unknown", "warning": str(exc)}

    def exact(self, term: str, repo_area: str | None = None, include_historical: bool = False, max_results: int = 20) -> dict[str, Any]:
        self.catalog.init()
        status_clause, params = self.allowed_status_clause(include_historical)
        results: list[dict[str, Any]] = []
        term_text = term.strip()
        term_lower = term_text.lower().replace("\\", "/").lstrip("/")
        q = f"%{term_text}%"
        seen: set[tuple[str, int, str]] = set()

        def add_result(item: dict[str, Any]) -> None:
            key = (str(item.get("path")), int(item.get("line_number") or 1), str(item.get("source_kind")))
            if key in seen or len(results) >= max_results:
                return
            seen.add(key)
            results.append(item)

        with self.catalog.connect() as conn:
            doc_rows = conn.execute(
                f"""
                SELECT d.path,d.title,d.status,d.repo_area,d.doc_type,d.authority,
                       COALESCE(MIN(c.line_start), 1) AS line_number
                FROM documents d
                LEFT JOIN chunks c ON c.document_id=d.document_id
                WHERE (lower(d.path)=? OR lower(d.path) LIKE ? OR lower(d.title)=? OR lower(d.title) LIKE ?) AND {status_clause.replace('status', 'd.status')}
                GROUP BY d.path
                ORDER BY d.authority DESC, d.path
                LIMIT ?
                """,
                [term_lower, f"%{term_lower}%", term_lower, f"%{term_lower}%", *params, max_results],
            ).fetchall()
            for row in doc_rows:
                if repo_area and repo_area != "any" and row["repo_area"] != repo_area:
                    continue
                add_result(
                    {
                        "path": row["path"],
                        "line_number": row["line_number"],
                        "snippet": row["title"],
                        "source_kind": "catalog_path" if term_lower in str(row["path"]).lower() else "catalog_title",
                        "related_canonical_docs": [row["path"]] if row["status"] in {"canonical", "runbook"} else [],
                    }
                )
            rows = conn.execute(
                f"SELECT path,line_start AS line_number,text AS snippet,'markdown' source_kind,title,status FROM chunks WHERE (text LIKE ? OR title LIKE ? OR path LIKE ?) AND {status_clause} LIMIT ?",
                [q, q, q, *params, max_results],
            ).fetchall()
            for row in rows:
                if repo_area and repo_area != "any":
                    doc = conn.execute("SELECT repo_area FROM documents WHERE path=?", (row["path"],)).fetchone()
                    if doc and doc["repo_area"] != repo_area:
                        continue
                add_result({"path": row["path"], "line_number": row["line_number"], "snippet": snippet(row["snippet"], term), "source_kind": row["source_kind"], "related_canonical_docs": []})
            remaining = max_results - len(results)
            if remaining > 0:
                file_rows = conn.execute(
                    """
                    SELECT path,1 AS line_number,path AS snippet,'source_path' AS source_kind,repo_area,source_repo
                    FROM source_files
                    WHERE lower(path)=? OR lower(path) LIKE ?
                    ORDER BY path LIMIT ?
                    """,
                    (term_lower, f"%{term_lower}%", remaining),
                ).fetchall()
                for row in file_rows:
                    if repo_area and repo_area != "any" and row["repo_area"] != repo_area:
                        continue
                    if not self.source_path_allowed(conn, row["path"], include_historical):
                        continue
                    add_result({"path": row["path"], "line_number": row["line_number"], "snippet": row["snippet"], "source_kind": row["source_kind"], "related_canonical_docs": self.related_docs_for_source(conn, row["path"], row["repo_area"], term_text)})
            remaining = max_results - len(results)
            if remaining > 0:
                sym_rows = conn.execute(
                    """
                    SELECT symbol,symbol_type,path,line_number,repo_area,source_repo,source_kind,context
                    FROM code_symbols
                    WHERE lower(symbol)=? OR symbol LIKE ?
                    ORDER BY symbol_type,path LIMIT ?
                    """,
                    (term_text.lower(), q, remaining),
                ).fetchall()
                for row in sym_rows:
                    if repo_area and repo_area != "any" and row["repo_area"] != repo_area:
                        continue
                    add_result({"path": row["path"], "line_number": row["line_number"], "snippet": row["context"] or row["symbol"], "source_kind": "code_symbol", "related_canonical_docs": self.related_docs_for_source(conn, row["path"], row["repo_area"], term_text)})
            remaining = max_results - len(results)
            if remaining > 0:
                key_rows = conn.execute(
                    """
                    SELECT key,key_type,path,line_number,repo_area,source_repo,source_kind,context
                    FROM config_keys
                    WHERE lower(key)=? OR key LIKE ?
                    ORDER BY key_type,path LIMIT ?
                    """,
                    (term_text.lower(), q, remaining),
                ).fetchall()
                for row in key_rows:
                    if repo_area and repo_area != "any" and row["repo_area"] != repo_area:
                        continue
                    add_result({"path": row["path"], "line_number": row["line_number"], "snippet": row["context"] or row["key"], "source_kind": "config_key", "related_canonical_docs": self.related_docs_for_source(conn, row["path"], row["repo_area"], term_text)})
            remaining = max_results - len(results)
            if remaining > 0:
                fts_query = " OR ".join(tokenize(term_text)) or term_text
                try:
                    source_rows = conn.execute(
                        """
                        SELECT path,line_number,text AS snippet,source_kind,repo_area,source_repo,bm25(source_lines_fts) AS rank
                        FROM source_lines_fts
                        WHERE source_lines_fts MATCH ?
                        ORDER BY rank LIMIT ?
                        """,
                        (fts_query, remaining),
                    ).fetchall()
                except Exception:
                    source_rows = []
                for row in source_rows:
                    if repo_area and repo_area != "any" and row["repo_area"] != repo_area:
                        continue
                    if not self.source_path_allowed(conn, row["path"], include_historical):
                        continue
                    add_result({"path": row["path"], "line_number": row["line_number"], "snippet": snippet(row["snippet"], term), "source_kind": row["source_kind"], "related_canonical_docs": self.related_docs_for_source(conn, row["path"], row["repo_area"], term_text)})
            remaining = max_results - len(results)
            if remaining > 0:
                entity_rows = conn.execute(
                    """
                    SELECT e.source_path AS path,e.line_start AS line_number,e.term,e.definition,'glossary' AS source_kind
                    FROM entities e
                    LEFT JOIN entity_aliases a ON a.entity_id=e.entity_id
                    WHERE lower(e.term)=? OR lower(a.alias)=? OR e.definition LIKE ?
                    ORDER BY e.authority DESC LIMIT ?
                    """,
                    (term_text.lower(), term_text.lower(), q, remaining),
                ).fetchall()
                for row in entity_rows:
                    add_result({"path": row["path"], "line_number": row["line_number"], "snippet": snippet(f"{row['term']}: {row['definition']}", term), "source_kind": row["source_kind"], "related_canonical_docs": []})
            remaining = max_results - len(results)
            if remaining > 0:
                route_rows = conn.execute("SELECT target_path AS path,topic,route_name,'route' AS source_kind FROM routes WHERE lower(topic)=? OR topic LIKE ? OR target_path LIKE ? ORDER BY priority LIMIT ?", (term_text.lower(), q, q, remaining)).fetchall()
                for row in route_rows:
                    add_result({"path": row["path"], "line_number": 1, "snippet": row["topic"], "source_kind": row["source_kind"], "related_canonical_docs": [row["path"]]})
        conf = "high" if results and any(r["source_kind"] in {"catalog_path", "catalog_title", "source_path", "code_symbol", "config_key"} or term.lower() in str(r["snippet"]).lower() for r in results[:3]) else "medium" if results else "low"
        return {"term": term, "confidence": conf, "results": results[:max_results]}

    def source_path_allowed(self, conn: Any, path: str, include_historical: bool) -> bool:
        doc = conn.execute("SELECT status FROM documents WHERE path=?", (path,)).fetchone()
        if not doc:
            return True
        excluded = set(self.config.data["policy"]["exclude_by_default"])
        if not include_historical and self.config.data["policy"].get("historical_requires_flag", True):
            excluded.add("historical")
        return str(doc["status"]) not in excluded

    def related_docs_for_source(self, conn: Any, source_path: str, repo_area: str, term: str, limit: int = 5) -> list[str]:
        terms = [part for part in tokenize(term) + tokenize(source_path) if len(part) > 2]
        if not terms:
            return []
        like_filters = " OR ".join(["lower(title) LIKE ? OR lower(path) LIKE ? OR lower(text) LIKE ?" for _ in terms[:6]])
        params: list[Any] = []
        for value in terms[:6]:
            params.extend([f"%{value.lower()}%", f"%{value.lower()}%", f"%{value.lower()}%"])
        rows = conn.execute(
            f"""
            SELECT path, MAX(authority) AS authority
            FROM chunks
            WHERE repo_area=? AND status IN ('canonical','runbook','active') AND ({like_filters})
            GROUP BY path
            ORDER BY authority DESC, path LIMIT ?
            """,
            [repo_area, *params, limit],
        ).fetchall()
        return [str(row["path"]) for row in rows]

    def open_doc(self, path: str, heading: str | None = None, line_start: int | None = None, line_end: int | None = None, max_chars: int = 12000) -> dict[str, Any]:
        normalized = path.replace("\\", "/").lstrip("/")
        target = (self.config.root / normalized).resolve()
        try:
            target.relative_to(self.config.root.resolve())
        except ValueError as exc:
            raise ValueError("path outside workspace is blocked")
        doc = self.catalog.doc(normalized)
        if doc is None:
            raise FileNotFoundError(f"{normalized} is not in the locator catalog")
        if not target.exists():
            raise FileNotFoundError(normalized)
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
        start = int(line_start or 1)
        end = int(line_end or len(lines))
        if heading:
            for idx, line in enumerate(lines, start=1):
                if heading.lower() in line.lower() and line.lstrip().startswith("#"):
                    start = idx
                    end = next((j - 1 for j in range(idx + 1, len(lines) + 1) if lines[j - 1].lstrip().startswith("#")), len(lines))
                    break
        start = max(1, start)
        end = min(len(lines), max(start, end))
        hard_max = 50000
        max_chars = max(1, min(int(max_chars or 12000), hard_max))
        content = "\n".join(lines[start - 1 : end])
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]
        return {
            "path": normalized,
            "title": doc.get("title") or Path(normalized).stem,
            "status": doc.get("status") or "unknown",
            "doc_type": doc.get("doc_type") or "unknown",
            "repo_area": doc.get("repo_area") or "unknown",
            "content": content,
            "line_start": start,
            "line_end": end,
            "truncated": truncated,
            "max_chars": max_chars,
            "citations": [{"path": normalized, "line_start": start, "line_end": end}],
        }

    def list_canonical(self, repo_area: str | None = None, topic: str | None = None) -> dict[str, Any]:
        self.catalog.init()
        params: list[Any] = ["canonical", "runbook"]
        filters = ["status IN (?,?)"]
        if repo_area and repo_area != "any":
            filters.append("repo_area=?")
            params.append(repo_area)
        if topic:
            filters.append("(lower(title) LIKE ? OR lower(path) LIKE ? OR lower(canonical_for_json) LIKE ?)")
            params.extend([f"%{topic.lower()}%"] * 3)
        with self.catalog.connect() as conn:
            rows = conn.execute(f"SELECT * FROM documents WHERE {' AND '.join(filters)} ORDER BY authority DESC,title LIMIT 100", params).fetchall()
        return {"results": [{"path": r["path"], "title": r["title"], "repo_area": r["repo_area"], "doc_type": r["doc_type"], "canonical_for": json.loads(r["canonical_for_json"] or "[]"), "aliases": json.loads(r["aliases_json"] or "[]")} for r in rows]}

    def neighbors(self, path: str, include_historical: bool = False) -> dict[str, Any]:
        normalized = path.replace("\\", "/")
        with self.catalog.connect() as conn:
            out = [dict(r) for r in conn.execute("SELECT target_path,link_text,link_type,line_number FROM links WHERE source_path=?", (normalized,))]
            inc = [dict(r) for r in conn.execute("SELECT source_path,link_text,link_type,line_number FROM links WHERE target_path LIKE ?", (f"%{normalized}%",))]
            doc = conn.execute("SELECT supersedes_json,replaced_by,repo_area FROM documents WHERE path=?", (normalized,)).fetchone()
            area = doc["repo_area"] if doc else None
            related = []
            if area:
                rows = conn.execute("SELECT path FROM documents WHERE repo_area=? AND path<>? AND status IN ('canonical','runbook') LIMIT 10", (area, normalized)).fetchall()
                related = [{"path": r["path"], "relation": "same_topic"} for r in rows]
        return {"path": normalized, "links_out": out, "links_in": inc, "supersedes": json.loads(doc["supersedes_json"] or "[]") if doc else [], "replaced_by": doc["replaced_by"] if doc else None, "related_docs": related}

    def explain(self, query: str, path: str | None = None) -> dict[str, Any]:
        result = self.search(query, max_results=20, verbosity="full")
        normalized = path.replace("\\", "/") if path else None
        match = next((r for r in result["results"] if normalized and r["path"] == normalized), None)
        return {
            "query": query,
            "path": path,
            "explanation": {
                "matched_by": match["why"] if match else [item for item, count in result.get("debug", {}).get("candidate_counts", {}).items() if count],
                "scores": match["signals"] if match else {},
                "policy": match.get("policy_adjustments", []) if match else [],
                "is_safe_as_canonical": bool(match and match["status"] in {"canonical", "runbook"}),
                "warnings": [] if match else (["path_not_in_top_results"] if path else ["no_path_requested"]),
                "debug": result.get("debug", {}),
            },
        }

