from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import LocatorConfig
from .entities import parse_entities
from .markdown import discover_markdown, git_commit, load_manifest_context, parse_document, rel_path
from .model import Chunk, Document
from .source_index import discover_source_files, extract_code_symbols, extract_config_keys, read_text, redact_line, source_file_for
from .vector import VectorIndex


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  document_id TEXT PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  doc_type TEXT NOT NULL,
  repo_area TEXT NOT NULL,
  authority REAL NOT NULL,
  owner TEXT,
  aliases_json TEXT,
  canonical_for_json TEXT,
  supersedes_json TEXT,
  replaced_by TEXT,
  last_reviewed TEXT,
  review_status TEXT,
  content_hash TEXT,
  git_commit TEXT,
  last_modified TEXT,
  frontmatter_json TEXT,
  warnings_json TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  path TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  doc_type TEXT NOT NULL,
  repo_area TEXT NOT NULL,
  authority REAL NOT NULL,
  heading_path_json TEXT,
  anchor TEXT,
  line_start INTEGER,
  line_end INTEGER,
  text TEXT,
  text_for_embedding TEXT,
  token_estimate INTEGER,
  content_hash TEXT,
  embedding_model TEXT,
  chunker_version TEXT,
  git_commit TEXT,
  last_modified TEXT,
  created_at TEXT,
  updated_at TEXT,
  FOREIGN KEY(document_id) REFERENCES documents(document_id)
);
CREATE TABLE IF NOT EXISTS links (
  source_path TEXT,
  target_path TEXT,
  link_text TEXT,
  link_type TEXT,
  line_number INTEGER
);
CREATE TABLE IF NOT EXISTS aliases (
  alias TEXT,
  document_id TEXT,
  path TEXT,
  weight REAL
);
CREATE TABLE IF NOT EXISTS routes (
  route_id TEXT,
  route_name TEXT,
  repo_area TEXT,
  topic TEXT,
  target_path TEXT,
  priority INTEGER
);
CREATE TABLE IF NOT EXISTS symbols (
  symbol TEXT,
  symbol_type TEXT,
  path TEXT,
  line_number INTEGER,
  repo_area TEXT,
  source_kind TEXT
);
CREATE TABLE IF NOT EXISTS source_files (
  path TEXT PRIMARY KEY,
  source_repo TEXT,
  repo_area TEXT,
  extension TEXT,
  source_kind TEXT,
  size_bytes INTEGER,
  content_hash TEXT,
  last_modified TEXT,
  indexed_at TEXT
);
CREATE TABLE IF NOT EXISTS code_symbols (
  symbol TEXT,
  symbol_type TEXT,
  path TEXT,
  line_number INTEGER,
  repo_area TEXT,
  source_repo TEXT,
  source_kind TEXT,
  container TEXT,
  signature TEXT,
  context TEXT
);
CREATE TABLE IF NOT EXISTS config_keys (
  key TEXT,
  key_type TEXT,
  path TEXT,
  line_number INTEGER,
  repo_area TEXT,
  source_repo TEXT,
  source_kind TEXT,
  context TEXT
);
CREATE TABLE IF NOT EXISTS entities (
  entity_id TEXT PRIMARY KEY,
  term TEXT NOT NULL,
  entity_type TEXT,
  definition TEXT,
  source_path TEXT NOT NULL,
  line_start INTEGER,
  line_end INTEGER,
  canonical_docs_json TEXT,
  authority REAL
);
CREATE TABLE IF NOT EXISTS entity_aliases (
  alias TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  source_path TEXT NOT NULL,
  weight REAL
);
CREATE TABLE IF NOT EXISTS index_runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT,
  completed_at TEXT,
  git_commit TEXT,
  embedding_model TEXT,
  embedding_backend TEXT,
  reranker_model TEXT,
  reranker_backend TEXT,
  chunker_version TEXT,
  docs_count INTEGER,
  chunks_count INTEGER,
  errors_json TEXT,
  warnings_json TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, path UNINDEXED, title, heading, text, text_for_embedding);
CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(entity_id UNINDEXED, term, aliases, definition, source_path UNINDEXED);
CREATE VIRTUAL TABLE IF NOT EXISTS source_lines_fts USING fts5(path UNINDEXED, line_number UNINDEXED, text, source_kind UNINDEXED, repo_area UNINDEXED, source_repo UNINDEXED);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Catalog:
    def __init__(self, config: LocatorConfig):
        self.config = config
        self.path = config.sqlite_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        return self.connect_to(self.path)

    def connect_to(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for column, definition in [
                ("embedding_backend", "TEXT"),
                ("reranker_backend", "TEXT"),
            ]:
                existing = [row[1] for row in conn.execute("PRAGMA table_info(index_runs)").fetchall()]
                if column not in existing:
                    conn.execute(f"ALTER TABLE index_runs ADD COLUMN {column} {definition}")

    def rebuild(self) -> dict[str, Any]:
        self.init()
        started = now()
        run_id = started.replace(":", "").replace(".", "")
        warnings: list[str] = []
        errors: list[str] = []
        commit = git_commit(self.config.root)
        nav_paths, generated_status = load_manifest_context(self.config)
        docs: list[Document] = []
        chunks: list[Chunk] = []
        links: list[dict[str, Any]] = []
        previous_index_runs = self.index_runs_snapshot()
        for path in discover_markdown(self.config):
            try:
                doc, doc_chunks, doc_links = parse_document(path, self.config, nav_paths, generated_status, commit)
                docs.append(doc)
                chunks.extend(doc_chunks)
                links.extend(doc_links)
                warnings.extend(f"{doc.path}: {w}" for w in doc.warnings)
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        temp_path = self.path.with_name(f"{self.path.stem}.{run_id}.build.sqlite")
        self.remove_sqlite_files(temp_path)
        with self.connect_to(temp_path) as conn:
            conn.executescript(SCHEMA)
            conn.executescript("DELETE FROM documents; DELETE FROM chunks; DELETE FROM links; DELETE FROM aliases; DELETE FROM routes; DELETE FROM symbols; DELETE FROM source_files; DELETE FROM code_symbols; DELETE FROM config_keys; DELETE FROM entities; DELETE FROM entity_aliases; DELETE FROM chunks_fts; DELETE FROM entities_fts; DELETE FROM source_lines_fts;")
            self.restore_index_runs(conn, previous_index_runs)
            for doc in docs:
                self.upsert_document(conn, doc)
            for chunk in chunks:
                self.upsert_chunk(conn, chunk)
            for link in links:
                conn.execute(
                    "INSERT INTO links(source_path,target_path,link_text,link_type,line_number) VALUES(?,?,?,?,?)",
                    (link["source_path"], link["target_path"], link["link_text"], link["link_type"], link["line_number"]),
                )
            self.load_manual_aliases(conn)
            self.load_routes(conn)
            self.load_entities(conn)
            self.index_source_files(conn)

        # Build the catalog in a side database first. If parsing/indexing is interrupted,
        # the previously usable catalog remains untouched.
        with self.connect_to(temp_path) as source, self.connect() as target:
            source.backup(target)
        self.remove_sqlite_files(temp_path)

        # Commit the deterministic SQLite catalog before the heavier vector rebuild.
        # This keeps exact/path lookups usable while Qdrant is still being populated.
        with self.connect() as conn:
            vector_result = VectorIndex(self.config).rebuild_from_sqlite(conn)

        with self.connect() as conn:
            conn.execute(
                "INSERT INTO index_runs(run_id,started_at,completed_at,git_commit,embedding_model,embedding_backend,reranker_model,reranker_backend,chunker_version,docs_count,chunks_count,errors_json,warnings_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    started,
                    now(),
                    commit,
                    self.config.embedding_model,
                    self.config.embedding_backend,
                    self.config.reranker_model,
                    self.config.reranker_backend,
                    self.config.chunker_version,
                    len(docs),
                    len(chunks),
                    json.dumps(errors),
                    json.dumps(warnings[:500]),
                ),
            )
        return {"docs": len(docs), "chunks": len(chunks), "warnings": warnings, "errors": errors, "sqlite": str(self.path), "qdrant": vector_result}

    def index_runs_snapshot(self) -> list[dict[str, Any]]:
        try:
            with self.connect() as conn:
                return [dict(row) for row in conn.execute("SELECT * FROM index_runs ORDER BY completed_at")]
        except Exception:
            return []

    def restore_index_runs(self, conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO index_runs(run_id,started_at,completed_at,git_commit,embedding_model,embedding_backend,reranker_model,reranker_backend,chunker_version,docs_count,chunks_count,errors_json,warnings_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row.get("run_id"),
                    row.get("started_at"),
                    row.get("completed_at"),
                    row.get("git_commit"),
                    row.get("embedding_model"),
                    row.get("embedding_backend"),
                    row.get("reranker_model"),
                    row.get("reranker_backend"),
                    row.get("chunker_version"),
                    row.get("docs_count"),
                    row.get("chunks_count"),
                    row.get("errors_json"),
                    row.get("warnings_json"),
                ),
            )

    def remove_sqlite_files(self, path: Path) -> None:
        for candidate in [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]:
            try:
                candidate.unlink(missing_ok=True)
            except Exception:
                pass

    def update(self) -> dict[str, Any]:
        # MVP keeps update deterministic by rebuilding. The hash schema is already present for a future delta pass.
        result = self.rebuild()
        result["mode"] = "full-rebuild-mvp"
        return result

    def upsert_document(self, conn: sqlite3.Connection, doc: Document) -> None:
        ts = now()
        conn.execute(
            """
            INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(document_id) DO UPDATE SET
              path=excluded.path,title=excluded.title,status=excluded.status,doc_type=excluded.doc_type,repo_area=excluded.repo_area,
              authority=excluded.authority,aliases_json=excluded.aliases_json,canonical_for_json=excluded.canonical_for_json,
              supersedes_json=excluded.supersedes_json,replaced_by=excluded.replaced_by,last_reviewed=excluded.last_reviewed,
              review_status=excluded.review_status,content_hash=excluded.content_hash,git_commit=excluded.git_commit,
              last_modified=excluded.last_modified,frontmatter_json=excluded.frontmatter_json,warnings_json=excluded.warnings_json,updated_at=excluded.updated_at
            """,
            (
                doc.document_id,
                doc.path,
                doc.title,
                doc.status,
                doc.doc_type,
                doc.repo_area,
                doc.authority,
                doc.owner,
                json.dumps(doc.aliases),
                json.dumps(doc.canonical_for),
                json.dumps(doc.supersedes),
                doc.replaced_by,
                doc.last_reviewed,
                doc.review_status,
                doc.content_hash,
                doc.git_commit,
                doc.last_modified,
                json.dumps(doc.frontmatter),
                json.dumps(doc.warnings),
                ts,
                ts,
            ),
        )
        for alias in doc.aliases + doc.canonical_for + [doc.title]:
            conn.execute("INSERT INTO aliases(alias,document_id,path,weight) VALUES(?,?,?,?)", (alias, doc.document_id, doc.path, 1.0 if alias == doc.title else 0.8))

    def upsert_chunk(self, conn: sqlite3.Connection, chunk: Chunk) -> None:
        ts = now()
        heading = " > ".join(chunk.heading_path)
        conn.execute(
            """
            INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chunk_id) DO UPDATE SET text=excluded.text,text_for_embedding=excluded.text_for_embedding,content_hash=excluded.content_hash,updated_at=excluded.updated_at
            """,
            (
                chunk.chunk_id,
                chunk.document_id,
                chunk.path,
                chunk.title,
                chunk.status,
                chunk.doc_type,
                chunk.repo_area,
                chunk.authority,
                json.dumps(chunk.heading_path),
                chunk.anchor,
                chunk.line_start,
                chunk.line_end,
                chunk.text,
                chunk.text_for_embedding,
                chunk.token_estimate,
                chunk.content_hash,
                chunk.embedding_model,
                chunk.chunker_version,
                chunk.git_commit,
                chunk.last_modified,
                ts,
                ts,
            ),
        )
        conn.execute("INSERT INTO chunks_fts(chunk_id,path,title,heading,text,text_for_embedding) VALUES(?,?,?,?,?,?)", (chunk.chunk_id, chunk.path, chunk.title, heading, chunk.text, chunk.text_for_embedding))

    def load_routes(self, conn: sqlite3.Connection) -> None:
        priority = 0
        for routes_path in self.config.configured_files("route_files"):
            if not routes_path.exists():
                continue
            try:
                data = json.loads(routes_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for route in data.get("routes", []):
                route_name = str(route.get("intent") or route.get("name") or "route")
                for item in route.get("entrypoints", []):
                    priority += 1
                    path = str(item.get("path", "")).replace("\\", "/")
                    repo_area = str(item.get("repo") or item.get("repo_area") or "any")
                    topic = " ".join([route_name, str(item.get("title", "")), str(item.get("surface", ""))]).strip()
                    conn.execute("INSERT INTO routes(route_id,route_name,repo_area,topic,target_path,priority) VALUES(?,?,?,?,?,?)", (f"{route_name}:{priority}", route_name, repo_area, topic, path, priority))

    def load_manual_aliases(self, conn: sqlite3.Connection) -> None:
        priority = 1000
        for aliases_path in self.config.configured_files("alias_files"):
            if not aliases_path.exists():
                continue
            try:
                data = json.loads(aliases_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for item in data.get("aliases", []):
                target = str(item.get("target_path", "")).replace("\\", "/")
                if not target:
                    continue
                doc = conn.execute("SELECT document_id,path,repo_area FROM documents WHERE path=?", (target,)).fetchone()
                if not doc:
                    continue
                weight = float(item.get("weight", 1.0))
                repo_area = str(item.get("repo_area") or doc["repo_area"])
                for alias in item.get("aliases", []):
                    alias_text = str(alias).strip()
                    if not alias_text:
                        continue
                    conn.execute("INSERT INTO aliases(alias,document_id,path,weight) VALUES(?,?,?,?)", (alias_text, doc["document_id"], doc["path"], weight))
                    priority += 1
                    conn.execute(
                        "INSERT INTO routes(route_id,route_name,repo_area,topic,target_path,priority) VALUES(?,?,?,?,?,?)",
                        (f"manual-alias:{priority}", "manual-alias", repo_area, alias_text, doc["path"], priority),
                    )

    def load_entities(self, conn: sqlite3.Connection) -> None:
        for entity in parse_entities(self.config):
            if not entity.term:
                continue
            entity_id = f"{entity.source_path}#{entity.term}".lower()
            aliases = sorted(set([entity.term, *entity.aliases]))
            conn.execute(
                "INSERT INTO entities(entity_id,term,entity_type,definition,source_path,line_start,line_end,canonical_docs_json,authority) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    entity.term,
                    entity.entity_type,
                    entity.definition,
                    entity.source_path,
                    entity.line_start,
                    entity.line_end,
                    json.dumps(entity.canonical_docs),
                    entity.authority,
                ),
            )
            for alias in aliases:
                conn.execute("INSERT INTO entity_aliases(alias,entity_id,source_path,weight) VALUES(?,?,?,?)", (alias, entity_id, entity.source_path, 1.0 if alias == entity.term else 0.85))
            conn.execute(
                "INSERT INTO entities_fts(entity_id,term,aliases,definition,source_path) VALUES(?,?,?,?,?)",
                (entity_id, entity.term, " ".join(aliases), entity.definition, entity.source_path),
            )

    def index_source_files(self, conn: sqlite3.Connection) -> None:
        line_max_bytes = int(self.config.data.get("paths", {}).get("source_line_index_max_bytes", 262144))
        ts = now()
        for path in discover_source_files(self.config):
            try:
                source = source_file_for(self.config, path)
                conn.execute(
                    "INSERT INTO source_files(path,source_repo,repo_area,extension,source_kind,size_bytes,content_hash,last_modified,indexed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (source.path, source.source_repo, source.repo_area, source.extension, source.source_kind, source.size_bytes, source.content_hash, source.last_modified, ts),
                )
                text = read_text(path)
                lines = text.splitlines()
                if source.size_bytes <= line_max_bytes:
                    for line_no, line in enumerate(lines, start=1):
                        if line.strip():
                            conn.execute(
                                "INSERT INTO source_lines_fts(path,line_number,text,source_kind,repo_area,source_repo) VALUES(?,?,?,?,?,?)",
                                (source.path, line_no, redact_line(line)[:2000], source.source_kind, source.repo_area, source.source_repo),
                            )
                for symbol in extract_code_symbols(source, lines):
                    conn.execute(
                        "INSERT INTO code_symbols(symbol,symbol_type,path,line_number,repo_area,source_repo,source_kind,container,signature,context) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (symbol.symbol, symbol.symbol_type, symbol.path, symbol.line_number, symbol.repo_area, symbol.source_repo, symbol.source_kind, symbol.container, symbol.signature, symbol.context),
                    )
                    conn.execute(
                        "INSERT INTO symbols(symbol,symbol_type,path,line_number,repo_area,source_kind) VALUES(?,?,?,?,?,?)",
                        (symbol.symbol, symbol.symbol_type, symbol.path, symbol.line_number, symbol.repo_area, symbol.source_kind),
                    )
                for key in extract_config_keys(source, lines):
                    conn.execute(
                        "INSERT INTO config_keys(key,key_type,path,line_number,repo_area,source_repo,source_kind,context) VALUES(?,?,?,?,?,?,?,?)",
                        (key.key, key.key_type, key.path, key.line_number, key.repo_area, key.source_repo, key.source_kind, key.context),
                    )
                    conn.execute(
                        "INSERT INTO symbols(symbol,symbol_type,path,line_number,repo_area,source_kind) VALUES(?,?,?,?,?,?)",
                        (key.key, key.key_type, key.path, key.line_number, key.repo_area, key.source_kind),
                    )
            except Exception:
                continue

    def stats(self) -> dict[str, Any]:
        self.init()
        with self.connect() as conn:
            last_run = conn.execute("SELECT * FROM index_runs ORDER BY completed_at DESC LIMIT 1").fetchone()
            return {
                "sqlite": str(self.path),
                "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                "links": conn.execute("SELECT COUNT(*) FROM links").fetchone()[0],
                "symbols": conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
                "source_files": conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
                "code_symbols": conn.execute("SELECT COUNT(*) FROM code_symbols").fetchone()[0],
                "config_keys": conn.execute("SELECT COUNT(*) FROM config_keys").fetchone()[0],
                "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                "by_status": [dict(r) for r in conn.execute("SELECT status, COUNT(*) count FROM documents GROUP BY status ORDER BY count DESC")],
                "last_run": dict(last_run) if last_run else None,
            }

    def doc(self, path: str) -> dict[str, Any] | None:
        self.init()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE path=? OR path=?", (path, path.replace("\\", "/"))).fetchone()
            return dict(row) if row else None

    def chunks_for_doc(self, path: str) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT chunk_id,heading_path_json,line_start,line_end,token_estimate FROM chunks WHERE path=? ORDER BY line_start", (path.replace("\\", "/"),))]

