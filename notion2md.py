#!/usr/bin/env python3
import os
import re
import json
import sys
import time
import textwrap
import argparse
import logging
import urllib.request
import urllib.error
import urllib.parse

__version__ = "0.3.5"

logger = logging.getLogger(__name__)

NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_VERSION = "2026-03-11"
NOTION_API_BASE = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


# ─── HTTP Layer ────────────────────────────────────────────────────────────────

def _request_notion_api(path: str, method: str = "GET", body=None, timeout: int = 60, _retries: int = 3) -> dict:
    url = f"{NOTION_API_BASE}{path}"
    req = urllib.request.Request(url, headers=HEADERS, method=method)
    if body is not None:
        req.data = json.dumps(body).encode()
    logger.debug("[_request_notion_api] method=%s, url=%s, data=%s", method, url, req.data)

    for attempt in range(_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < _retries - 1:
                retry_after = e.headers.get("Retry-After")
                wait = int(retry_after) if retry_after else 2 ** attempt
                logger.warning(
                    "[_request_notion_api] 429 rate limited, retrying in %ds (attempt %d/%d)",
                    wait, attempt + 1, _retries,
                )
                time.sleep(wait)
                continue
            raise


# ─── Utilities ─────────────────────────────────────────────────────────────────

def to_uuid(s: str) -> str:
    s = s.replace("-", "")
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"


def _url_encode(s: str) -> str:
    """URL-encode a string (e.g. property id with special chars)."""
    return urllib.parse.quote(s, safe='')


# ─── API: Page ────────────────────────────────────────────────────────────────

def get_page_markdown(page_id: str) -> str:
    path = f"/pages/{page_id}/markdown"
    return _request_notion_api(path)["markdown"]


def get_page_title(page_id: str) -> str:
    path = f"/pages/{page_id}"
    page = _request_notion_api(path)
    props = page.get("properties", {})
    for name, prop in props.items():
        if prop.get("type") == "title":
            title_arr = prop.get("title", [])
            return "".join(x.get("plain_text", "") for x in title_arr)
    return ""


# ─── API: View ────────────────────────────────────────────────────────────────

def list_views(database_id: str):
    path = f"/views?database_id={database_id}&page_size=100"
    return _request_notion_api(path)["results"]


def retrieve_view(view_id: str):
    path = f"/views/{view_id}"
    return _request_notion_api(path)


def create_view_query(view_id: str, page_size: int = 100) -> dict:
    path = f"/views/{view_id}/queries"
    body = {"page_size": page_size}
    return _request_notion_api(path, method="POST", body=body)


def get_view_query_results(view_id: str, query_id: str, start_cursor, page_size: int = 100) -> dict:
    path = f"/views/{view_id}/queries/{query_id}"
    params = f"?page_size={page_size}"
    if start_cursor:
        params += f"&start_cursor={_url_encode(start_cursor)}"
    return _request_notion_api(path + params)


def delete_view_query(view_id: str, query_id: str) -> dict:
    path = f"/views/{view_id}/queries/{query_id}"
    return _request_notion_api(path, method="DELETE")


def retrieve_block(block_id: str) -> dict:
    path = f"/blocks/{block_id}"
    return _request_notion_api(path)


# ─── API: Data source ─────────────────────────────────────────────────────────

_data_source_cache: dict[str, dict] = {}


def retrieve_data_source(data_source_id: str) -> dict:
    cached = _data_source_cache.get(data_source_id)
    if cached is not None:
        logger.debug("[retrieve_data_source] cache hit for %s", data_source_id)
        return cached
    path = f"/data_sources/{data_source_id}"
    ds = _request_notion_api(path)
    _data_source_cache[data_source_id] = ds
    return ds


def query_data_source(data_source_id: str, filter=None, sorts=None, filter_properties=None):
    body = {"page_size": 100}
    if filter:
        body["filter"] = filter
    if sorts:
        body["sorts"] = sorts

    path = f"/data_sources/{data_source_id}/query"
    if filter_properties:
        qs = "&".join(f"filter_properties[]={_url_encode(p)}" for p in filter_properties)
        path = f"{path}?{qs}"

    results, cursor = [], None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        data = _request_notion_api(path, method="POST", body=body)
        results.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return results


# ─── Filter helpers ────────────────────────────────────────────────────────────

def _expand_quick_filters(quick_filters: dict) -> list:
    """quick_filters: {name_or_id: condition} -> [{property: name_or_id, **condition}]"""
    if not quick_filters:
        return []
    return [{"property": k, **v} for k, v in quick_filters.items()]


def _clean_filter(node):
    """Recursively remove empty filter conditions (e.g. ``{"rich_text": {}}``).

    View filters may include property conditions with no operator set, which the
    view itself ignores but the data-source query API rejects.
    Returns None when the node becomes empty.
    """
    if not isinstance(node, dict):
        return node
    if "and" in node or "or" in node:
        key = "and" if "and" in node else "or"
        cleaned = [_clean_filter(c) for c in node[key]]
        cleaned = [c for c in cleaned if c is not None]
        if not cleaned:
            return None
        if len(cleaned) == 1:
            return cleaned[0]
        return {key: cleaned}
    if "property" in node:
        for type_key, val in node.items():
            if type_key == "property":
                continue
            if isinstance(val, dict) and len(val) == 0:
                return None
        return node
    return node


def _merge_quick_filters_and_filter(quick_filters, filter):
    """Merge quick_filters and view filter, respecting the 2-level nesting limit.

    data_source query API only supports compound filters (and/or) up to 2
    levels deep.  When the view's ``filter`` is already ``or(...)`` (which may
    itself contain nested ``and`` at level 2), wrapping it in another ``and``
    would reach level 3.  Instead, distribute the quick-filter conditions
    across each branch using Boolean algebra:

        qf AND (a OR b)  ⇒  (qf AND a) OR (qf AND b)

    When ``filter`` is ``and(...)`` or a leaf, a single ``and`` merge is safe.
    """
    qf_items = _expand_quick_filters(quick_filters)

    if not qf_items and not filter:
        return None
    if not qf_items:
        return filter  # pass view filter as-is
    if not filter:
        if len(qf_items) == 1:
            return qf_items[0]
        return {"and": qf_items}

    # Both exist — check structure
    if isinstance(filter, dict) and "or" in filter:
        # If any branch is itself a compound filter, we must distribute qf
        # across branches to stay within the 2-level nesting limit.
        # Otherwise, a simple wrap is safe.
        has_nested = any(
            isinstance(item, dict) and ("and" in item or "or" in item)
            for item in filter["or"]
        )
        if has_nested:
            branches = []
            for item in filter["or"]:
                combined = list(qf_items)
                if isinstance(item, dict) and "and" in item:
                    combined.extend(item["and"])
                else:
                    combined.append(item)
                if len(combined) == 1:
                    branches.append(combined[0])
                else:
                    branches.append({"and": combined})
            if not branches:
                return None
            if len(branches) == 1:
                return branches[0]
            return {"or": branches}
        # All leaf branches — simple wrap is fine
        qf_items.append(filter)
        if len(qf_items) == 1:
            return qf_items[0]
        return {"and": qf_items}
    elif isinstance(filter, dict) and "and" in filter:
        # Same-type: flatten into a single and
        qf_items.extend(filter["and"])
        if len(qf_items) == 1:
            return qf_items[0]
        return {"and": qf_items}
    else:
        # Leaf — wrap with qf in a single and
        qf_items.append(filter)
        if len(qf_items) == 1:
            return qf_items[0]
        return {"and": qf_items}


# ─── View config helpers ──────────────────────────────────────────────────────

def _get_filter_properties(configuration):
    """Returns ordered list of visible property names for filter_properties param.

    Returns None if configuration is absent or no visible columns.
    """
    if not configuration:
        return None
    props = configuration.get("properties", [])
    visible = [p["property_id"] for p in props if p.get("visible")]
    return visible if visible else None


# ─── Rendering ────────────────────────────────────────────────────────────────

def render_prop(prop: dict) -> str:
    t = prop["type"]
    v = prop.get(t)
    if v is None:
        return ""
    elif t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v).replace("|", "\\|")
    elif t == "number":
        return str(v)
    elif t in ("select", "status"):
        return v["name"] if v else ""
    elif t == "multi_select":
        return ", ".join(o["name"] for o in v)
    elif t == "date":
        return v["start"] + (f" → {v['end']}" if v.get("end") else "")
    elif t == "checkbox":
        return "✓" if v else ""
    elif t in ("url", "email", "phone_number"):
        return v or ""
    elif t == "people":
        return ", ".join(p.get("name", p["id"]) for p in v)
    elif t == "files":
        return ", ".join(f.get("name", "") for f in v)
    elif t == "formula":
        ft = v["type"]
        return str(v.get(ft, ""))
    elif t == "relation":
        return f"<{len(v)} relation(s)>" if v else ""
    elif t == "rollup":
        rt = v["type"]
        return "<array>" if rt == "array" else str(v.get(rt, ""))
    return str(v)


def rows_to_markdown_table(rows):
    """Render rows as a markdown table.

    Columns are taken from first row's properties keys in API response order.
    """
    if not rows:
        return "_(no rows)_"
    cols = list(rows[0]["properties"].keys())
    logger.debug("[rows_to_markdown_table] cols (%d): %s", len(cols), cols)
    if not cols:
        return "_(no columns)_"
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    lines = [header, sep]
    for row in rows:
        props = row["properties"]
        cells = []
        for name in cols:
            p = props.get(name)
            cells.append(render_prop(p) if p else "")
        lines.append("| " + " | ".join(c.replace("\n", " ") for c in cells) + " |")
    return "\n".join(lines)


# ─── Orchestration ─────────────────────────────────────────────────────────────

DB_TAG_RE = re.compile(
    r'(?P<open_tag><database\s+[^>]*(?<=\s)url="(?P<url>[^"]+)"[^>]*>)'
    r'(?P<title>.*?)'
    r'(?P<close_tag></database>)',
    re.I
)
ID_RE = re.compile(r'([0-9a-f]{32}|[0-9a-f-]{36})', re.I)


def render_view(view_id: str) -> str:
    view = retrieve_view(view_id)
    view_name = view.get("name")
    logger.info("[render_view] Rendering view '%s' (%s)", view_name or "(untitled)", view_id)

    ds_id = view.get("data_source_id")
    if not ds_id:
        return "_(view has no data source — likely a dashboard)_"

    # Fetch data source title
    ds_title = ""
    try:
        ds = retrieve_data_source(ds_id)
        title_rich = ds.get("title", [])
        ds_title = "".join(x.get("plain_text", "") for x in title_rich)
    except Exception:
        logger.warning("[render_view] Could not retrieve data source title")

    quick_filters = view.get("quick_filters")
    logger.debug("[render_view] raw quick_filters=%s", quick_filters)
    filter = view.get("filter")
    logger.debug("[render_view] raw filter=%s", filter)
    merged_filter = _merge_quick_filters_and_filter(quick_filters, filter)
    logger.debug("[render_view] merged_filter=%s", merged_filter)
    cleaned_filter = _clean_filter(merged_filter)
    logger.debug("[render_view] cleaned_filter=%s", cleaned_filter)
    sorts = view.get("sorts")

    configuration = view.get("configuration")
    logger.debug("[render_view] configuration=%s", configuration)
    filter_properties = _get_filter_properties(configuration)
    logger.debug("[render_view] filter_properties=%s", filter_properties)

    # try filter_properties optimization, fallback on 400
    if filter_properties:
        try:
            rows = query_data_source(
                ds_id,
                filter=cleaned_filter,
                sorts=sorts,
                filter_properties=filter_properties
            )
        except urllib.error.HTTPError as e:
            if e.code == 400:
                logger.warning("[render_view] filter_properties caused 400, falling back to all columns")
                rows = query_data_source(ds_id, filter=cleaned_filter, sorts=sorts)
            else:
                raise
    else:
        rows = query_data_source(ds_id, filter=merged_filter, sorts=sorts)

    # View query + intersection logic
    query_id = None
    try:
        vq = create_view_query(view_id)
        query_id = vq["id"]
        view_page_ids = [r["id"] for r in vq["results"]]
        next_cursor = vq.get("next_cursor")

        # Always paginate through all view query results for complete ordering
        while next_cursor:
            more = get_view_query_results(view_id, query_id, next_cursor)
            view_page_ids.extend(r["id"] for r in more["results"])
            next_cursor = more.get("next_cursor")

        # Intersect and reorder rows to match view query ordering
        ds_count = len(rows)
        vq_count = len(view_page_ids)
        if vq_count != ds_count:
            logger.warning("[render_view] ds_count=%d, view_count=%d — counts differ, intersecting with view query results", ds_count, vq_count)
        rows_by_id = {row["id"]: row for row in rows}
        rows = [rows_by_id[pid] for pid in view_page_ids if pid in rows_by_id]
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        logger.warning("[render_view] view query API failed (%d: %s), falling back to query_data_source results", e.code, body)
    finally:
        if query_id:
            try:
                delete_view_query(view_id, query_id)
            except Exception:
                pass

    logger.info("[render_view] Found %d row(s) for view %s", len(rows), view_id)

    table = rows_to_markdown_table(rows)

    # Build header: ## {data source title} (view id={view_id}, name={view_name})
    suffix = f"view id={view_id}"
    if view_name:
        suffix += f", name={view_name}"

    if ds_title:
        header = f"## {ds_title} ({suffix})"
    else:
        header = f"## ({suffix})"

    return f"{header}\n\n{table}" if header else table


def render_database(db_id: str) -> str:
    logger.info("[render_database] Rendering database %s", db_id)
    views = list_views(db_id)
    if not views:
        return "_(no views accessible)_"
    view_id = views[0]["id"]
    logger.info("[render_database] Found %d view(s) for database %s", len(views), db_id)
    return render_view(view_id)


def render_page(page_id: str) -> str:
    title = get_page_title(page_id)
    md = get_page_markdown(page_id)

    tags = list(DB_TAG_RE.finditer(md))
    total = len(tags)
    logger.info("[render_page] Page has %d embedded database(s)", total)
    if not tags:
        return f"# {title}\n\n{md}" if title else md

    idx = 0

    def repl(match: re.Match) -> str:
        nonlocal idx
        idx += 1
        url = match.group('url')
        m = ID_RE.search(url)
        if not m:
            logger.warning("[render_page] Could not parse database id from %s", url)
            return match.group(0)
        db_id = to_uuid(m.group(1))
        logger.info("[render_page] Expanding database %d/%d (%s)", idx, total, db_id)
        try:
            table = render_database(db_id)
            logger.info("[render_page] Finished database %d/%d (%s)", idx, total, db_id)
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8')
            logger.warning("[render_page] Database %d/%d (%s) fetch error (%d: %s), skipping", idx, total, db_id, e.code, body)
            table = f"_(error fetching database: {e.code}: {body})_"
        return "\n".join([match.group('open_tag'), table, match.group('close_tag')])

    expanded = DB_TAG_RE.sub(repl, md)
    return f"# {title}\n\n{expanded}" if title else expanded


def render_from_url(url: str) -> str:
    """
    Render a Notion URL to markdown.

    Supported URL formats:
    - View URL:    ...?v=view_uuid        -> render_view(view_id)
    - Page URL:    .../<page_uuid>        -> render_page(page_id)
    - Database URL: .../<database_uuid>   -> render_database(database_id)
    """
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Expected a Notion URL (https://...)")

    logger.info("[render_from_url] Rendering Notion URL: %s", url)

    # Check query params for view parameter (?v=...)
    qs = urllib.parse.parse_qs(parsed.query)
    view_ids = qs.get("v")
    if view_ids:
        view_id = view_ids[0]
        if not ID_RE.fullmatch(view_id):
            raise ValueError("Invalid view URL")
        logger.info("[render_from_url] View URL detected")
        return render_view(to_uuid(view_id))

    # Extract block UUID from URL path
    m = ID_RE.search(parsed.path)
    if not m:
        raise ValueError("Could not parse page/database ID from URL")

    block_id = to_uuid(m.group(1))
    block = retrieve_block(block_id)
    block_type = block.get("type")

    if block_type == "child_page":
        logger.info("[render_from_url] Page URL detected")
        return render_page(block_id)
    elif block_type == "child_database":
        logger.info("[render_from_url] Database URL detected")
        return render_database(block_id)
    else:
        raise ValueError(f"Unsupported block type '{block_type}'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="notion2md",
        description="A CLI tool to convert Notion pages, databases, or views to Markdown.",
        epilog=textwrap.dedent("""\
        environment variables:
          NOTION_API_KEY      required for authenticating with Notion API

        examples:
          export NOTION_API_KEY=ntn_xxx
          ./notion2md.py "https://notion.so/"...
          NOTION_API_KEY=$MY_KEY ./notion2md.py "https://notion.so/"...
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("notion_url", help="notion page, database, or view URL")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}", help="show version and exit")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-v", "--verbose", action="store_true", help="enable verbose (debug) output")
    group.add_argument("-q", "--quiet", action="store_true", help="suppress info output, show warnings and errors only")
    parser.add_argument("-o", "--output", metavar="FILE", help="write output to FILE instead of stdout")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO)
    logging.basicConfig(
        format="[%(asctime)s] [%(levelname).1s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
        level=log_level,
    )

    if not NOTION_API_KEY:
        logger.error("[main] Error: NOTION_API_KEY environment variable is not set")
        sys.exit(1)

    try:
        markdown = render_from_url(args.notion_url)
        logger.info("[main] Outputting markdown to %s >>>>>>>>>>", args.output or "stdout")
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(markdown)
        else:
            print(markdown)
    except ValueError as e:
        logger.error("[main] Error: %s", e)
        sys.exit(1)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        logger.error("[main] HTTPError %d: %s", e.code, body)
        sys.exit(1)
