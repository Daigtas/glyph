#!/usr/bin/env python3
"""Glyph — fast incremental codebase knowledge graph indexer.

tree-sitter AST extraction + SQLite storage + hash-based dirty tracking.
One DB, many projects. Zero LLM cost. Sub-100ms incremental updates.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

from tree_sitter import Language, Parser, Node

VERSION = "2.0.1"
SCHEMA_VERSION = 2

# ═══════════════════════════════════════════════════════════════════
# LANGUAGE LOADING
# ═══════════════════════════════════════════════════════════════════

try:
    import tree_sitter_typescript as tst
    TS_LANG = (Language(tst.language_typescript()), Language(tst.language_tsx()))
except Exception:
    TS_LANG = (None, None)

try:
    import tree_sitter_python as tsp
    PY_LANG = Language(tsp.language())
except Exception:
    PY_LANG = None

try:
    import tree_sitter_go as tsg
    GO_LANG = Language(tsg.language())
except Exception:
    GO_LANG = None

try:
    import tree_sitter_bash as tsb
    BASH_LANG = Language(tsb.language())
except Exception:
    BASH_LANG = None

# ext -> language tag. The tag (not a Language object) is what crosses the
# process boundary to parse workers; objects from the native extension are
# not picklable, so each worker builds its own parsers lazily.
EXT_LANG = {}
if TS_LANG[0]:
    EXT_LANG.update({
        ".ts": "typescript", ".tsx": "tsx",
        ".js": "javascript", ".jsx": "jsx",
        ".mjs": "javascript", ".cjs": "javascript",
    })
if PY_LANG:
    EXT_LANG[".py"] = "python"
    EXT_LANG[".pyi"] = "python"
if GO_LANG:
    EXT_LANG[".go"] = "go"
if BASH_LANG:
    EXT_LANG[".sh"] = "bash"
    EXT_LANG[".bash"] = "bash"

_LANG_FOR_TAG = {
    "typescript": lambda: TS_LANG[0], "javascript": lambda: TS_LANG[0],
    "tsx": lambda: TS_LANG[1], "jsx": lambda: TS_LANG[1],
    "python": lambda: PY_LANG, "go": lambda: GO_LANG, "bash": lambda: BASH_LANG,
}
_PARSERS: dict = {}


def get_parser(tag: str) -> Parser:
    """Parsers are expensive to build and safe to reuse — cache one per tag."""
    p = _PARSERS.get(tag)
    if p is None:
        p = Parser(_LANG_FOR_TAG[tag]())
        _PARSERS[tag] = p
    return p


IGNORE_DIRS = {
    "node_modules", ".next", "dist", "build", ".git", "__pycache__",
    ".venv", "venv", ".turbo", "coverage", ".cache", "generated",
    ".generated", "graphify-out", ".mypy_cache", ".pytest_cache",
    "vendor", "target", ".svelte-kit", "out", ".nuxt", "site-packages",
}
# Matched with endswith() so multi-dot suffixes like ".d.ts" actually work —
# Path(".d.ts").suffix is ".ts", which is why the old set never matched.
IGNORE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".lock", ".map",
    ".d.ts", ".min.js", ".min.css", ".bundle.js", ".chunk.js",
    ".sqlite", ".sqlite3", ".db", ".bin", ".wasm", ".pyc",
)
MAX_FILE_BYTES = 2 * 1024 * 1024  # skip generated megafiles


def should_ignore_dir(name: str) -> bool:
    return name in IGNORE_DIRS or (name.startswith(".") and name != ".")


def should_ignore_file(name: str) -> bool:
    return name.endswith(IGNORE_SUFFIXES)


# ═══════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════

GLYPH_HOME = os.path.expanduser("~/.glyph")
DB_PATH = os.environ.get("GLYPH_DB", os.path.join(GLYPH_HOME, "glyph.db"))


def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA cache_size=-64000")   # 64 MB page cache
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA mmap_size=268435456")
    return db


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    last_scan INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    hash TEXT,
    size INTEGER DEFAULT 0,
    mtime INTEGER DEFAULT 0,
    lang TEXT,
    line_count INTEGER DEFAULT 0,
    last_parsed INTEGER DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    UNIQUE(project_id, path)
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    end_line INTEGER DEFAULT 0,
    exported INTEGER DEFAULT 0,
    parent_id INTEGER,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);
-- target_name is kept alongside target_id so an edge survives its target
-- being re-parsed: incremental scans re-resolve by name instead of losing
-- every inbound edge (the v1 bug that decayed the graph 35% per 10 edits).
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    src_file_id INTEGER NOT NULL,
    source_id INTEGER,
    target_id INTEGER,
    target_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (src_file_id) REFERENCES files(id) ON DELETE CASCADE,
    UNIQUE(src_file_id, source_id, target_name, kind)
);
CREATE TABLE IF NOT EXISTS file_metrics (
    file_id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    line_count INTEGER DEFAULT 0,
    symbol_count INTEGER DEFAULT 0,
    max_nesting_depth INTEGER DEFAULT 0,
    fallow_last_health INTEGER DEFAULT 0,
    fallow_last_dead_code INTEGER DEFAULT 0,
    fallow_last_dupes INTEGER DEFAULT 0,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS fallow_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    file_id INTEGER,
    symbol_name TEXT,
    issue_kind TEXT NOT NULL,
    sub_kind TEXT,
    severity TEXT,
    line INTEGER DEFAULT 0,
    col INTEGER DEFAULT 0,
    cyclomatic INTEGER,
    cognitive INTEGER,
    line_count INTEGER,
    param_count INTEGER,
    crap_score REAL,
    message TEXT,
    actions_json TEXT,
    ingested_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    scan_version TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS file_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    old_hash TEXT,
    new_hash TEXT NOT NULL,
    commit_hash TEXT NOT NULL,
    commit_msg TEXT,
    author TEXT,
    committed_at INTEGER NOT NULL,
    change_type TEXT,
    lines_added INTEGER,
    lines_removed INTEGER,
    summary TEXT,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS descriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    detail TEXT,
    generated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    model TEXT,
    source_session_id TEXT,
    confidence REAL DEFAULT 1.0,
    version_hash TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS session_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    file_id INTEGER,
    symbol_id INTEGER,
    ref_type TEXT NOT NULL,
    summary TEXT,
    message_count INTEGER,
    first_at REAL,
    last_at REAL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_symbols_lookup   ON symbols(project_id, name);
CREATE INDEX IF NOT EXISTS idx_symbols_file     ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_kind     ON symbols(project_id, kind);
CREATE INDEX IF NOT EXISTS idx_symbols_exported ON symbols(project_id, exported);
CREATE INDEX IF NOT EXISTS idx_edges_src        ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_tgt        ON edges(project_id, target_id);
CREATE INDEX IF NOT EXISTS idx_edges_name       ON edges(project_id, target_name);
CREATE INDEX IF NOT EXISTS idx_edges_srcfile    ON edges(src_file_id);
CREATE INDEX IF NOT EXISTS idx_files_project    ON files(project_id);
CREATE INDEX IF NOT EXISTS idx_fallow_project   ON fallow_issues(project_id, issue_kind);
CREATE INDEX IF NOT EXISTS idx_fallow_file      ON fallow_issues(file_id);
CREATE INDEX IF NOT EXISTS idx_history_file     ON file_history(file_id, committed_at);
CREATE INDEX IF NOT EXISTS idx_descriptions_tgt ON descriptions(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_refs_file        ON session_refs(file_id);
CREATE INDEX IF NOT EXISTS idx_refs_symbol      ON session_refs(symbol_id);
"""


def _detected_schema_version(db) -> int:
    """0 = empty DB, 1 = pre-2.0 layout, 2 = current."""
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "projects" not in tables:
        return 0
    if "meta" in tables:
        row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row:
            return int(row[0])
    return 1


def init_schema(allow_migrate: bool = True) -> None:
    db = get_db()
    ver = _detected_schema_version(db)
    if ver == 1 and allow_migrate:
        db.close()
        _migrate_v1_to_v2()
        return
    db.executescript(SCHEMA)
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
               (str(SCHEMA_VERSION),))
    db.commit()
    db.close()


def _migrate_v1_to_v2() -> None:
    """v1 stored no edge sources and no file mtimes, and ~46% of its symbol
    names were corrupt. Nothing there is worth salvaging: keep the project
    list, drop the derived data, and let the next scan rebuild it."""
    backup = f"{DB_PATH}.v1-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        import shutil
        shutil.copy2(DB_PATH, backup)
        print(f"[glyph] migrating schema v1 → v2 (backup: {backup})")
    except Exception as e:
        print(f"[glyph] warning: could not back up DB ({e})")

    db = get_db()
    db.execute("PRAGMA foreign_keys=OFF")
    projects = db.execute("SELECT name, path FROM projects").fetchall()
    for t in ("edges", "symbols", "file_metrics", "fallow_issues",
              "file_history", "descriptions", "session_refs", "files", "projects"):
        db.execute(f"DROP TABLE IF EXISTS {t}")
    db.executescript(SCHEMA)
    db.executemany("INSERT OR IGNORE INTO projects (name, path) VALUES (?, ?)", projects)
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
               (str(SCHEMA_VERSION),))
    db.commit()
    db.close()
    print(f"[glyph] migrated. {len(projects)} project(s) kept — "
          f"re-run 'glyph scan <name> <path>' to rebuild the graph.")


# ═══════════════════════════════════════════════════════════════════
# EXTRACTION
# ═══════════════════════════════════════════════════════════════════
#
# A parse produces three parallel lists, all using *file-local* indices.
# The parent process converts those to real row ids after allocating an id
# range, which keeps workers free of any DB handle.
#
#   symbols : (name, kind, line, end_line, exported, parent_local)
#   edges   : (src_local, target_name, kind, line)   src_local -1 = module scope
#   imports : (local_name, module_path)
#
# Scope tracking is what gives edges a source. v1 emitted a hardcoded None
# here, which is why deps/path/bridges returned nothing on any project.

MODULE_SCOPE = -1

# Members so ubiquitous that resolving them by name is always noise.
NOISE_CALLS = frozenset("""
map filter reduce forEach push pop shift unshift slice splice concat join split
then catch finally log warn error info debug trace assign keys values entries
toString valueOf test match replace trim get set has add delete clear length
parse stringify from of all race resolve reject bind call apply
""".split())


def _txt(src: bytes, node: Node) -> str:
    """Slice the SOURCE BYTES — tree-sitter offsets are byte offsets.

    v1 sliced the decoded str with these, so a single non-ASCII byte earlier
    in the file shifted every later name; 46% of symbols came out corrupt.
    """
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _field(node: Node, name: str):
    return node.child_by_field_name(name)


def _child_of_type(node: Node, *types: str):
    for c in node.children:
        if c.type in types:
            return c
    return None


def _binding_names(src: bytes, node: Node):
    """Yield every identifier a binding site introduces.

    `const { a, b: c } = x` and `const [r, g] = y` are single declarators whose
    name node is a pattern. Storing the raw pattern text ("{ a, b: c }") makes
    the symbol unsearchable, so destructure it into the names it actually binds.
    """
    if node is None:
        return
    t = node.type
    if t in ("identifier", "shorthand_property_identifier_pattern",
             "property_identifier", "shorthand_property_identifier"):
        yield _txt(src, node)
    elif t in ("object_pattern", "array_pattern"):
        for c in node.children:
            yield from _binding_names(src, c)
    elif t == "pair_pattern":
        yield from _binding_names(src, _field(node, "value"))
    elif t in ("rest_pattern", "assignment_pattern", "object_assignment_pattern"):
        target = _field(node, "left") or (node.children[0] if node.children else None)
        yield from _binding_names(src, target)


def _ts_exported(node: Node) -> bool:
    p = node.parent
    return bool(p and p.type == "export_statement")


# Only a literal function body opens a lexical scope for edge attribution.
# A `const x = someCall()` must NOT: otherwise every call in the enclosing
# function gets attributed to the last local variable instead of the function.
FUNCTION_VALUE_TYPES = ("arrow_function", "function_expression",
                        "function", "generator_function")


def _is_function_value(value: Node) -> bool:
    return value is not None and value.type in FUNCTION_VALUE_TYPES


def extract_ts(src: bytes, tag: str) -> dict:
    """TypeScript / JavaScript / TSX / JSX."""
    tree = get_parser(tag).parse(src)
    symbols: list = []
    edges: list = []
    imports: list = []
    export_names: set = set()

    # (node, enclosing_symbol_local_index)
    stack = [(tree.root_node, MODULE_SCOPE)]
    while stack:
        node, scope = stack.pop()
        t = node.type
        new_scope = scope

        if t in ("function_declaration", "generator_function_declaration",
                 "function_signature"):
            n = _field(node, "name")
            if n:
                symbols.append((_txt(src, n), "function", node.start_point[0] + 1,
                                node.end_point[0] + 1, int(_ts_exported(node)), scope))
                new_scope = len(symbols) - 1

        elif t == "class_declaration":
            n = _field(node, "name")
            if n:
                symbols.append((_txt(src, n), "class", node.start_point[0] + 1,
                                node.end_point[0] + 1, int(_ts_exported(node)), scope))
                new_scope = len(symbols) - 1

        elif t in ("method_definition", "method_signature"):
            n = _field(node, "name")
            if n and n.type in ("property_identifier", "identifier",
                                "private_property_identifier"):
                symbols.append((_txt(src, n), "method", node.start_point[0] + 1,
                                node.end_point[0] + 1, 0, scope))
                new_scope = len(symbols) - 1

        elif t in ("variable_declaration", "lexical_declaration"):
            exported = int(_ts_exported(node))
            first = node.children[0].type if node.children else "const"
            kind0 = first if first in ("const", "let", "var") else "const"
            for child in node.children:
                if child.type != "variable_declarator":
                    continue
                n = _field(child, "name")
                if not n:
                    continue
                value = _field(child, "value")
                names = list(_binding_names(src, n))
                if not names:
                    continue
                destructured = n.type in ("object_pattern", "array_pattern")
                for bname in names:
                    kind = kind0
                    # React component: Capitalised const bound to a function
                    # that returns JSX, or a React.* wrapper (memo/forwardRef).
                    if not destructured and kind == "const" and bname[:1].isupper() \
                            and value is not None:
                        head = src[value.start_byte:value.start_byte + 120]
                        if value.type in ("arrow_function", "function_expression") or \
                           head[:6] in (b"React.", b"forwar") or head[:4] == b"memo":
                            kind = "component"
                    elif not destructured and kind == "const" and _is_function_value(value):
                        kind = "function"
                    symbols.append((bname, kind, child.start_point[0] + 1,
                                    child.end_point[0] + 1, exported, scope))
                    if not destructured and (_is_function_value(value)
                                             or kind == "component"):
                        new_scope = len(symbols) - 1

        elif t == "type_alias_declaration":
            n = _field(node, "name")
            if n:
                symbols.append((_txt(src, n), "type", node.start_point[0] + 1,
                                node.end_point[0] + 1, int(_ts_exported(node)), scope))

        elif t == "interface_declaration":
            n = _field(node, "name")
            if n:
                symbols.append((_txt(src, n), "interface", node.start_point[0] + 1,
                                node.end_point[0] + 1, int(_ts_exported(node)), scope))

        elif t == "enum_declaration":
            n = _field(node, "name")
            if n:
                symbols.append((_txt(src, n), "enum", node.start_point[0] + 1,
                                node.end_point[0] + 1, int(_ts_exported(node)), scope))

        elif t == "import_statement":
            src_node = _field(node, "source")
            module = _txt(src, src_node).strip("'\"` ") if src_node else ""
            # NOTE: there is no "clause" field in this grammar — v1 used
            # child_by_field_name("clause") and so extracted almost no imports
            # (24 across 4,710 files). The node type is import_clause.
            clause = _child_of_type(node, "import_clause")
            if clause:
                for name in _ts_import_names(src, clause):
                    imports.append((name, module))
                    edges.append((MODULE_SCOPE, name, "import", node.start_point[0] + 1))

        elif t == "export_statement":
            clause = _child_of_type(node, "export_clause")
            if clause:
                for spec in clause.children:
                    if spec.type == "export_specifier":
                        n = _field(spec, "name")
                        if n:
                            export_names.add(_txt(src, n))

        elif t == "call_expression":
            fn = _field(node, "function")
            if fn is not None:
                name = None
                if fn.type == "identifier":
                    name = _txt(src, fn)
                elif fn.type == "member_expression":
                    prop = _field(fn, "property")
                    if prop is not None:
                        name = _txt(src, prop)
                if name and name not in NOISE_CALLS:
                    edges.append((scope, name, "call", node.start_point[0] + 1))

        elif t in ("jsx_self_closing_element", "jsx_opening_element"):
            n = _field(node, "name")
            if n is not None:
                tagname = _txt(src, n)
                if tagname[:1].isupper():
                    edges.append((scope, tagname, "jsx_use", node.start_point[0] + 1))

        elif t == "new_expression":
            ctor = _field(node, "constructor")
            if ctor is not None and ctor.type == "identifier":
                edges.append((scope, _txt(src, ctor), "call", node.start_point[0] + 1))

        for c in reversed(node.children):
            stack.append((c, new_scope))

    if export_names:
        symbols = [
            (n, k, l, el, 1 if n in export_names else e, p)
            for (n, k, l, el, e, p) in symbols
        ]
    return {"symbols": symbols, "edges": edges, "imports": imports}


def _ts_import_names(src: bytes, clause: Node):
    """Yield every local name bound by an import clause."""
    for child in clause.children:
        if child.type == "identifier":                  # default import
            yield _txt(src, child)
        elif child.type == "named_imports":
            for spec in child.children:
                if spec.type != "import_specifier":
                    continue
                alias = _field(spec, "alias")
                name = _field(spec, "name")
                node = alias or name
                if node is not None:
                    yield _txt(src, node)
        elif child.type == "namespace_import":
            ident = _child_of_type(child, "identifier")
            if ident is not None:
                yield _txt(src, ident)


def extract_py(src: bytes, tag: str = "python") -> dict:
    tree = get_parser("python").parse(src)
    symbols: list = []
    edges: list = []
    imports: list = []

    stack = [(tree.root_node, MODULE_SCOPE, 0)]
    while stack:
        node, scope, depth = stack.pop()
        t = node.type
        new_scope, new_depth = scope, depth

        if t in ("function_definition", "class_definition"):
            n = _field(node, "name")
            if n:
                name = _txt(src, n)
                kind = "class" if t == "class_definition" else (
                    "method" if depth > 0 and scope != MODULE_SCOPE else "function")
                # Python has no export keyword: module-level public names are
                # the closest equivalent, which is what `orphans` needs.
                exported = int(depth == 0 and not name.startswith("_"))
                symbols.append((name, kind, node.start_point[0] + 1,
                                node.end_point[0] + 1, exported, scope))
                new_scope = len(symbols) - 1
                new_depth = depth + 1

        elif t in ("import_statement", "import_from_statement"):
            for name, module in _py_import_names(src, node):
                imports.append((name, module))
                edges.append((MODULE_SCOPE, name, "import", node.start_point[0] + 1))

        elif t == "call":
            fn = _field(node, "function")
            if fn is not None:
                name = None
                if fn.type == "identifier":
                    name = _txt(src, fn)
                elif fn.type == "attribute":
                    attr = _field(fn, "attribute")
                    if attr is not None:
                        name = _txt(src, attr)
                if name and name not in NOISE_CALLS:
                    edges.append((scope, name, "call", node.start_point[0] + 1))

        elif t == "assignment" and scope == MODULE_SCOPE:
            left = _field(node, "left")
            if left is not None and left.type == "identifier":
                name = _txt(src, left)
                if name.isupper():
                    symbols.append((name, "const", node.start_point[0] + 1,
                                    node.end_point[0] + 1,
                                    int(not name.startswith("_")), scope))

        for c in reversed(node.children):
            stack.append((c, new_scope, new_depth))

    return {"symbols": symbols, "edges": edges, "imports": imports}


def _py_import_names(src: bytes, node: Node):
    """Yield (local_name, module) for import / from-import statements."""
    if node.type == "import_from_statement":
        mod_node = _field(node, "module_name")
        module = _txt(src, mod_node) if mod_node else ""
        for child in node.children:
            if child is mod_node:
                continue
            if child.type == "dotted_name":
                yield _txt(src, child).split(".")[-1], module
            elif child.type == "aliased_import":
                alias = _field(child, "alias")
                nm = _field(child, "name")
                node2 = alias or nm
                if node2 is not None:
                    yield _txt(src, node2), module
    else:
        for child in node.children:
            if child.type == "dotted_name":
                yield _txt(src, child).split(".")[0], _txt(src, child)
            elif child.type == "aliased_import":
                alias = _field(child, "alias")
                nm = _field(child, "name")
                module = _txt(src, nm) if nm else ""
                if alias is not None:
                    yield _txt(src, alias), module


def extract_go(src: bytes, tag: str = "go") -> dict:
    tree = get_parser("go").parse(src)
    symbols: list = []
    edges: list = []
    imports: list = []

    stack = [(tree.root_node, MODULE_SCOPE)]
    while stack:
        node, scope = stack.pop()
        t = node.type
        new_scope = scope

        if t in ("function_declaration", "method_declaration"):
            n = _field(node, "name")
            if n:
                name = _txt(src, n)
                kind = "function" if t == "function_declaration" else "method"
                # Go exports by capitalisation.
                symbols.append((name, kind, node.start_point[0] + 1,
                                node.end_point[0] + 1, int(name[:1].isupper()), scope))
                new_scope = len(symbols) - 1

        elif t == "type_declaration":
            for child in node.children:
                if child.type == "type_spec":
                    n = _field(child, "name")
                    if n:
                        name = _txt(src, n)
                        symbols.append((name, "type", child.start_point[0] + 1,
                                        child.end_point[0] + 1,
                                        int(name[:1].isupper()), scope))

        elif t == "import_declaration":
            for spec in _iter_descendants(node, "import_spec"):
                p = _field(spec, "path")
                if p is not None:
                    full = _txt(src, p).strip('"')
                    mod = full.rsplit("/", 1)[-1]
                    imports.append((mod, full))
                    edges.append((MODULE_SCOPE, mod, "import", spec.start_point[0] + 1))

        elif t == "call_expression":
            fn = _field(node, "function")
            if fn is not None:
                name = None
                if fn.type == "identifier":
                    name = _txt(src, fn)
                elif fn.type == "selector_expression":
                    sel = _field(fn, "field")
                    if sel is not None:
                        name = _txt(src, sel)
                if name and name not in NOISE_CALLS:
                    edges.append((scope, name, "call", node.start_point[0] + 1))

        for c in reversed(node.children):
            stack.append((c, new_scope))

    return {"symbols": symbols, "edges": edges, "imports": imports}


def extract_bash(src: bytes, tag: str = "bash") -> dict:
    tree = get_parser("bash").parse(src)
    symbols: list = []
    edges: list = []

    stack = [(tree.root_node, MODULE_SCOPE)]
    while stack:
        node, scope = stack.pop()
        new_scope = scope
        if node.type == "function_definition":
            n = _field(node, "name")
            if n:
                symbols.append((_txt(src, n), "function", node.start_point[0] + 1,
                                node.end_point[0] + 1, 1, scope))
                new_scope = len(symbols) - 1
        elif node.type == "command":
            n = _field(node, "name")
            if n is not None:
                name = _txt(src, n)
                if name and name.replace("_", "").replace("-", "").isalnum():
                    edges.append((scope, name, "call", node.start_point[0] + 1))
        for c in reversed(node.children):
            stack.append((c, new_scope))

    return {"symbols": symbols, "edges": edges, "imports": []}


def _iter_descendants(node: Node, type_name: str):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == type_name:
            yield n
        stack.extend(n.children)


EXTRACTORS = {
    "typescript": extract_ts, "javascript": extract_ts,
    "tsx": extract_ts, "jsx": extract_ts,
    "python": extract_py, "go": extract_go, "bash": extract_bash,
}


# ═══════════════════════════════════════════════════════════════════
# INDEXER
# ═══════════════════════════════════════════════════════════════════

def hash_bytes(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def walk_source_files(root: str, unreadable_dirs: list = None):
    """Yield (rel, abspath, tag, size, mtime). os.scandir avoids a second
    stat() per entry, which matters on trees with tens of thousands of files.

    Directories we cannot enter are appended to `unreadable_dirs` rather than
    dropped silently — an index that is quietly missing files is worse than
    one that says so.
    """
    root = os.path.abspath(root)
    pending = [root]
    while pending:
        d = pending.pop()
        try:
            entries = list(os.scandir(d))
        except OSError as e:
            if unreadable_dirs is not None:
                unreadable_dirs.append((os.path.relpath(d, root), e.strerror or str(e)))
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if not should_ignore_dir(e.name):
                        pending.append(e.path)
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                name = e.name
                if should_ignore_file(name):
                    continue
                ext = os.path.splitext(name)[1].lower()
                tag = EXT_LANG.get(ext)
                if not tag:
                    continue
                st = e.stat()
                if st.st_size > MAX_FILE_BYTES or st.st_size == 0:
                    continue
                yield (os.path.relpath(e.path, root), e.path, tag,
                       st.st_size, int(st.st_mtime))
            except OSError:
                continue


def _parse_one(job):
    """Worker: hash, and parse only if the hash actually changed."""
    rel, abspath, tag, old_hash = job
    try:
        with open(abspath, "rb") as f:
            data = f.read()
    except OSError as e:
        return (rel, tag, None, 0, None, e.strerror or str(e))
    h = hash_bytes(data)
    line_count = data.count(b"\n") + 1
    if h == old_hash:
        return (rel, tag, h, line_count, None, None)   # identical, skip parse
    try:
        out = EXTRACTORS[tag](data, tag)
    except Exception as e:
        return (rel, tag, h, line_count, {"symbols": [], "edges": [], "imports": []},
                f"parse failed: {type(e).__name__}")
    return (rel, tag, h, line_count, out, None)


def _resolve_module(module: str, from_rel: str, path_index: dict):
    """Map an import specifier to a file id in this project, or None."""
    if not module:
        return None
    if module.startswith("."):
        base = os.path.normpath(os.path.join(os.path.dirname(from_rel), module))
    elif module[0] in "@~#" and "/" in module:
        base = module.split("/", 1)[1]          # '@/lib/x' -> 'lib/x'
    else:
        return None                             # bare package: external
    base = base.replace(os.sep, "/").lstrip("./")
    for cand in (base, f"{base}/index", f"src/{base}", f"src/{base}/index"):
        fid = path_index.get(cand)
        if fid is not None:
            return fid
    return None


def _resolve_targets(cur, project_id: int, rows, path_index, import_map):
    """Point every edge at a concrete symbol.

    Order of preference — narrowest scope wins, and an ambiguous name is left
    unresolved rather than pointed at an arbitrary definition:
      1. a symbol of that name in the calling file
      2. the file the name was imported from
      3. the only symbol of that name in the project
      4. the only *exported* symbol of that name
    """
    by_name = defaultdict(list)
    for sid, name, fid, exported in cur.execute(
            "SELECT id, name, file_id, exported FROM symbols WHERE project_id=?",
            (project_id,)):
        by_name[name].append((sid, fid, exported))

    updates = []
    for edge_id, src_file_id, target_name in rows:
        cands = by_name.get(target_name)
        if not cands:
            continue
        chosen = None
        for sid, fid, _ in cands:                           # 1. same file
            if fid == src_file_id:
                chosen = sid
                break
        if chosen is None:
            want = import_map.get((src_file_id, target_name))
            if want is not None:                            # 2. imported from
                for sid, fid, _ in cands:
                    if fid == want:
                        chosen = sid
                        break
        if chosen is None:
            # Visibility rule: a symbol declared in another file is only
            # reachable if it is exported. Without this, a member call like
            # `parsed.error.flatten()` links to an unrelated module-local
            # `flatten()` and invents hundreds of false dependencies.
            visible = [c for c in cands if c[2]]
            if len(visible) == 1:                           # 3. unique export
                chosen = visible[0][0]
        if chosen is not None:
            updates.append((chosen, edge_id))
    if updates:
        cur.executemany("UPDATE edges SET target_id=? WHERE id=?", updates)
    return len(updates)


def scan_project(name: str, root: str, full: bool = False, jobs: int = 0,
                 quiet: bool = False):
    """Index a project directory. Returns a stats dict."""
    init_schema()
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        print(f"[glyph] error: {root} is not a directory")
        return None

    t0 = time.time()
    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT INTO projects (name, path) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET path=excluded.path", (name, root))
    project_id = cur.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()[0]

    unreadable_dirs: list = []
    found = list(walk_source_files(root, unreadable_dirs))
    found_rels = {f[0] for f in found}

    existing = {}
    for path, h, size, mtime, fid in cur.execute(
            "SELECT path, hash, size, mtime, id FROM files WHERE project_id=?",
            (project_id,)):
        existing[path] = (h, size, mtime, fid)

    # 1. Drop files that vanished. ON DELETE CASCADE clears symbols/metrics;
    #    edges out of them go too, and edges *into* them are repaired below.
    stale = [p for p in existing if p not in found_rels]
    if stale:
        cur.executemany("DELETE FROM files WHERE project_id=? AND path=?",
                        [(project_id, p) for p in stale])

    if full:
        cur.execute("DELETE FROM edges WHERE project_id=?", (project_id,))
        cur.execute("DELETE FROM symbols WHERE project_id=?", (project_id,))

    # 2. mtime+size fast path: unchanged files never get read or hashed at all.
    jobs_list, unchanged = [], 0
    for rel, abspath, tag, size, mtime in found:
        prev = existing.get(rel)
        if prev and not full and prev[1] == size and prev[2] == mtime:
            unchanged += 1
            continue
        jobs_list.append((rel, abspath, tag, None if full else (prev[0] if prev else None)))

    if not jobs_list:
        # No repair pass here: symbol ids only change when a scan re-parses a
        # file, so nothing can have become dangling since the last scan.
        cur.execute("UPDATE projects SET last_scan=? WHERE id=?",
                    (int(time.time()), project_id))
        db.commit()
        db.close()
        if not quiet:
            print(f"[glyph] {name}: {unchanged} files unchanged, nothing to parse "
                  f"({time.time()-t0:.2f}s)")
        return {"project": name, "total": len(found), "parsed": 0,
                "skipped": unchanged, "symbols": 0, "edges": 0, "linked": 0,
                "seconds": round(time.time() - t0, 3)}

    # 3. Parse. tree-sitter parsing is CPU-bound C code, so processes scale;
    #    for small batches the pool costs more than it saves.
    if jobs == 0:
        jobs = min(os.cpu_count() or 4, 8)
    raw = []
    if jobs > 1 and len(jobs_list) > 64:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            raw = [r for r in pool.map(_parse_one, jobs_list, chunksize=32) if r]
    else:
        raw = [r for r in (_parse_one(j) for j in jobs_list) if r]

    # Split off files that could not be read or parsed so they are reported
    # rather than quietly missing from the index.
    results, failures = [], []
    for rel, tag, h, lc, out, err in raw:
        if h is None:
            failures.append((rel, err))
        else:
            results.append((rel, tag, h, lc, out))
            if err:
                failures.append((rel, err))

    now = int(time.time())
    stat_by_rel = {f[0]: (f[3], f[4]) for f in found}

    # 4. Upsert file rows, then read back ids in one query.
    file_rows = []
    for rel, tag, h, line_count, _out in results:
        size, mtime = stat_by_rel.get(rel, (0, 0))
        file_rows.append((project_id, rel, h, size, mtime, tag, line_count, now))
    cur.executemany(
        """INSERT INTO files (project_id, path, hash, size, mtime, lang, line_count, last_parsed)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(project_id, path) DO UPDATE SET
             hash=excluded.hash, size=excluded.size, mtime=excluded.mtime,
             lang=excluded.lang, line_count=excluded.line_count,
             last_parsed=excluded.last_parsed""", file_rows)

    path_to_id = {p: i for p, i in cur.execute(
        "SELECT path, id FROM files WHERE project_id=?", (project_id,))}

    reparsed = [(rel, out) for rel, tag, h, lc, out in results if out is not None]
    changed_ids = [path_to_id[rel] for rel, _ in reparsed if rel in path_to_id]

    # 5. Replace only what this file *emits*. Inbound edges are deliberately
    #    left alone — deleting them was the v1 decay bug — and re-pointed by
    #    the repair pass once the new symbol ids exist.
    if changed_ids:
        qs = ",".join("?" * len(changed_ids))
        cur.execute(f"DELETE FROM edges WHERE src_file_id IN ({qs})", changed_ids)
        cur.execute(f"DELETE FROM symbols WHERE file_id IN ({qs})", changed_ids)

    # 6. Allocate ids up front so edges can reference symbols before insert.
    next_id = (cur.execute("SELECT COALESCE(MAX(id), 0) FROM symbols").fetchone()[0]) + 1
    sym_rows, edge_rows, metric_rows, import_rows = [], [], [], []

    for rel, out in reparsed:
        file_id = path_to_id.get(rel)
        if file_id is None:
            continue
        base = next_id
        syms = out["symbols"]
        for i, (sname, kind, line, end_line, exported, parent_local) in enumerate(syms):
            if not sname:
                continue
            parent_id = base + parent_local if parent_local >= 0 else None
            sym_rows.append((base + i, project_id, file_id, sname, kind,
                             line, end_line, exported, parent_id))
        next_id = base + len(syms)

        seen = set()
        for src_local, tname, kind, line in out["edges"]:
            if not tname:
                continue
            source_id = base + src_local if src_local >= 0 else None
            key = (source_id, tname, kind)
            if key in seen:          # one edge per (caller, callee) pair
                continue
            seen.add(key)
            edge_rows.append((project_id, file_id, source_id, tname, kind, line))

        for local_name, module in out["imports"]:
            import_rows.append((file_id, local_name, module, rel))

        metric_rows.append((file_id, project_id, len(syms)))

    if sym_rows:
        cur.executemany(
            "INSERT INTO symbols (id, project_id, file_id, name, kind, line, "
            "end_line, exported, parent_id) VALUES (?,?,?,?,?,?,?,?,?)", sym_rows)
    if edge_rows:
        cur.executemany(
            "INSERT OR IGNORE INTO edges (project_id, src_file_id, source_id, "
            "target_name, kind, line) VALUES (?,?,?,?,?,?)", edge_rows)
    if metric_rows:
        cur.executemany(
            """INSERT INTO file_metrics (file_id, project_id, symbol_count, line_count)
               VALUES (?,?,?,(SELECT line_count FROM files WHERE id=?))
               ON CONFLICT(file_id) DO UPDATE SET
                 symbol_count=excluded.symbol_count, line_count=excluded.line_count""",
            [(f, p, c, f) for f, p, c in metric_rows])

    _repair_edges(cur, project_id, root, import_rows)

    cur.execute("UPDATE projects SET last_scan=? WHERE id=?", (now, project_id))
    db.commit()

    sym_count = cur.execute("SELECT COUNT(*) FROM symbols WHERE project_id=?",
                            (project_id,)).fetchone()[0]
    edge_count = cur.execute("SELECT COUNT(*) FROM edges WHERE project_id=?",
                             (project_id,)).fetchone()[0]
    linked = cur.execute("SELECT COUNT(*) FROM edges WHERE project_id=? "
                         "AND target_id IS NOT NULL", (project_id,)).fetchone()[0]
    db.close()

    if not quiet:
        mode = "full reindex" if full else "incremental"
        print(f"[glyph] {name}: {len(reparsed)} parsed, {unchanged} skipped — "
              f"{sym_count} symbols, {edge_count} edges ({linked} linked) "
              f"— {mode} in {time.time()-t0:.2f}s")
    _warn_skipped(failures, unreadable_dirs, quiet)
    return {"project": name, "total": len(found), "parsed": len(reparsed),
            "skipped": unchanged, "symbols": sym_count, "edges": edge_count,
            "linked": linked, "seconds": round(time.time() - t0, 3),
            "unreadable": [f for f, _ in failures] + [d for d, _ in unreadable_dirs]}


def _warn_skipped(failures, unreadable_dirs, quiet: bool) -> None:
    """Say out loud what did not make it into the index."""
    if quiet or (not failures and not unreadable_dirs):
        return
    total = len(failures) + len(unreadable_dirs)
    print(f"  ⚠ {total} path(s) skipped — the index is incomplete:")
    for path, why in (failures + unreadable_dirs)[:8]:
        print(f"      {path} — {why}")
    if total > 8:
        print(f"      ... and {total - 8} more")


def _repair_edges(cur, project_id: int, root: str, import_rows=None) -> int:
    """Re-point every edge that has no live target symbol.

    This is what makes incremental scans lossless: when a file is re-parsed
    its symbols get fresh ids, so edges from *other* files dangle. v1 deleted
    those edges instead, losing ~35% of the graph every ten edits.
    """
    dangling = cur.execute("""
        SELECT e.id, e.src_file_id, e.target_name
        FROM edges e
        LEFT JOIN symbols s ON s.id = e.target_id
        WHERE e.project_id = ? AND (e.target_id IS NULL OR s.id IS NULL)
    """, (project_id,)).fetchall()
    if not dangling:
        return 0

    path_index = {}
    for path, fid in cur.execute(
            "SELECT path, id FROM files WHERE project_id=?", (project_id,)):
        norm = path.replace(os.sep, "/")
        path_index[os.path.splitext(norm)[0]] = fid

    import_map = {}
    if import_rows:
        for file_id, local_name, module, rel in import_rows:
            tgt = _resolve_module(module, rel, path_index)
            if tgt is not None:
                import_map[(file_id, local_name)] = tgt

    return _resolve_targets(cur, project_id, dangling, path_index, import_map)


# ═══════════════════════════════════════════════════════════════════
# QUERIES
# ═══════════════════════════════════════════════════════════════════

class ProjectNotFound(Exception):
    pass


def _project(cur, name: str):
    row = cur.execute("SELECT id, path FROM projects WHERE name=?", (name,)).fetchone()
    if not row:
        raise ProjectNotFound(name)
    return row


def _emit(data, as_json: bool, render):
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        render(data)


def find_symbol(project: str, name: str, limit: int = 20, as_json: bool = False):
    db = get_db()
    cur = db.cursor()
    pid, _ = _project(cur, project)

    rows = cur.execute("""
        SELECT s.name, s.kind, s.line, f.path, s.exported,
               (SELECT COUNT(*) FROM edges e WHERE e.target_id = s.id) AS refs
        FROM symbols s JOIN files f ON s.file_id = f.id
        WHERE s.project_id=? AND s.name=?
        ORDER BY s.exported DESC, refs DESC, s.line
        LIMIT ?""", (pid, name, limit)).fetchall()

    exact = bool(rows)
    if not rows:
        rows = cur.execute("""
            SELECT s.name, s.kind, s.line, f.path, s.exported,
                   (SELECT COUNT(*) FROM edges e WHERE e.target_id = s.id) AS refs
            FROM symbols s JOIN files f ON s.file_id = f.id
            WHERE s.project_id=? AND s.name LIKE ?
            ORDER BY s.exported DESC, refs DESC, LENGTH(s.name)
            LIMIT ?""", (pid, f"%{name}%", limit)).fetchall()
    db.close()

    data = {"query": name, "project": project, "exact": exact,
            "results": [{"name": n, "kind": k, "file": p, "line": l,
                         "exported": bool(e), "refs": r}
                        for n, k, l, p, e, r in rows]}

    def render(d):
        if not d["results"]:
            print(f"  '{name}' not found in {project}")
            return
        if not d["exact"]:
            print(f"  no exact match — substring matches for '{name}':")
        for r in d["results"]:
            exp = " [exported]" if r["exported"] else ""
            refs = f"  ({r['refs']} refs)" if r["refs"] else ""
            print(f"  {r['name']} ({r['kind']}) → {r['file']}:{r['line']}{exp}{refs}")
    _emit(data, as_json, render)


def deps(project: str, name: str, direction: str = "both", limit: int = 40,
         as_json: bool = False):
    db = get_db()
    cur = db.cursor()
    pid, _ = _project(cur, project)

    syms = cur.execute(
        """SELECT s.id, s.name, s.kind, f.path, s.line
           FROM symbols s JOIN files f ON s.file_id=f.id
           WHERE s.project_id=? AND s.name=?""", (pid, name)).fetchall()
    if not syms:
        db.close()
        if as_json:
            print(json.dumps({"found": False, "symbol": name, "results": []}))
        else:
            print(f"  '{name}' not found in {project}")
        return

    out = []
    for sid, sname, kind, spath, sline in syms:
        entry = {"symbol": sname, "kind": kind, "file": spath, "line": sline,
                 "callers": [], "callees": []}
        if direction in ("callers", "both"):
            entry["callers"] = [
                {"name": n, "kind": k, "file": p, "line": l, "via": ek}
                for n, k, p, l, ek in cur.execute("""
                    SELECT COALESCE(src.name, '<module>'), COALESCE(src.kind, 'file'),
                           sf.path, COALESCE(src.line, e.line), e.kind
                    FROM edges e
                    JOIN files sf ON e.src_file_id = sf.id
                    LEFT JOIN symbols src ON e.source_id = src.id
                    WHERE e.target_id = ?
                    ORDER BY sf.path LIMIT ?""", (sid, limit))]
        if direction in ("callees", "both"):
            entry["callees"] = [
                {"name": n, "kind": k, "file": p, "line": l, "via": ek}
                for n, k, p, l, ek in cur.execute("""
                    SELECT tgt.name, tgt.kind, tf.path, tgt.line, e.kind
                    FROM edges e
                    JOIN symbols tgt ON e.target_id = tgt.id
                    JOIN files tf ON tgt.file_id = tf.id
                    WHERE e.source_id = ?
                    ORDER BY tf.path LIMIT ?""", (sid, limit))]
        out.append(entry)
    db.close()

    def render(d):
        for e in d:
            print(f"\n  {e['symbol']} ({e['kind']}) → {e['file']}:{e['line']}")
            if e["callers"]:
                print(f"    ↑ used by {len(e['callers'])}:")
                for c in e["callers"]:
                    print(f"      {c['name']} ({c['kind']}) — {c['file']}:{c['line']} [{c['via']}]")
            if e["callees"]:
                print(f"    ↓ uses {len(e['callees'])}:")
                for c in e["callees"]:
                    print(f"      {c['name']} ({c['kind']}) — {c['file']}:{c['line']} [{c['via']}]")
            if not e["callers"] and not e["callees"]:
                print("    (no resolved edges)")
    _emit(out, as_json, render)


def godnodes(project: str, limit: int = 15, as_json: bool = False):
    db = get_db()
    cur = db.cursor()
    pid, _ = _project(cur, project)
    # GROUP BY over the indexed edge table beats v1's correlated COUNT(*)
    # subquery, which re-scanned edges once per symbol.
    rows = cur.execute("""
        SELECT s.name, s.kind, s.line, f.path, COUNT(e.id) AS refs
        FROM edges e
        JOIN symbols s ON e.target_id = s.id
        JOIN files f ON s.file_id = f.id
        WHERE e.project_id = ?
        GROUP BY e.target_id
        ORDER BY refs DESC
        LIMIT ?""", (pid, limit)).fetchall()
    db.close()
    data = [{"name": n, "kind": k, "file": p, "line": l, "refs": c}
            for n, k, l, p, c in rows]

    def render(d):
        print(f"\n  Top {len(d)} most-referenced symbols in {project}:")
        print(f"  {'#':<4} {'Symbol':<32} {'Kind':<11} {'Refs':<7} Location")
        print(f"  {'─'*4} {'─'*32} {'─'*11} {'─'*7} {'─'*44}")
        for i, r in enumerate(d, 1):
            print(f"  {i:<4} {r['name'][:32]:<32} {r['kind']:<11} "
                  f"{r['refs']:<7} {r['file']}:{r['line']}")
    _emit(data, as_json, render)


def bridges(project: str, limit: int = 20, as_json: bool = False):
    """Symbols pulled in from many different files — the real coupling points."""
    db = get_db()
    cur = db.cursor()
    pid, _ = _project(cur, project)
    rows = cur.execute("""
        SELECT s.name, s.kind, s.line, f.path,
               COUNT(DISTINCT e.src_file_id) AS caller_files
        FROM edges e
        JOIN symbols s ON e.target_id = s.id
        JOIN files f ON s.file_id = f.id
        WHERE e.project_id = ? AND e.src_file_id != s.file_id
        GROUP BY e.target_id
        ORDER BY caller_files DESC
        LIMIT ?""", (pid, limit)).fetchall()
    db.close()
    data = [{"name": n, "kind": k, "file": p, "line": l, "caller_files": c}
            for n, k, l, p, c in rows]

    def render(d):
        print(f"\n  Cross-file bridges in {project}:")
        print(f"  {'Symbol':<32} {'Kind':<11} {'Files':<7} Location")
        print(f"  {'─'*32} {'─'*11} {'─'*7} {'─'*44}")
        for r in d:
            print(f"  {r['name'][:32]:<32} {r['kind']:<11} "
                  f"{r['caller_files']:<7} {r['file']}:{r['line']}")
    _emit(data, as_json, render)


def orphans(project: str, limit: int = 30, as_json: bool = False):
    """Exported symbols nothing references.

    Uses NOT EXISTS, not NOT IN: v1's `id NOT IN (SELECT source_id ...)`
    evaluated to NULL for every row once any source_id was NULL — which was
    always — so this command silently returned nothing.
    """
    db = get_db()
    cur = db.cursor()
    pid, _ = _project(cur, project)
    rows = cur.execute("""
        SELECT s.name, s.kind, s.line, f.path
        FROM symbols s
        JOIN files f ON s.file_id = f.id
        WHERE s.project_id = ? AND s.exported = 1
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.target_id = s.id)
        ORDER BY f.path, s.line
        LIMIT ?""", (pid, limit)).fetchall()
    total = cur.execute("""
        SELECT COUNT(*) FROM symbols s
        WHERE s.project_id = ? AND s.exported = 1
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.target_id = s.id)
    """, (pid,)).fetchone()[0]
    db.close()
    data = {"total": total,
            "results": [{"name": n, "kind": k, "file": p, "line": l}
                        for n, k, l, p in rows]}

    def render(d):
        print(f"\n  Unreferenced exports in {project} ({d['total']} total):")
        for r in d["results"]:
            print(f"    {r['name']} ({r['kind']}) → {r['file']}:{r['line']}")
        if d["total"] > len(d["results"]):
            print(f"    ... and {d['total'] - len(d['results'])} more")
    _emit(data, as_json, render)


def path_between(project: str, src: str, dst: str, max_depth: int = 6,
                 as_json: bool = False):
    """Shortest call chain A → B (BFS, depth-limited)."""
    db = get_db()
    cur = db.cursor()
    pid, _ = _project(cur, project)

    src_ids = [r[0] for r in cur.execute(
        "SELECT id FROM symbols WHERE project_id=? AND name=?", (pid, src))]
    dst_ids = {r[0] for r in cur.execute(
        "SELECT id FROM symbols WHERE project_id=? AND name=?", (pid, dst))}
    if not src_ids or not dst_ids:
        db.close()
        missing = src if not src_ids else dst
        if as_json:
            print(json.dumps({"found": False, "reason": "symbol_not_found",
                              "missing": missing}))
        else:
            print(f"  '{missing}' not found in {project}")
        return

    adj = defaultdict(list)
    for a, b in cur.execute(
            "SELECT source_id, target_id FROM edges WHERE project_id=? "
            "AND source_id IS NOT NULL AND target_id IS NOT NULL", (pid,)):
        adj[a].append(b)
    names = {sid: (n, k) for sid, n, k in cur.execute(
        "SELECT id, name, kind FROM symbols WHERE project_id=?", (pid,))}
    locs = {sid: (p, l) for sid, p, l in cur.execute(
        """SELECT s.id, f.path, s.line FROM symbols s
           JOIN files f ON s.file_id=f.id WHERE s.project_id=?""", (pid,))}
    db.close()

    parents, found = {sid: None for sid in src_ids}, None
    frontier, depth = list(src_ids), 0
    if dst_ids & set(src_ids):
        found = next(iter(dst_ids & set(src_ids)))
    while frontier and found is None and depth < max_depth:
        nxt = []
        for cur_id in frontier:
            for nb in adj.get(cur_id, ()):
                if nb in parents:
                    continue
                parents[nb] = cur_id
                if nb in dst_ids:
                    found = nb
                    break
                nxt.append(nb)
            if found is not None:
                break
        frontier = nxt
        depth += 1

    if found is None:
        if as_json:
            print(json.dumps({"found": False, "reason": "no_path",
                              "max_depth": max_depth, "chain": []}))
        else:
            print(f"  No path from '{src}' to '{dst}' within {max_depth} hops")
        return

    chain, node = [], found
    while node is not None:
        n, k = names.get(node, (f"#{node}", "?"))
        p, l = locs.get(node, ("?", 0))
        chain.append({"name": n, "kind": k, "file": p, "line": l})
        node = parents.get(node)
    chain.reverse()

    def render(d):
        print(f"  Path ({len(d['chain'])-1} hops):")
        for i, node in enumerate(d["chain"]):
            arrow = "   " if i == 0 else " → "
            print(f"  {arrow}{node['name']} ({node['kind']})  "
                  f"{node['file']}:{node['line']}")
    _emit({"found": True, "hops": len(chain) - 1, "chain": chain}, as_json, render)


def context(project: str, name: str, as_json: bool = True):
    """Everything an agent needs about a symbol in one round trip:
    definition, callers, callees, and the rest of its file."""
    db = get_db()
    cur = db.cursor()
    pid, root = _project(cur, project)

    row = cur.execute("""
        SELECT s.id, s.name, s.kind, s.line, s.end_line, s.exported, f.path, f.id
        FROM symbols s JOIN files f ON s.file_id=f.id
        WHERE s.project_id=? AND s.name=?
        ORDER BY s.exported DESC, s.line LIMIT 1""", (pid, name)).fetchone()
    if not row:
        db.close()
        print(json.dumps({"error": f"'{name}' not found in {project}"}))
        return
    sid, sname, kind, line, end_line, exported, fpath, fid = row

    callers = [{"name": n or "<module>", "file": p, "line": l}
               for n, p, l in cur.execute("""
        SELECT src.name, sf.path, COALESCE(src.line, e.line)
        FROM edges e JOIN files sf ON e.src_file_id=sf.id
        LEFT JOIN symbols src ON e.source_id=src.id
        WHERE e.target_id=? LIMIT 30""", (sid,))]
    callees = [{"name": n, "file": p, "line": l}
               for n, p, l in cur.execute("""
        SELECT t.name, tf.path, t.line FROM edges e
        JOIN symbols t ON e.target_id=t.id JOIN files tf ON t.file_id=tf.id
        WHERE e.source_id=? LIMIT 30""", (sid,))]
    siblings = [{"name": n, "kind": k, "line": l}
                for n, k, l in cur.execute("""
        SELECT name, kind, line FROM symbols WHERE file_id=? AND id!=?
        ORDER BY line LIMIT 40""", (fid, sid))]
    db.close()

    print(json.dumps({
        "project": project, "root": root,
        "symbol": {"name": sname, "kind": kind, "file": fpath,
                   "line": line, "end_line": end_line, "exported": bool(exported),
                   "abs_path": os.path.join(root, fpath)},
        "callers": callers, "callees": callees, "file_symbols": siblings,
    }, indent=2))


def refresh_all(quiet: bool = False, as_json: bool = False):
    """Incrementally re-scan every indexed project from its recorded path.

    Cheap enough to run on a timer: unchanged files are skipped on a
    stat() alone, so a no-op pass over several thousand files is ~0.1s.
    """
    init_schema()
    db = get_db()
    rows = db.execute("SELECT name, path FROM projects ORDER BY name").fetchall()
    db.close()

    results = []
    for name, root in rows:
        if not os.path.isdir(root):
            results.append({"project": name, "error": f"path missing: {root}"})
            continue
        try:
            res = scan_project(name, root, quiet=True)
            results.append(res or {"project": name, "error": "scan failed"})
        except Exception as e:
            results.append({"project": name, "error": str(e)})

    if as_json:
        print(json.dumps(results, indent=2))
    elif not quiet:
        for r in results:
            if r.get("error"):
                print(f"  {r['project']:<16} error: {r['error']}")
            else:
                warn = len(r.get("unreadable") or [])
                suffix = f"  ⚠ {warn} unreadable" if warn else ""
                print(f"  {r['project']:<16} {r.get('parsed', 0)} parsed, "
                      f"{r.get('skipped', 0)} unchanged  "
                      f"({r.get('seconds', 0)}s){suffix}")
    return results


def list_projects(as_json: bool = False):
    db = get_db()
    rows = db.execute("""
        SELECT p.name, p.path, p.last_scan,
               (SELECT COUNT(*) FROM files f WHERE f.project_id=p.id),
               (SELECT COUNT(*) FROM symbols s WHERE s.project_id=p.id)
        FROM projects p ORDER BY p.name""").fetchall()
    db.close()
    data = [{"name": n, "path": p, "last_scan": ts, "files": fc, "symbols": sc}
            for n, p, ts, fc, sc in rows]

    def render(d):
        if not d:
            print("  No projects indexed. Run: glyph scan <name> <path>")
            return
        print(f"  {'Project':<20} {'Files':>7} {'Symbols':>9}  {'Last scan':<17} Path")
        print(f"  {'─'*20} {'─'*7} {'─'*9}  {'─'*17} {'─'*40}")
        for r in d:
            when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_scan"]))
                    if r["last_scan"] else "never")
            print(f"  {r['name']:<20} {r['files']:>7} {r['symbols']:>9}  "
                  f"{when:<17} {r['path']}")
    _emit(data, as_json, render)


def stats(project: str = None, as_json: bool = False):
    db = get_db()
    cur = db.cursor()
    where, params = ("WHERE p.name = ?", (project,)) if project else ("", ())
    rows = cur.execute(f"""
        SELECT p.id, p.name, p.last_scan,
               (SELECT COUNT(*) FROM files   f WHERE f.project_id=p.id),
               (SELECT COUNT(*) FROM symbols s WHERE s.project_id=p.id),
               (SELECT COUNT(*) FROM edges   e WHERE e.project_id=p.id),
               (SELECT COUNT(*) FROM edges   e WHERE e.project_id=p.id AND e.target_id IS NOT NULL),
               (SELECT COUNT(*) FROM file_history h WHERE h.project_id=p.id),
               (SELECT COUNT(*) FROM fallow_issues i WHERE i.project_id=p.id)
        FROM projects p {where} ORDER BY p.last_scan DESC""", params).fetchall()
    if not rows and project:
        db.close()
        raise ProjectNotFound(project)

    out = []
    for pid, name, ts, nf, ns, ne, nl, nh, ni in rows:
        kinds = dict(cur.execute(
            "SELECT kind, COUNT(*) FROM symbols WHERE project_id=? "
            "GROUP BY kind ORDER BY COUNT(*) DESC", (pid,)).fetchall())
        langs = dict(cur.execute(
            "SELECT lang, COUNT(*) FROM files WHERE project_id=? "
            "GROUP BY lang ORDER BY COUNT(*) DESC", (pid,)).fetchall())
        out.append({"project": name, "last_scan": ts, "files": nf, "symbols": ns,
                    "edges": ne, "linked_edges": nl, "history": nh, "issues": ni,
                    "kinds": kinds, "languages": langs})
    db.close()
    db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0

    def render(d):
        print(f"\n  glyph v{VERSION} — knowledge graph")
        print(f"  {'─'*52}")
        for r in d:
            when = (datetime.fromtimestamp(r["last_scan"]).strftime('%Y-%m-%d %H:%M')
                    if r["last_scan"] else "never")
            pct = (100 * r["linked_edges"] / r["edges"]) if r["edges"] else 0
            print(f"\n  {r['project']}")
            print(f"    Scanned:    {when}")
            print(f"    Structure:  {r['files']} files, {r['symbols']} symbols, "
                  f"{r['edges']} edges ({r['linked_edges']} linked, {pct:.0f}%)")
            if r["history"] or r["issues"]:
                print(f"    Extras:     {r['history']} git changes, {r['issues']} quality issues")
            if r["languages"]:
                langs = ", ".join(f"{k} {v}" for k, v in list(r["languages"].items())[:6])
                print(f"    Languages:  {langs}")
            if r["kinds"]:
                kinds = ", ".join(f"{k} {v}" for k, v in list(r["kinds"].items())[:8])
                print(f"    Kinds:      {kinds}")
        print(f"\n  DB: {db_size/1024/1024:.1f} MB  ({DB_PATH})")
    _emit(out, as_json, render)


# ═══════════════════════════════════════════════════════════════════
# PROJECT MAP
# ═══════════════════════════════════════════════════════════════════

def generate_map(project: str, out_path: str = None, top: int = 20):
    db = get_db()
    cur = db.cursor()
    pid, root = _project(cur, project)

    dirs = defaultdict(list)
    for fpath, lc in cur.execute(
            "SELECT path, line_count FROM files WHERE project_id=? ORDER BY path", (pid,)):
        dirs[os.path.dirname(fpath) or "."].append((os.path.basename(fpath), lc))

    gods = cur.execute("""
        SELECT s.name, s.kind, f.path, s.line, COUNT(e.id) AS refs
        FROM edges e JOIN symbols s ON e.target_id=s.id JOIN files f ON s.file_id=f.id
        WHERE e.project_id=? GROUP BY e.target_id ORDER BY refs DESC LIMIT ?""",
        (pid, top)).fetchall()

    bridge_rows = cur.execute("""
        SELECT s.name, s.kind, f.path, s.line, COUNT(DISTINCT e.src_file_id) AS cf
        FROM edges e JOIN symbols s ON e.target_id=s.id JOIN files f ON s.file_id=f.id
        WHERE e.project_id=? AND e.src_file_id != s.file_id
        GROUP BY e.target_id ORDER BY cf DESC LIMIT ?""", (pid, top)).fetchall()

    entry = cur.execute("""
        SELECT f.path, f.line_count, m.symbol_count
        FROM files f LEFT JOIN file_metrics m ON m.file_id=f.id
        WHERE f.project_id=? ORDER BY f.line_count DESC LIMIT ?""",
        (pid, top)).fetchall()

    exports = cur.execute("""
        SELECT s.kind, s.name, f.path, s.line
        FROM symbols s JOIN files f ON s.file_id=f.id
        WHERE s.project_id=? AND s.exported=1 ORDER BY s.kind, s.name""",
        (pid,)).fetchall()

    nf, ns, ne = (cur.execute(f"SELECT COUNT(*) FROM {t} WHERE project_id=?",
                              (pid,)).fetchone()[0] for t in ("files", "symbols", "edges"))
    db.close()

    L = [f"# {project} — Codebase Map", ""]
    L.append(f"*Generated by glyph v{VERSION} on {time.strftime('%Y-%m-%d %H:%M')}*")
    L.append("")
    L.append(f"**Root:** `{root}`  ")
    L.append(f"**Indexed:** {nf} files · {ns} symbols · {ne} edges")
    L.append("")

    L += ["## Largest Files", "", "| File | Lines | Symbols |", "|---|---:|---:|"]
    for p, lc, sc in entry:
        L.append(f"| `{p}` | {lc} | {sc or 0} |")
    L.append("")

    if gods:
        L += ["## Most-Referenced Symbols", "",
              "| Symbol | Kind | Refs | Defined in |", "|---|---|---:|---|"]
        for n, k, p, ln, c in gods:
            L.append(f"| `{n}` | {k} | {c} | `{p}:{ln}` |")
        L.append("")

    if bridge_rows:
        L += ["## Cross-File Bridges", "",
              "| Symbol | Kind | Caller files | Defined in |", "|---|---|---:|---|"]
        for n, k, p, ln, c in bridge_rows:
            L.append(f"| `{n}` | {k} | {c} | `{p}:{ln}` |")
        L.append("")

    L += ["## Directory Structure", "", "```"]
    for d in sorted(dirs):
        label = project if d == "." else d
        L.append(f"{label}/  ({len(dirs[d])} files)")
        for fname, lc in sorted(dirs[d])[:8]:
            L.append(f"    {fname}  ({lc} lines)")
        if len(dirs[d]) > 8:
            L.append(f"    ... {len(dirs[d]) - 8} more")
    L += ["```", ""]

    by_kind = defaultdict(list)
    for kind, n, p, ln in exports:
        by_kind[kind].append(f"- `{n}` — `{p}:{ln}`")
    if by_kind:
        L += ["## Exported API", ""]
        for kind in sorted(by_kind, key=lambda k: -len(by_kind[k])):
            L.append(f"### {kind} ({len(by_kind[kind])})")
            L += by_kind[kind][:30]
            if len(by_kind[kind]) > 30:
                L.append(f"- ... and {len(by_kind[kind]) - 30} more")
            L.append("")

    dest = out_path or os.path.join(root, "PROJECT_MAP.md")
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"[glyph] wrote {dest}  ({len(L)} lines)")


# ═══════════════════════════════════════════════════════════════════
# WATCH
# ═══════════════════════════════════════════════════════════════════

def watch_project(name: str, interval: int = 5):
    """Poll for changes and re-index incrementally.

    v1 called scan_project(name, "") here — an empty root, so it walked
    nothing and the loop was a no-op. The path comes from the DB.
    """
    db = get_db()
    cur = db.cursor()
    try:
        _, root = _project(cur, name)
    finally:
        db.close()
    print(f"[glyph] watching {name} at {root} (every {interval}s) — Ctrl+C to stop")
    try:
        while True:
            res = scan_project(name, root, quiet=True)
            if res and res.get("parsed"):
                print(f"  [{time.strftime('%H:%M:%S')}] {res['parsed']} file(s) "
                      f"re-indexed — {res['symbols']} symbols, {res['edges']} edges "
                      f"({res['seconds']}s)")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[glyph] watch stopped.")


# ═══════════════════════════════════════════════════════════════════
# GIT HISTORY
# ═══════════════════════════════════════════════════════════════════

def history_project(name: str, max_commits: int = 5000):
    init_schema()
    db = get_db()
    cur = db.cursor()
    pid, root = _project(cur, name)

    path_to_id = {p: i for p, i in cur.execute(
        "SELECT path, id FROM files WHERE project_id=?", (pid,))}
    cur.execute("DELETE FROM file_history WHERE project_id=?", (pid,))

    try:
        r = subprocess.run(
            ["git", "-C", root, "log", f"-n{max_commits}",
             "--format=%H%x00%at%x00%an%x00%s", "--name-status", "--diff-filter=AMDR"],
            capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  git log failed: {e}")
        db.close()
        return
    if r.returncode != 0:
        print(f"  git error: {r.stderr[:200]}")
        db.close()
        return

    rows, current, skipped = [], None, 0
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        if "\x00" in line:
            parts = line.split("\x00", 3)
            if len(parts) >= 4:
                current = (parts[0], int(parts[1]), parts[2], parts[3][:500])
            continue
        if "\t" in line and current:
            status = line[0]
            fpath = line.split("\t")[-1]
            fid = path_to_id.get(fpath)
            if fid is None:
                skipped += 1
                continue
            ctype = {"A": "added", "M": "modified",
                     "D": "deleted", "R": "renamed"}.get(status, "modified")
            rows.append((fid, pid, current[0], current[3], current[2],
                         current[1], ctype, "", ""))
    if rows:
        cur.executemany("""INSERT INTO file_history
            (file_id, project_id, commit_hash, commit_msg, author,
             committed_at, change_type, old_hash, new_hash)
            VALUES (?,?,?,?,?,?,?,?,?)""", rows)
    db.commit()
    db.close()
    print(f"[glyph] history: {len(rows)} entries stored "
          f"({skipped} touched paths outside the index)")


def hotspots(project: str, limit: int = 20, as_json: bool = False):
    """Files that change often AND are structurally central — where bugs live."""
    db = get_db()
    cur = db.cursor()
    pid, _ = _project(cur, project)
    rows = cur.execute("""
        SELECT f.path, COUNT(DISTINCT h.commit_hash) AS commits,
               COALESCE(m.line_count, 0), COALESCE(m.symbol_count, 0),
               (SELECT COUNT(DISTINCT e.src_file_id) FROM edges e
                JOIN symbols s ON e.target_id = s.id WHERE s.file_id = f.id) AS dependents
        FROM files f
        JOIN file_history h ON h.file_id = f.id
        LEFT JOIN file_metrics m ON m.file_id = f.id
        WHERE f.project_id = ?
        GROUP BY f.id
        ORDER BY commits * (dependents + 1) DESC
        LIMIT ?""", (pid, limit)).fetchall()
    db.close()
    if not rows:
        if as_json:
            print(json.dumps({"error": "no_history",
                              "hint": f"run: glyph history {project}", "results": []}))
        else:
            print(f"  No history for {project}. Run: glyph history {project}")
        return
    data = [{"file": p, "commits": c, "lines": lc, "symbols": sc, "dependents": d}
            for p, c, lc, sc, d in rows]

    def render(d):
        print(f"\n  Change hotspots in {project} (churn × dependents):")
        print(f"  {'File':<52} {'Commits':>8} {'Deps':>6} {'Lines':>7}")
        print(f"  {'─'*52} {'─'*8} {'─'*6} {'─'*7}")
        for r in d:
            print(f"  {r['file'][:52]:<52} {r['commits']:>8} "
                  f"{r['dependents']:>6} {r['lines']:>7}")
    _emit(data, as_json, render)


# ═══════════════════════════════════════════════════════════════════
# FALLOW INTEGRATION
# ═══════════════════════════════════════════════════════════════════

FALLOW_KINDS = ("dead-code", "health", "dupes")


def fallow_ingest(project: str, kinds: str = "all"):
    init_schema()
    db = get_db()
    cur = db.cursor()
    pid, root = _project(cur, project)

    analyses = list(FALLOW_KINDS) if kinds == "all" else [
        k.strip() for k in kinds.split(",") if k.strip()]
    bad = [k for k in analyses if k not in FALLOW_KINDS]
    if bad:
        print(f"  unknown analysis: {', '.join(bad)} (expected {', '.join(FALLOW_KINDS)})")
        db.close()
        return

    path_to_id = {p: i for p, i in cur.execute(
        "SELECT path, id FROM files WHERE project_id=?", (pid,))}
    total = 0
    for analysis in analyses:
        print(f"[glyph] running fallow {analysis} on {project}...")
        try:
            r = subprocess.run(
                ["npx", "fallow", analysis, "--root", root, "--format", "json", "--quiet"],
                capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            print(f"  ⚠ fallow {analysis} timed out — skipping")
            continue
        except FileNotFoundError:
            print("  ⚠ npx not found — install Node, then: npm install -g fallow")
            db.close()
            return
        if not r.stdout.strip():
            print(f"  ⚠ no output: {r.stderr[:200]}")
            continue
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            print(f"  ⚠ could not parse fallow {analysis} JSON")
            continue

        cur.execute("DELETE FROM fallow_issues WHERE project_id=? AND issue_kind=?",
                    (pid, analysis))
        version, now, issues = data.get("version", "unknown"), int(time.time()), []
        {"dead-code": _ingest_dead_code, "health": _ingest_health,
         "dupes": _ingest_dupes}[analysis](data, pid, path_to_id, now, version, issues)
        if issues:
            cur.executemany("""INSERT INTO fallow_issues
                (project_id, file_id, symbol_name, issue_kind, sub_kind, severity,
                 line, col, cyclomatic, cognitive, line_count, param_count,
                 crap_score, message, actions_json, ingested_at, scan_version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", issues)
        col = "fallow_last_" + analysis.replace("-", "_")
        cur.execute(
            f"""UPDATE file_metrics SET {col}=? WHERE project_id=? AND file_id IN
                (SELECT file_id FROM fallow_issues
                 WHERE project_id=? AND issue_kind=? AND file_id IS NOT NULL)""",
            (now, pid, pid, analysis))
        total += len(issues)
        print(f"  {analysis}: {len(issues)} issues (fallow v{version})")

    db.commit()
    db.close()
    print(f"\n[glyph] {total} fallow issues stored")


def _ingest_dead_code(data, pid, path_to_id, now, version, issues):
    def add(fid, sym, sub, sev, line, col, msg, actions):
        issues.append((pid, fid, sym, "dead-code", sub, sev, line, col,
                       None, None, None, None, None, msg,
                       json.dumps(actions or []), now, version))
    for e in data.get("unused_files", []):
        p = e.get("path", "")
        add(path_to_id.get(p), None, "unused_file", "warning", 0, 0,
            f"Unused file: {p}", e.get("actions"))
    for e in data.get("unused_exports", []):
        p, n = e.get("path", ""), e.get("name", "?")
        add(path_to_id.get(p), n, "unused_export", "warning",
            e.get("line", 0), e.get("col", 0), f"Unused export '{n}' in {p}", e.get("actions"))
    for e in data.get("unused_types", []):
        p, n = e.get("path", ""), e.get("name", "?")
        add(path_to_id.get(p), n, "unused_type", "warning",
            e.get("line", 0), 0, f"Unused type '{n}' in {p}", e.get("actions"))
    for e in data.get("unused_dependencies", []):
        n = e.get("name", "?")
        add(None, n, "unused_dependency", "info", 0, 0, f"Unused dependency: {n}", None)
    for e in data.get("circular_dependencies", []):
        paths = e.get("paths", [])
        add(None, None, "circular_dep", "error", 0, 0,
            " → ".join(paths) if paths else "circular dependency", None)
    for e in data.get("boundary_violations", []):
        add(path_to_id.get(e.get("path", "")), None, "boundary_violation", "error",
            0, 0, e.get("message", "boundary violation"), None)


def _ingest_health(data, pid, path_to_id, now, version, issues):
    for f in data.get("findings", []):
        path = f.get("path", "")
        cyclo, cog = f.get("cyclomatic"), f.get("cognitive")
        lines, params, crap = f.get("line_count"), f.get("param_count"), f.get("crap")
        exceeded = f.get("exceeded", "")
        sub = ({"cyclomatic": "high_cyclomatic", "cognitive": "high_cognitive",
                "line_count": "large_function", "param_count": "too_many_params",
                "all": "high_complexity"}.get(exceeded)
               or ("high_cyclomatic" if cyclo else "high_cognitive" if cog
                   else "large_function" if lines else "complexity"))
        parts = [p for p in (f"CC={cyclo}" if cyclo else None,
                             f"cognitive={cog}" if cog else None,
                             f"{lines} lines" if lines else None,
                             f"{params} params" if params else None,
                             f"CRAP={crap:.0f}" if crap else None) if p]
        name = f.get("name")
        issues.append((pid, path_to_id.get(path), name, "health", sub,
                       f.get("severity", "warning"), f.get("line", 0), f.get("col", 0),
                       cyclo, cog, lines, params, crap,
                       f"{name or '?'} in {path}: {', '.join(parts)}",
                       json.dumps(f.get("actions", [])), now, version))


def _ingest_dupes(data, pid, path_to_id, now, version, issues):
    """Ingest duplicate-block findings.

    Fallow 3.x reports `clone_groups` with an `instances` list keyed on
    "file"; 1.x/2.x reported `duplications` with `files` keyed on "path".
    Only the old shape was handled, so every v3 run silently ingested zero
    duplications while reporting success. Both are accepted now.
    """
    groups = []
    for g in data.get("clone_groups") or []:
        members = [
            (i.get("file") or i.get("path") or "",
             i.get("start_line", 0), i.get("end_line", 0))
            for i in (g.get("instances") or [])
        ]
        if members:
            groups.append(members)
    if not groups:                                   # fallback: pre-3.x shape
        for g in data.get("duplications") or []:
            members = [
                (d.get("path") or d.get("file") or "",
                 d.get("start_line", 0), d.get("end_line", 0))
                for d in (g.get("files") or [])
            ]
            if members:
                groups.append(members)

    for members in groups:
        peers = [m[0] for m in members]
        for path, start, end in members:
            others = [x for x in peers if x != path]
            span = max(end - start, 0)
            issues.append((
                pid, path_to_id.get(path), None, "dupes", "duplicate_block",
                "warning", start, 0, None, None, span, None, None,
                f"Duplicated block ({span} lines), also in: "
                f"{', '.join(others[:4]) or '—'}"
                + (f" (+{len(others) - 4} more)" if len(others) > 4 else ""),
                "[]", now, version,
            ))


def issues_list(project: str, kind: str = None, severity: str = None,
                limit: int = 50, as_json: bool = False):
    db = get_db()
    cur = db.cursor()
    pid, _ = _project(cur, project)

    where, params = ["fi.project_id = ?"], [pid]
    if kind:
        ks = [k.strip() for k in kind.split(",")]
        where.append(f"fi.issue_kind IN ({','.join('?' * len(ks))})")
        params += ks
    if severity:
        where.append("fi.severity = ?")
        params.append(severity)
    clause = " AND ".join(where)

    rows = cur.execute(f"""
        SELECT fi.issue_kind, fi.sub_kind, fi.severity,
               COALESCE(fi.symbol_name,'-'), COALESCE(f.path,'-'), fi.line, fi.message
        FROM fallow_issues fi LEFT JOIN files f ON fi.file_id = f.id
        WHERE {clause}
        ORDER BY CASE fi.severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1
                                  WHEN 'warning' THEN 2 ELSE 3 END,
                 fi.issue_kind, fi.line
        LIMIT ?""", [*params, limit]).fetchall()
    counts = cur.execute(f"""
        SELECT fi.issue_kind, fi.sub_kind, COUNT(*) FROM fallow_issues fi
        WHERE {clause} GROUP BY fi.issue_kind, fi.sub_kind
        ORDER BY fi.issue_kind, COUNT(*) DESC""", params).fetchall()
    db.close()

    data = {"summary": [{"kind": k, "sub_kind": s, "count": c} for k, s, c in counts],
            "issues": [{"kind": k, "sub_kind": s, "severity": sv, "symbol": sym,
                        "file": fp, "line": ln, "message": m}
                       for k, s, sv, sym, fp, ln, m in rows]}

    def render(d):
        if not d["issues"]:
            print(f"\n  No fallow issues for {project}. Run: glyph fallow {project}")
            return
        print(f"\n  Fallow issues — {project}")
        print(f"  {'─'*72}")
        for s in d["summary"]:
            print(f"    {s['kind']}/{s['sub_kind']}: {s['count']}")
        print()
        print(f"  {'Kind':<12} {'Sub-kind':<20} {'Sev':<9} {'Symbol':<24} Location")
        print(f"  {'─'*12} {'─'*20} {'─'*9} {'─'*24} {'─'*40}")
        for i in d["issues"]:
            loc = f"{i['file']}:{i['line']}" if i["file"] != "-" else "-"
            print(f"  {i['kind']:<12} {(i['sub_kind'] or '-'):<20} "
                  f"{(i['severity'] or '-'):<9} {i['symbol'][:24]:<24} {loc}")
    _emit(data, as_json, render)


def health_report(project: str):
    db = get_db()
    cur = db.cursor()
    pid, _ = _project(cur, project)
    has_fallow = cur.execute("SELECT COUNT(*) FROM fallow_issues WHERE project_id=?",
                             (pid,)).fetchone()[0] > 0

    print("\n  ╔═══════════════════════════════════════════════════════╗")
    print("  ║  glyph — codebase health report                       ║")
    print("  ╟───────────────────────────────────────────────────────╢")
    print(f"  ║  {project[:52]:<52} ║")
    print("  ╚═══════════════════════════════════════════════════════╝")

    if has_fallow:
        print("\n  ▸ Quality issues (fallow)")
        print(f"  {'─'*56}")
        last = None
        for ik, sk, sev, cnt in cur.execute("""
                SELECT issue_kind, sub_kind, severity, COUNT(*)
                FROM fallow_issues WHERE project_id=?
                GROUP BY issue_kind, sub_kind, severity
                ORDER BY issue_kind, COUNT(*) DESC""", (pid,)):
            if ik != last:
                print(f"\n  [{ik}]")
                last = ik
            print(f"    {(sk or '-'):<26} {(sev or '-'):<10} {cnt:>5}")
    else:
        print(f"\n  ⚠ No fallow data. Run: glyph fallow {project}")

    print("\n  ▸ Structural health (glyph)")
    print(f"  {'─'*56}")
    print("\n  Largest files:")
    print(f"  {'File':<52} {'Lines':>7} {'Symbols':>8}")
    print(f"  {'─'*52} {'─'*7} {'─'*8}")
    for p, lc, sc in cur.execute("""
            SELECT f.path, f.line_count, COALESCE(m.symbol_count, 0)
            FROM files f LEFT JOIN file_metrics m ON m.file_id=f.id
            WHERE f.project_id=? ORDER BY f.line_count DESC LIMIT 10""", (pid,)):
        print(f"  {p[:52]:<52} {lc:>7} {sc:>8}")

    if has_fallow:
        rows = cur.execute("""
            SELECT fi.symbol_name, COALESCE(f.path,'?'), fi.line,
                   fi.cyclomatic, fi.cognitive, fi.crap_score
            FROM fallow_issues fi LEFT JOIN files f ON fi.file_id=f.id
            WHERE fi.project_id=? AND fi.issue_kind='health'
              AND fi.cyclomatic IS NOT NULL
            ORDER BY fi.cyclomatic DESC LIMIT 12""", (pid,)).fetchall()
        if rows:
            print("\n  Most complex functions:")
            print(f"  {'Symbol':<28} {'Location':<40} {'CC':>4} {'Cog':>5} {'CRAP':>6}")
            print(f"  {'─'*28} {'─'*40} {'─'*4} {'─'*5} {'─'*6}")
            for n, p, l, cc, cog, crap in rows:
                print(f"  {(n or '?')[:28]:<28} {f'{p}:{l}'[:40]:<40} "
                      f"{cc or 0:>4} {cog or 0:>5} {crap or 0:>6.0f}")

    print("\n  Most-referenced symbols (change impact):")
    print(f"  {'Symbol':<32} {'Kind':<12} {'Refs':>6}")
    print(f"  {'─'*32} {'─'*12} {'─'*6}")
    for n, k, c in cur.execute("""
            SELECT s.name, s.kind, COUNT(e.id) FROM edges e
            JOIN symbols s ON e.target_id=s.id
            WHERE e.project_id=? GROUP BY e.target_id
            ORDER BY COUNT(e.id) DESC LIMIT 10""", (pid,)):
        print(f"  {n[:32]:<32} {k:<12} {c:>6}")

    orphan_n = cur.execute("""
        SELECT COUNT(*) FROM symbols s WHERE s.project_id=? AND s.exported=1
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.target_id = s.id)""",
        (pid,)).fetchone()[0]
    nf, ns, ne = (cur.execute(f"SELECT COUNT(*) FROM {t} WHERE project_id=?",
                              (pid,)).fetchone()[0] for t in ("files", "symbols", "edges"))
    last = cur.execute("SELECT last_scan FROM projects WHERE id=?", (pid,)).fetchone()[0]
    db.close()
    when = datetime.fromtimestamp(last).strftime('%Y-%m-%d %H:%M') if last else "never"
    print(f"\n  {'─'*56}")
    print(f"  {nf} files · {ns} symbols · {ne} edges · {orphan_n} unreferenced exports")
    print(f"  indexed {when}\n")


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def build_parser():
    import argparse
    p = argparse.ArgumentParser(
        prog="glyph", description=f"glyph v{VERSION} — codebase knowledge graph")
    p.add_argument("--version", action="version", version=f"glyph v{VERSION}")
    sub = p.add_subparsers(dest="cmd")

    def add(name, help_, *, json_flag=True):
        sp = sub.add_parser(name, help=help_)
        if json_flag:
            sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    s = add("scan", "index a project")
    s.add_argument("project"); s.add_argument("path")
    s.add_argument("--full", action="store_true", help="re-parse every file")
    s.add_argument("--jobs", type=int, default=0, help="parse workers (0 = auto)")
    s.add_argument("--quiet", action="store_true")

    s = add("find", "where is a symbol defined?")
    s.add_argument("project"); s.add_argument("symbol")
    s.add_argument("--limit", type=int, default=20)

    s = add("deps", "what uses this symbol / what it uses")
    s.add_argument("project"); s.add_argument("symbol")
    s.add_argument("--direction", choices=("callers", "callees", "both"), default="both")
    s.add_argument("--limit", type=int, default=40)

    s = add("context", "full agent context for a symbol (JSON)")
    s.add_argument("project"); s.add_argument("symbol")

    s = add("path", "shortest call chain A → B")
    s.add_argument("project"); s.add_argument("source"); s.add_argument("target")
    s.add_argument("--max-depth", type=int, default=6)

    for name, help_, default in (("godnodes", "most-referenced symbols", 15),
                                 ("bridges", "cross-file connectors", 20),
                                 ("orphans", "unreferenced exports", 30),
                                 ("hotspots", "churn × structural centrality", 20)):
        s = add(name, help_)
        s.add_argument("project")
        s.add_argument("--limit", type=int, default=default)

    s = add("stats", "index statistics")
    s.add_argument("project", nargs="?")

    s = add("list", "list indexed projects")

    s = add("refresh", "incrementally re-scan every indexed project")
    s.add_argument("--quiet", action="store_true")

    s = add("map", "write PROJECT_MAP.md", json_flag=False)
    s.add_argument("project")
    s.add_argument("--out", help="output path (default <root>/PROJECT_MAP.md)")
    s.add_argument("--top", type=int, default=20)

    s = add("watch", "poll and re-index on change", json_flag=False)
    s.add_argument("project")
    s.add_argument("--interval", type=int, default=5)

    s = add("history", "backfill git change history", json_flag=False)
    s.add_argument("project")
    s.add_argument("--max-commits", type=int, default=5000)

    s = add("fallow", "run fallow and ingest results", json_flag=False)
    s.add_argument("project")
    s.add_argument("kinds", nargs="?", default="all",
                   help="dead-code,health,dupes or 'all'")

    s = add("issues", "query ingested quality issues")
    s.add_argument("project")
    s.add_argument("--kind"); s.add_argument("--sev")
    s.add_argument("--limit", type=int, default=50)

    s = add("health", "combined structural + quality report", json_flag=False)
    s.add_argument("project")

    s = add("doctor", "check the index for problems", json_flag=False)

    return p


def doctor():
    """Sanity-check the index — catches the failure modes v1 shipped with."""
    init_schema()
    db = get_db()
    cur = db.cursor()
    print(f"\n  glyph v{VERSION} doctor")
    print(f"  {'─'*54}")
    print(f"  DB: {DB_PATH}")
    print(f"  schema version: {_detected_schema_version(db)}")
    langs = [t for t, fn in _LANG_FOR_TAG.items() if fn() is not None]
    print(f"  languages: {', '.join(sorted(set(langs))) or 'NONE — pip install tree-sitter-*'}")

    ident = re.compile(r"^[A-Za-z_$][\w$]*$")
    problems = 0
    # fetchall() first: re-using `cur` inside the loop would reset the very
    # iteration we are walking, silently stopping after the first project.
    for pid, name in cur.execute("SELECT id, name FROM projects").fetchall():
        ns = cur.execute("SELECT COUNT(*) FROM symbols WHERE project_id=?", (pid,)).fetchone()[0]
        ne = cur.execute("SELECT COUNT(*) FROM edges WHERE project_id=?", (pid,)).fetchone()[0]
        nl = cur.execute("SELECT COUNT(*) FROM edges WHERE project_id=? "
                         "AND target_id IS NOT NULL", (pid,)).fetchone()[0]
        bad = sum(1 for (n,) in cur.execute(
            "SELECT name FROM symbols WHERE project_id=?", (pid,)).fetchall()
            if not ident.match(n or ""))
        dangling = cur.execute("""SELECT COUNT(*) FROM edges e
            LEFT JOIN symbols s ON s.id=e.target_id
            WHERE e.project_id=? AND e.target_id IS NOT NULL AND s.id IS NULL""",
            (pid,)).fetchone()[0]
        print(f"\n  {name}")
        print(f"    symbols            {ns}")
        print(f"    edges              {ne}  ({nl} linked, "
              f"{100*nl/ne if ne else 0:.0f}%)")
        ok = lambda c: "ok" if c == 0 else "PROBLEM"
        print(f"    malformed names    {bad}  [{ok(bad)}]")
        print(f"    dangling targets   {dangling}  [{ok(dangling)}]")
        problems += (bad > 0) + (dangling > 0)
        if ne and nl == 0:
            print(f"    ⚠ no edge resolves to a symbol — re-run: glyph scan {name} <path> --full")
            problems += 1
    db.close()
    print(f"\n  {'all checks passed' if problems == 0 else f'{problems} problem(s) found'}\n")
    return problems


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    j = getattr(args, "json", False)
    try:
        if args.cmd == "scan":
            scan_project(args.project, args.path, full=args.full,
                         jobs=args.jobs, quiet=args.quiet)
        elif args.cmd == "find":
            find_symbol(args.project, args.symbol, args.limit, j)
        elif args.cmd == "deps":
            deps(args.project, args.symbol, args.direction, args.limit, j)
        elif args.cmd == "context":
            context(args.project, args.symbol)
        elif args.cmd == "path":
            path_between(args.project, args.source, args.target, args.max_depth, j)
        elif args.cmd == "godnodes":
            godnodes(args.project, args.limit, j)
        elif args.cmd == "bridges":
            bridges(args.project, args.limit, j)
        elif args.cmd == "orphans":
            orphans(args.project, args.limit, j)
        elif args.cmd == "hotspots":
            hotspots(args.project, args.limit, j)
        elif args.cmd == "stats":
            stats(args.project, j)
        elif args.cmd == "list":
            list_projects(j)
        elif args.cmd == "refresh":
            refresh_all(args.quiet, j)
        elif args.cmd == "map":
            generate_map(args.project, args.out, args.top)
        elif args.cmd == "watch":
            watch_project(args.project, args.interval)
        elif args.cmd == "history":
            history_project(args.project, args.max_commits)
        elif args.cmd == "fallow":
            fallow_ingest(args.project, args.kinds)
        elif args.cmd == "issues":
            issues_list(args.project, args.kind, args.sev, args.limit, j)
        elif args.cmd == "health":
            health_report(args.project)
        elif args.cmd == "doctor":
            return 1 if doctor() else 0
    except ProjectNotFound as e:
        print(f"  project '{e}' is not indexed. Run: glyph scan {e} <path>")
        return 2
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
