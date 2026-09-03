<h1>
  <img src="assets/logo-128.png" alt="" width="32" height="32" align="absmiddle">
  Glyph
</h1>

> **Carve meaning into your codebase.**

[![Website](https://img.shields.io/badge/website-glyph.boottify.com-%23d2f800?style=flat-square)](https://glyph.boottify.com)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://www.python.org/)
[![Tools](https://img.shields.io/badge/Boottify-Tools-%23d2f800?style=flat-square)](https://boottify.com/tools)

Glyph is a **lightning-fast, zero-LLM-cost codebase indexer** that builds a knowledge graph of your source code using tree-sitter AST parsing. It answers the questions that slow you down: *Where is X defined? What calls this? What are the most-connected symbols? Show me the call chain from A to B.*

> ### 🚨 v2.0.0 — correctness rewrite
>
> If you used v1.x, **re-index**. v1 had three defects that made most of the graph
> unusable, all now fixed and regression-tested:
>
> - Symbol names were extracted with **byte offsets sliced into a decoded string** —
>   a single non-ASCII character corrupted every later name in a file.
>   Measured on a live 57,121-symbol index: **46% of names were garbage.**
> - Every edge's `source_id` was hardcoded `None`, so `deps`, `path` and `bridges`
>   **returned nothing on any project, ever** (0 of 18,927 edges had a source).
> - Incremental scans deleted inbound edges without rebuilding them —
>   **editing 10 files destroyed 35% of the graph.**
>
> Plus a **3× faster** full scan. See [CHANGES.md](CHANGES.md).

---

## The Problem

Navigating large codebases is hard. Tools like `grep` and `rg` find text, but they can't tell you structure. They can't show you call chains, identify cross-module bridges, or surface the "god nodes" that everything depends on.

We tried [Graphify](https://github.com/safishamsi/graphify) — an ambitious tool that combines AST parsing with LLM-powered semantic extraction. But in practice:

| Graphify Problem | Real-World Impact |
|-----------------|-------------------|
| **LLM extraction fails silently** | DeepSeek/OpenAI API errors mid-job, no retry, no resume |
| **Full rescan every time** | 2,400 files = 90+ seconds, even if you changed 3 files |
| **No incremental updates** | No file hashing, no dirty tracking — always starts from zero |
| **Code skips LLM anyway** | The docs admit "code files skip LLM extraction" — so the big value prop is dead on arrival for TypeScript/Python |
| **Fragile installation** | npm link issues, Node version sensitivity |
| **JSON dump, no query engine** | Raw JSON output with no indexed lookups, no path traversal |
| **No multi-project view** | One project at a time, no cross-repo awareness |
| **Performative boot screen** | "357 agents across 12 departments" — it's a static ASCII banner, not actually orchestrating anything |

---

## How Glyph Is Different

| Feature | Graphify | Glyph |
|---------|----------|-------|
| **Indexing engine** | AST + LLM (but LLM often fails) | Pure tree-sitter AST — fast, reliable, never fails |
| **Incremental updates** | ❌ Full rescan every time | ✅ MD5 file hashing — 3 changed files = <100ms |
| **LLM cost** | $0.002–$0.05 per scan | **$0.00** — AST is free |
| **Storage** | JSON files | SQLite with indexed lookups |
| **Query engine** | CLI + regex on JSON | BFS path traversal, indexed symbol lookup, edge counting |
| **Multi-project** | ❌ | ✅ One DB, many projects |
| **Installation** | npm + Node version dance | `pip install` + one script |
| **Languages** | TS, Python | TS/TSX/JS/JSX, Python, Go, Bash |
| **Agent-ready output** | ❌ | ✅ `--json` on every command, plus `glyph context` |
| **Index self-check** | ❌ | ✅ `glyph doctor` |
| **Auto-generated maps** | ❌ | ✅ `glyph map` generates `PROJECT_MAP.md` |
| **Lines of code** | ~5,000+ (complex multi-module) | **~2,200** (single file, readable) |

---

## What Glyph Tells You

```bash
# Where is something defined?
glyph find myproject sendEmail
  → SendEmailOptions (interface) → src/lib/email/types.ts:66
  → sendEmailSchema (const) → src/app/api/email/send/route.ts:19

# Show me the god nodes — most-connected symbols
glyph godnodes myproject
  → Button (component) — 713 connections
  → Card (component) — 696 connections
  → cn (function) — 422 connections

# Trace a call chain
glyph path myproject "webhookHandler" "sendEmail"
  → webhookHandler → processPush → deployApp → notifyUser → sendEmail (4 hops)

# Find cross-file bridges
glyph bridges myproject
  → logger — called from 47 different files
  → withErrorHandler — called from 35 different files

# Generate a PROJECT_MAP.md for your repo
glyph map myproject
  → Writes PROJECT_MAP.md with directory tree, god nodes, key symbols

# Stats at a glance
glyph stats myproject
  → boottify: 3,161 files, 45,191 symbols, 74,589 edges

# Everything about one symbol, as JSON — for AI agents
glyph context myproject sendEmail
  → definition + line range + every caller + every callee + the file's other symbols

# Which files change often AND are structurally central? (needs `glyph history`)
glyph hotspots myproject
  → lib/auth.ts — 4 commits, 81 dependents

# Is the index healthy?
glyph doctor
  → malformed names 0 [ok] · dangling targets 0 [ok] · all checks passed

# Re-scan every indexed project (cheap enough to cron)
glyph refresh
  → boottify 0 parsed, 3161 unchanged (0.08s)
```

---

## Installation

```bash
# Clone and install
git clone https://github.com/Daigtas/glyph.git
cd glyph
./install.sh
```

Or one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/Daigtas/glyph/main/install.sh | bash
```

**Requirements:** Python 3.10+, pip, git

---

## Quick Start

```bash
# 1. Index your project
glyph scan myproject /path/to/your/repo

# 2. Explore
glyph find myproject sendEmail
glyph godnodes myproject
glyph bridges myproject
glyph path myproject "auth" "email"

# 3. Generate documentation
glyph map myproject       # Creates PROJECT_MAP.md at repo root
glyph stats               # All indexed projects

# 4. Update after code changes (incremental — only changed files)
glyph scan myproject /path/to/your/repo

# 5. Index more projects
glyph scan another /path/to/another/repo
glyph list                # See all indexed projects
```

---

## How It Works

```
┌─────────────────────────────────────────┐
│  glyph scan myproject /path/to/repo     │
│                                         │
│  1. Walk filesystem                     │
│     ↓                                   │
│  2. MD5 hash → skip unchanged files     │  ← incremental!
│     ↓                                   │
│  3. tree-sitter AST parse               │  ← TypeScript, Python, Go, Bash
│     ↓                                   │
│  4. Extract symbols + edges             │  ← functions, classes, imports, calls
│     ↓                                   │
│  5. Store in SQLite (~/.glyph/glyph.db) │  ← indexed, queryable
│     ↓                                   │
│  glyph find / godnodes / path / map     │
└─────────────────────────────────────────┘
```

### Symbol Kinds Detected

- **TypeScript/JSX:** `function`, `class`, `const`, `let`, `var`, `interface`, `type`, `enum`, `component` (React), `method`
- **Python:** `function`, `class`
- **Go:** `function`, `method`, `type`
- **Bash:** `function`

### Edge Kinds

- `import` — module imports
- `call` — function/method calls
- `jsx_use` — React component usage in JSX

---

## Real-World Performance

Measured on two production Next.js codebases, v1.2.0 vs v2.0.0 on the same machine:

| Metric | v1.2.0 | v2.0.0 |
|--------|--------|--------|
| Full scan — 3,161 files | 7.79 s | **2.57 s** (3.0× faster) |
| Full scan — 1,576 files | 3.31 s | **0.99 s** (3.3× faster) |
| No-op incremental scan | 0.29 s | **0.17 s** |
| `glyph refresh`, 5,015 files across 3 projects | — | **0.28 s** |
| Edges extracted (1,576-file repo) | 3,265 | **31,491** |
| Edges resolved to a symbol | **0** | **14,048** |
| Corrupt symbol names | **46%** | **0%** |
| LLM cost | $0.00 | **$0.00** |

Accuracy spot-checked against `grep` on a 1,576-file repo: glyph reports 260 caller
files for `withAuth` vs grep's 263, and 405 vs 401 for the `db` singleton — within
1-2%, with the difference being import-only files that grep counts differently.

```
Symbol kinds (3,161-file repo):
  const            35,195
  function          6,064
  interface         2,123
  let               1,142
  type                283
  method              184
  component           167
  class                27
```

> **What "resolved" means.** Roughly 45-50% of extracted edges resolve to a symbol
> *inside* the project; the rest are calls into `node_modules`/stdlib, which Glyph
> deliberately does not index. An unresolved edge means "points outside the
> project", not "not called".

---

## Use Cases

- **AI coding agents** — Give your agent a map of the codebase before it starts working. Dramatically reduces the "find → read → trace" tool-call loop.
- **Onboarding** — New developers get `PROJECT_MAP.md` instantly.
- **Refactoring** — Find all callers of a function, identify unused exports.
- **Architecture review** — God nodes and bridges reveal structural coupling.
- **Multi-repo navigation** — Index all your projects in one DB.

---

## Compared to...

| Tool | Approach | Glyph Advantage |
|------|----------|-----------------|
| `grep` / `rg` | Text search | Structure-aware — "show me what *calls* this" |
| Graphify | AST + optional LLM | Incremental updates, SQLite queries, multi-project |
| Sourcegraph | Cloud-hosted, full LSIF | Local, free, zero setup |
| GitHub Code Search | Text index | Finds definitions by AST, not just text matches |

---

## Roadmap

- [x] `glyph watch` — polling watcher for live incremental updates *(v2.0)*
- [x] Source-level edge resolution — scope-tracked, import- and visibility-aware *(v2.0)*
- [x] JSON output on every command + `glyph context` for AI agents *(v2.0)*
- [x] `glyph doctor` — index integrity self-check *(v2.0)*
- [x] `glyph hotspots` — churn × structural centrality from git history *(v2.0)*
- [ ] Community detection (Louvain algorithm)
- [ ] HTML graph visualization
- [ ] PR diff mode — "what changed in the graph between commits?"
- [ ] Rust / Java / C# extractors
- [ ] VSCode extension

---

## Contributing

Glyph is a single Python file. It's designed to be readable and hackable.

```bash
git clone https://github.com/Daigtas/glyph.git
cd glyph
python3 -m venv venv
source venv/bin/activate
pip install tree-sitter tree-sitter-typescript tree-sitter-python tree-sitter-go tree-sitter-bash
python3 glyph.py scan test /path/to/some/code
```

Run `glyph doctor` after any change to the extractors — it catches malformed
symbol names, dangling edge targets, and a graph where nothing resolves.

Pull requests welcome. See `glyph.py` — it's ~2,200 lines with clear sections.

---

## License

MIT © [Boottify](https://boottify.com)

---

*"The most valuable tool for an AI agent isn't a faster grep — it's a map."*

---

🌐 **[glyph.boottify.com](https://glyph.boottify.com)** — landing page, docs, and live demo
