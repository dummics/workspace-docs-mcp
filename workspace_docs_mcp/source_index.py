from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import LocatorConfig
from .markdown import infer_repo_area, modified_time, rel_path


DEFAULT_TEXT_EXTENSIONS = {
    ".asmdef",
    ".cs",
    ".cshtml",
    ".css",
    ".editorconfig",
    ".env",
    ".example",
    ".fs",
    ".fsx",
    ".gitignore",
    ".html",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".md",
    ".mjs",
    ".props",
    ".ps1",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


CODE_EXTENSIONS = {".cs", ".fs", ".fsx", ".js", ".jsx", ".mjs", ".py", ".ts", ".tsx"}
CONFIG_EXTENSIONS = {".asmdef", ".env", ".example", ".json", ".props", ".ps1", ".sh", ".toml", ".xml", ".yaml", ".yml"}
HARD_EXCLUDES = {
    ".cache",
    ".git",
    ".next",
    ".pytest_cache",
    ".rag",
    ".turbo",
    ".venv",
    ".vs",
    ".work",
    "__pycache__",
    "bin",
    "build",
    "coverage",
    "dist",
    "library",
    "node_modules",
    "obj",
    "out",
    "temp",
    "testresults",
}
SENSITIVE_NAME_RE = re.compile(r"(password|passwd|pwd|secret|token|apikey|api_key|private|credential)", re.IGNORECASE)


@dataclass(frozen=True)
class SourceFile:
    path: str
    source_repo: str
    repo_area: str
    extension: str
    source_kind: str
    size_bytes: int
    content_hash: str
    last_modified: str


@dataclass(frozen=True)
class CodeSymbol:
    symbol: str
    symbol_type: str
    path: str
    line_number: int
    repo_area: str
    source_repo: str
    source_kind: str
    container: str
    signature: str
    context: str


@dataclass(frozen=True)
class ConfigKey:
    key: str
    key_type: str
    path: str
    line_number: int
    repo_area: str
    source_repo: str
    source_kind: str
    context: str


def configured_text_extensions(config: LocatorConfig) -> set[str]:
    values = config.data.get("paths", {}).get("source_file_extensions")
    if not values:
        return set(DEFAULT_TEXT_EXTENSIONS)
    return {str(value).lower() if str(value).startswith(".") else f".{str(value).lower()}" for value in values}


def source_roots(config: LocatorConfig) -> list[Path]:
    configured = config.data.get("paths", {}).get("source_roots")
    if configured:
        return [(config.root / str(path)).resolve() for path in configured]
    roots = [config.root]
    roots.extend(path.resolve() for path in config.code_roots() if path.exists())
    roots.extend(path.resolve() for path in config.docs_roots() if path.exists())
    roots.extend(path.resolve() for path in config.manifest_files() if path.exists())
    return sorted(set(roots))


def is_excluded(config: LocatorConfig, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(config.root.resolve())
    except ValueError:
        return True
    excludes = {str(value).lower() for value in config.data["paths"].get("exclude", [])}.union(HARD_EXCLUDES)
    parts = {part.lower() for part in relative.parts}
    return bool(parts.intersection(excludes))


def discover_source_files(config: LocatorConfig) -> list[Path]:
    extensions = configured_text_extensions(config)
    max_bytes = int(config.data.get("paths", {}).get("source_max_file_bytes", 524288))
    excludes = {str(value).lower() for value in config.data["paths"].get("exclude", [])}.union(HARD_EXCLUDES)
    results: list[Path] = []
    seen: set[Path] = set()
    for root in source_roots(config):
        if not root.exists() or is_excluded(config, root):
            continue
        if root.is_file():
            files: Iterable[Path] = [root]
        else:
            walked: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if name.lower() not in excludes]
                current = Path(dirpath)
                if is_excluded(config, current):
                    dirnames[:] = []
                    continue
                walked.extend(current / name for name in filenames)
            files = walked
        for path in files:
            resolved = path.resolve()
            if not path.is_file() or resolved in seen:
                continue
            if is_excluded(config, path):
                continue
            suffix = path.suffix.lower()
            if suffix not in extensions:
                continue
            try:
                if path.stat().st_size > max_bytes:
                    continue
            except OSError:
                continue
            seen.add(resolved)
            results.append(path)
    return sorted(results)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def infer_source_repo(path: str) -> str:
    normalized = path.replace("\\", "/").lower()
    if normalized.startswith("server-repo/"):
        return "server"
    if normalized.startswith("assets/flowyes licensing system/"):
        return "client"
    if normalized.startswith("server/") or "/server/" in normalized or "/backend/" in normalized:
        return "server"
    if normalized.startswith("client/") or "/client/" in normalized or "/frontend/" in normalized:
        return "client"
    return "meta"


def infer_source_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in CODE_EXTENSIONS:
        return "code"
    if suffix in CONFIG_EXTENSIONS:
        return "config"
    if suffix == ".md":
        return "markdown"
    return "text"


def source_file_for(config: LocatorConfig, path: Path) -> SourceFile:
    rp = rel_path(config.root, path)
    source_repo = infer_source_repo(rp)
    repo_area = infer_repo_area(rp)
    if source_repo in {"server", "client"} and repo_area in {"framework", "agent-workflow"}:
        repo_area = source_repo
    return SourceFile(
        path=rp,
        source_repo=source_repo,
        repo_area=repo_area,
        extension=path.suffix.lower() or "[noext]",
        source_kind=infer_source_kind(path),
        size_bytes=path.stat().st_size,
        content_hash=sha256_file(path),
        last_modified=modified_time(path),
    )


CS_TYPE_RE = re.compile(r"\b(class|record|interface|enum|struct)\s+([A-Z][A-Za-z0-9_]*)")
CS_METHOD_RE = re.compile(
    r"\b(?:public|private|protected|internal)\s+(?:static\s+)?(?:async\s+)?(?:[A-Za-z0-9_<>,\[\]?]+\s+)+([A-Z_a-z][A-Za-z0-9_]*)\s*\("
)
TS_SYMBOL_RE = re.compile(r"\b(?:export\s+)?(?:class|interface|type|function|const|let|var)\s+([A-Z_a-z][A-Za-z0-9_]*)")
PY_SYMBOL_RE = re.compile(r"^\s*(?:class|def)\s+([A-Z_a-z][A-Za-z0-9_]*)\s*[\(:]")
UPPER_KEY_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{2,})(?![A-Za-z0-9_])")
SNAKE_KEY_RE = re.compile(r"(?<![A-Za-z0-9_])([a-z][a-z0-9]+(?:_[a-z0-9]+)+)(?![A-Za-z0-9_])")
KEY_VALUE_RE = re.compile(r"^\s*[\"']?([A-Za-z_][A-Za-z0-9_.:-]*)[\"']?\s*[:=]")
ENV_ACCESS_RE = re.compile(r"(?:process\.env\.|\$env:|env\.|configuration\[\"|GetEnvironmentVariable\(\"?)([A-Z][A-Z0-9_]{2,})")


def extract_code_symbols(source: SourceFile, lines: list[str]) -> list[CodeSymbol]:
    symbols: list[CodeSymbol] = []
    suffix = source.extension
    current_container = ""
    for line_no, line in enumerate(lines, start=1):
        matches: list[tuple[str, str]] = []
        if suffix == ".cs":
            for match in CS_TYPE_RE.finditer(line):
                matches.append((match.group(2), match.group(1)))
            for match in CS_METHOD_RE.finditer(line):
                name = match.group(1)
                if name not in {"if", "for", "foreach", "while", "switch", "catch", "using"}:
                    matches.append((name, "method"))
        elif suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
            for match in TS_SYMBOL_RE.finditer(line):
                matches.append((match.group(1), "symbol"))
        elif suffix in {".py"}:
            for match in PY_SYMBOL_RE.finditer(line):
                matches.append((match.group(1), "symbol"))
        for symbol, symbol_type in matches:
            if symbol_type in {"class", "record", "interface", "enum", "struct", "type"}:
                current_container = symbol
            symbols.append(
                CodeSymbol(
                    symbol=symbol,
                    symbol_type=symbol_type,
                    path=source.path,
                    line_number=line_no,
                    repo_area=source.repo_area,
                    source_repo=source.source_repo,
                    source_kind=source.source_kind,
                    container=current_container,
                    signature=line.strip()[:300],
                    context=line.strip()[:500],
                )
            )
    return symbols


def extract_config_keys(source: SourceFile, lines: list[str]) -> list[ConfigKey]:
    keys: list[ConfigKey] = []
    seen: set[tuple[str, int]] = set()
    for line_no, line in enumerate(lines, start=1):
        candidates: list[tuple[str, str]] = []
        for match in ENV_ACCESS_RE.finditer(line):
            candidates.append((match.group(1), "env_access"))
        if source.source_kind == "config":
            for regex, key_type in [(UPPER_KEY_RE, "constant_or_env"), (SNAKE_KEY_RE, "snake_key")]:
                for match in regex.finditer(line):
                    candidates.append((match.group(1), key_type))
        key_match = KEY_VALUE_RE.match(line)
        if key_match and ("_" in key_match.group(1) or source.extension in {".json", ".yaml", ".yml", ".toml", ".props", ".csproj"}):
            candidates.append((key_match.group(1), "config_key"))
        for key, key_type in candidates:
            if len(key) < 3 or (key, line_no) in seen:
                continue
            seen.add((key, line_no))
            keys.append(
                ConfigKey(
                    key=key,
                    key_type=key_type,
                    path=source.path,
                    line_number=line_no,
                    repo_area=source.repo_area,
                    source_repo=source.source_repo,
                    source_kind=source.source_kind,
                    context=redact_line(line).strip()[:500],
                )
            )
    return keys


def split_camel(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [part.lower() for part in re.split(r"[^A-Za-z0-9]+", spaced) if part]


def redact_line(line: str) -> str:
    stripped = line.strip()
    key_match = KEY_VALUE_RE.match(stripped)
    if key_match and SENSITIVE_NAME_RE.search(key_match.group(1)):
        key = key_match.group(1)
        delimiter = "=" if "=" in stripped.split(key, 1)[-1][:5] else ":"
        return f"{key}{delimiter}<redacted>"
    return re.sub(
        r"((?:password|passwd|secret|token|api[_-]?key|credential)[A-Za-z0-9_.-]*\s*[:=]\s*)([\"']?)[^\"'\\s,;]+\\2",
        r"\1<redacted>",
        line,
        flags=re.IGNORECASE,
    )
