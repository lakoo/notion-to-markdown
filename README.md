# Notion to Markdown

A single-file CLI tool that converts Notion pages, databases, or views to Markdown via the Notion API. Zero external dependencies — only Python 3.9+ and a Notion API key.

## Features

- **Page** — Convert any Notion page to Markdown; embedded database blocks are expanded inline.
- **Database** — Render a database as a Markdown table using its first accessible view.
- **View** — Render a specific view (identified by `?v=` in the URL) as a Markdown table.
- **Frontmatter** — Output includes YAML-style metadata (source URL, block ID, type, title, breadcrumbs, fetch time, tool version).
- **Rate-limit resilience** — Automatic retry with `Retry-After` header or exponential backoff on 429 responses.
- **Configurable logging** — `--verbose` / `--quiet` flags control log output on stderr.

## Requirements

- Python 3.9+
- `NOTION_API_KEY` environment variable — set to a valid Notion API integration token.

## Usage

```bash
export NOTION_API_KEY="ntn_xxxxx"

# Render a page to stdout
./notion2md.py "https://www.notion.so/My-Page-1a2b3c4d..."

# Render a specific view
./notion2md.py "https://www.notion.so/...?v=abc123"

# Write Markdown to a file
./notion2md.py -o output.md "https://www.notion.so/..."

# Quiet mode (stderr: warnings & errors only)
./notion2md.py -q "https://www.notion.so/..."
```

### Options

| Flag | Description |
|------|-------------|
| `-v`, `--verbose` | Enable debug logging to stderr |
| `-q`, `--quiet` | Suppress info logs, show warnings/errors only |
| `-o FILE`, `--output FILE` | Write output to a file instead of stdout |
| `--version` | Show version and exit |

## Background

Notion's API version `2026-03-11` introduced the **Views API** (`/views`, `/views/{id}/queries`, `/data_sources/{id}/query`) and the **Page Markdown API** (`/pages/{id}/markdown`), both previously unavailable.

Under older API versions, fetching a page required recursively walking `/blocks/{id}/children` to discover child blocks. Inline databases appeared as `child_database` blocks, but there was no way to retrieve the corresponding view — and therefore no way to determine the filter, sort, or column configuration that the Notion UI applies. As a result, existing tools and MCP servers either skip inline databases or render them without proper filtering.

This tool targets `2026-03-11` and uses the Views API to:

- Retrieve the view associated with an inline database from its embedded URL
- Apply the view's server-side filter and sort logic via view queries
- Expand the result into a Markdown table

## Architecture

The script uses a **two-phase approach** to render database content:

1. **Query the data source** via `POST /data_sources/{id}/query` — returns full row data (all properties resolved).
2. **Create a view query** via `POST /views/{id}/queries` — returns only page IDs in view order with server-side filter/sort applied.

Results are intersected by page ID: the data source provides the row data, and the view query supplies the correct ordering and filtering. This avoids making N+1 individual page fetches just to resolve row data.

### Design notes

**Why not just use the View Query API alone?**
The View Query API (`/views/{id}/queries`) returns only page IDs, not property values. Fetching each page individually would create an N+1 problem. Instead, the script queries the data source for full row data and uses the view query solely for ordering and filtering.

**View filters can be stale or incomplete.**
The `filter` field on a View object may include property conditions with no operator set — the Notion UI ignores these silently, but `POST /data_sources/{id}/query` rejects them with a 400 error. The `_clean_filter()` function recursively strips empty conditions before sending.

**The `filter_properties` parameter can also cause 400 errors.**
Filtering to visible columns is an optimization that reduces payload size, but unknown column IDs trigger a 400. The script attempts it and falls back to all columns if it fails.

**Filter nesting is limited to 2 levels.**
The Data Source Query API only accepts compound filters (`and` / `or`) up to 2 levels deep. When a view's built-in filter is already at this limit (e.g. `or(A, and(B, C))`), merging quick-filters would exceed it. The script detects this via `_has_nested_compound()` and relies on view query intersection to fill the filtering gap instead.

## Known limitations

- **Relation properties** — Output as `<N relation(s)>` (placeholder; no fetched summaries).
- **Rollup arrays** — Output as `<array>` (placeholder).
- **Database columns** — Derived from the first row's property keys; columns not present in the first row are omitted.
- **Pagination of final output** — The script assumes the output fits in memory; no streaming support.

## Claude Code Skill

This repository includes [SKILL.md](SKILL.md) for use as a [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview) skill. When loaded, Claude Code can automatically invoke `notion2md.py` when a user provides a Notion URL and asks to read its content.

## Project history

The git history is organized into two phases:

- **Reconstructed history** — Commits prefixed with `snapshot(...)` were created retrospectively from dated snapshots to preserve the evolution of the project.
- **Real development** — Commits after the snapshot phase use conventional commit prefixes (`feat:`, `fix:`, `docs:`, etc.).
