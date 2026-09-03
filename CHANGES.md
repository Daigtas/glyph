# Glyph changelog

## v2.0.1 — 2026-09-03

Two silent-failure fixes found while dogfooding v2.0.0.

- **Unreadable files were skipped without a word.** `walk_source_files` and the
  parse worker both swallowed `OSError`, so a file or directory the user could
  not read simply never appeared in the index — no warning, no count. On a real
  project this hid five root-level config files, including a Next.js
  `middleware.ts`, from an index that otherwise looked complete. Scans now
  report every path they could not read, and `glyph refresh` shows a per-project
  count.
- **`glyph fallow <project> dupes` silently ingested nothing against Fallow 3.x.**
  The ingester looked for a top-level `duplications` key with `files[].path`;
  Fallow 3.x emits `clone_groups` with `instances[].file`. Every run reported
  "0 issues" and success. Both shapes are now accepted — the same project went
  from 0 to 4,261 duplicate blocks, matching Fallow's own `clone_instances` stat.

## v2.0.0 — 2026-09-03

Correctness rewrite. v1's graph was largely non-functional; this fixes the
causes and roughly triples scan speed.

### Correctness

- **Byte offsets were sliced into a decoded `str`.** tree-sitter reports byte
  offsets, so one non-ASCII character shifted every later symbol name in a
  file. Measured on a live index: **26,240 of 57,121 symbols (46%) were
  corrupt** — names like `'\n\n    const totalPushed = ...'`. Extraction now
  slices the source bytes and decodes per node.
- **`source_id` was hardcoded `None`** in edge resolution, so every edge had a
  target and no source: 0 of 18,927 edges were attributed. `deps`, `path` and
  `bridges` therefore returned nothing on any project, ever. Edges now carry a
  real source symbol via scope tracking during the AST walk, plus a
  `src_file_id` so module-level imports stay attributable.
- **Incremental scans destroyed the graph.** Re-parsing a file deleted the
  inbound edges pointing at its symbols and never rebuilt them. Measured:
  editing 10 files dropped forgehub from **3,265 to 2,109 edges (−35%)**.
  Edges now store `target_name` beside `target_id` and a repair pass re-points
  them after a re-parse. Verified lossless over repeated edit cycles.
- **`NOT IN (SELECT source_id ...)` with NULLs.** SQL `NOT IN` yields NULL —
  never true — if the subquery returns any NULL, so `orphans` silently
  returned zero rows. Replaced with `NOT EXISTS`.
- **Imports were never extracted.** The walker looked up a field named
  `clause`, which does not exist in the TS grammar (the node type is
  `import_clause`). Only 24 import edges existed across 4,710 files.
- **`.d.ts` / `.min.js` were never ignored** — `Path("x.d.ts").suffix` is
  `.ts`. Now matched with `endswith()`.
- **`watch` was a no-op**: it called `scan_project(name, "")` with an empty
  root. It now reads the project path from the DB.
- **Destructuring bindings** (`const { a, b } = x`) were stored as one
  unsearchable blob; they now bind each name individually (+2,457 real
  symbols on forgehub).
- **Export visibility is enforced during resolution.** A non-exported symbol
  is only reachable inside its own file, so member calls like
  `parsed.error.flatten()` no longer link to an unrelated module-local
  `flatten()` (that alone invented 678 false edges on forgehub).
- `_ingest_dupes` built its message from a list that grew inside the loop, so
  each row in a duplication group got a different truncated peer list.
- `doctor`/`stats` reused one cursor inside its own iteration, which resets
  the cursor and silently stopped after the first project.
- `generate_map` credited output to "codex".

### Performance

Measured on boottify.com (3,161 files) and forgehub (1,576 files):

| Operation | v1 | v2 |
|---|---|---|
| Full scan, 3,161 files | 7.79 s | **2.57 s** (3.0×) |
| Full scan, 1,576 files | 3.31 s | **0.99 s** (3.3×) |
| No-op incremental | 0.29 s | **0.17 s** (1.7×) |
| Edges extracted (forgehub) | 3,265 | **31,491** |
| Edges resolved to a symbol | 0 | **14,048** |

- `stat()`-based change detection: unchanged files are never read or hashed.
- Parallel parsing across processes (`--jobs`, auto-sized).
- BLAKE2b instead of MD5; parsers cached instead of rebuilt per file.
- Iterative AST walk (no recursion limit, less overhead).
- Symbol ids allocated up front, so inserts are one `executemany` and edges
  can reference symbols before insert — no per-file `INSERT`+`SELECT`.
- `GROUP BY` joins replace correlated `COUNT(*)` subqueries in
  `godnodes`/`bridges`.
- No-op scans skip the edge repair pass entirely.

### New

- `glyph context <project> <symbol>` — definition, callers, callees and file
  symbols in one JSON call, for agent use.
- `glyph doctor` — index self-check (malformed names, dangling targets,
  resolution rate).
- `glyph refresh` — incrementally re-scan every indexed project.
- `glyph hotspots` — churn × structural centrality, from git history.
- `--json` on every query command, with a consistent contract on the
  not-found paths.
- argparse CLI with real flags; `ON DELETE CASCADE` throughout; schema
  versioning with an automatic v1→v2 migration (backs the old DB up first).
- Python: methods vs functions distinguished, module-level public names
  treated as exports. Go: exports detected by capitalisation.

### Migration

v1 data is not salvageable (46% corrupt names, zero edge sources). On first
run the DB is backed up to `~/.glyph/glyph.db.v1-backup-<timestamp>`, project
names and paths are kept, and derived data is rebuilt on the next scan.
