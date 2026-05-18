---
name: notion-to-markdown
description: Fetch and convert Notion pages, databases, or views into Markdown when a user provides a notion.so / .notion.site URL and asks to read or convert its content. Notion pages cannot be accessed with WebFetch (requires JS login). Do NOT trigger if a Notion URL is merely mentioned without a request to read it.
---

# Notion to Markdown Skill

Use this skill when the user provides a Notion URL and asks you to read or convert it to Markdown. The `notion2md.py` script fetches Notion content via the Notion API and renders it as Markdown — WebFetch and browser tools cannot do this.

## Requirements

- `NOTION_API_KEY` environment variable must be set.
- Python 3 must be available.

## Automatic Trigger

Invoke this skill when **both** conditions are met:
1. The user provides a URL containing `notion.so` or `.notion.site`
2. The user asks you to read, fetch, convert, summarize, or otherwise access the content

Do **not** trigger if a Notion URL is merely mentioned without intent to read it.

## How to Invoke

The `notion2md.py` script lives in the same directory as this SKILL.md file. When this skill loads, note the SKILL.md path — the script is at the same level. Run it from that directory:

```bash
# cd to the skill directory first, then invoke
cd <skill-directory> && python3 ./notion2md.py <notion_url> [options]
```

Or use the full path (resolve `<skill-directory>` from the skill load context):
```bash
python3 <skill-directory>/notion2md.py <notion_url> [options]
```

Supported URL formats:

| URL Type | Example | Renders |
|----------|---------|---------|
| Page | `https://www.notion.so/My-Page-1a2b3c4d...` | Page markdown with embedded database tables |
| Database | `https://www.notion.so/1a2b3c4d...` | Markdown table using the first accessible view |
| View | `https://www.notion.so/...?v=view_uuid` | Specific view as a markdown table |

### Options

| Flag | Description |
|------|-------------|
| `-v` / `--verbose` | Enable debug output (to stderr) |
| `-q` / `--quiet` | Suppress info output, show warnings/errors only |
| `-o FILE` / `--output FILE` | Write Markdown output to a file instead of stdout |
| `--version` | Show version and exit |

### Recommended Workflow (for agents)

1. **Write output to a temp file** with `-o /tmp/notion-<name>.md` instead of printing to stdout.
2. **Read the file** with the Read tool to inspect the content.
3. **Re-read as needed** — the temp file persists for the session, avoiding repeated API calls.

### Examples

```bash
# Render to a temp file — resolve <skill-directory> from the SKILL.md path
python3 <skill-directory>/notion2md.py -o /tmp/notion-output.md "https://www.notion.so/...?v=abc123"

# Quick check to stdout
python3 <skill-directory>/notion2md.py "https://www.notion.so/My-Page-1a2b3c4d..."
```

## Verification

After converting, confirm:
- Output is valid Markdown (headings, lists, tables, etc. are intact).
- All blocks from the Notion page are captured (no empty sections where content should be).
- Embedded database tables are expanded correctly (look for `_(error` markers).

## Timeout & Progress

- **stdout is empty until the script finishes** — the entire output is assembled and then emitted at once. Seeing no stdout does NOT mean the script is stuck.
- **Progress is logged to stderr in real-time.** Watch for heartbeat lines like `[render_view]` and `[render_database]`, which indicate the script is actively processing.
- **Pages with many embedded databases can take 60+ seconds.** Do not impose a short timeout. When invoking through a shell tool, use `timeout: 120000` (or omit the timeout) to allow enough time.
- **Use `-v` for more detail.** Debug-level logs show each Notion API call, which helps diagnose slow pages.

## Notes

- The script outputs rendered Markdown to **stdout**. Log messages go to **stderr**, so capturing stdout cleanly (e.g., redirecting to a file) works without log noise.
- Embedded `<database>` tags in page content are expanded into Markdown tables automatically.
- If `NOTION_API_KEY` is not set, the script exits with an error.
