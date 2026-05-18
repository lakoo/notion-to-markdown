import os
import re
import json
import sys
import argparse
import urllib.request
import urllib.error
import urllib.parse

NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_VERSION = "2026-03-11"
NOTION_API_BASE = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

_verbose = False

def _verbose_print(*args, **kwargs):
    if _verbose:
        print(*args, **kwargs)


# ─── HTTP Layer ────────────────────────────────────────────────────────────────

def _request(url: str, method: str = "GET", body: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=HEADERS, method=method)
    if body is not None:
        req.data = json.dumps(body).encode()
    _verbose_print(f"[_request] method={method}, url={url}, data={req.data}")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode('utf-8'))


# ─── Utilities ─────────────────────────────────────────────────────────────────

def to_uuid(raw: str) -> str:
    s = raw.replace("-", "")
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"


def _url_encode(s: str) -> str:
    """URL-encode a string (e.g. property id with special chars)."""
    return urllib.parse.quote(s, safe='')


# ─── API: Page ────────────────────────────────────────────────────────────────

def get_page_markdown(page_id: str) -> str:
    return _request(f"{NOTION_API_BASE}/pages/{page_id}/markdown")["markdown"]


def get_page_title(page_id: str) -> str:
    page = _request(f"{NOTION_API_BASE}/pages/{page_id}")
    props = page.get("properties", {})
    for name, prop in props.items():
        if prop.get("type") == "title":
            title_arr = prop.get("title", [])
            return "".join(x.get("plain_text", "") for x in title_arr)
    return ""


# ─── API: View ────────────────────────────────────────────────────────────────

def list_views(database_id: str):
    return _request(f"{NOTION_API_BASE}/views?database_id={database_id}&page_size=100")["results"]


def retrieve_view(view_id: str):
    return _request(f"{NOTION_API_BASE}/views/{view_id}")


def create_view_query(view_id: str, page_size: int = 100) -> dict:
    url = f"{NOTION_API_BASE}/views/{view_id}/queries"
    body = {"page_size": page_size}
    return _request(url, method="POST", body=body)


def get_view_query_results(view_id: str, query_id: str, start_cursor: str | None, page_size: int = 100) -> dict:
    url = f"{NOTION_API_BASE}/views/{view_id}/queries/{query_id}"
    params = f"?page_size={page_size}"
    if start_cursor:
        params += f"&start_cursor={_url_encode(start_cursor)}"
    return _request(url + params)


def delete_view_query(view_id: str, query_id: str) -> dict:
    url = f"{NOTION_API_BASE}/views/{view_id}/queries/{query_id}"
    return _request(url, method="DELETE")


# ─── API: Data source ─────────────────────────────────────────────────────────

def query_data_source(data_source_id: str, filter=None, sorts=None, filter_properties: list | None = None):
    body = {"page_size": 100}
    if filter:
        body["filter"] = filter
    if sorts:
        body["sorts"] = sorts

    url = f"{NOTION_API_BASE}/data_sources/{data_source_id}/query"
    if filter_properties:
        qs = "&".join(f"filter_properties[]={_url_encode(p)}" for p in filter_properties)
        url = f"{url}?{qs}"

    results, cursor = [], None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        data = _request(url, method="POST", body=body)
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


def _merge_quick_filters_and_filter(quick_filters: dict | None, filter: dict | None) -> dict | None:
    qf_items = _expand_quick_filters(quick_filters)
    if filter:
        qf_items.insert(0, filter)
    if not qf_items:
        return None
    if len(qf_items) == 1:
        return qf_items[0]
    return {"and": qf_items}


# ─── View config helpers ──────────────────────────────────────────────────────

def _get_filter_properties(configuration: dict | None) -> list | None:
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
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v).replace("|", "\\|")
    if t == "number":
        return "" if v is None else str(v)
    if t == "select":
        return v["name"] if v else ""
    if t == "multi_select":
        return ", ".join(o["name"] for o in v)
    if t == "status":
        return v["name"] if v else ""
    if t == "date":
        if not v:
            return ""
        return v["start"] + (f" → {v['end']}" if v.get("end") else "")
    if t == "checkbox":
        return "✓" if v else ""
    if t == "url":
        return v or ""
    if t == "email":
        return v or ""
    if t == "phone_number":
        return v or ""
    if t == "people":
        return ", ".join(p.get("name", p["id"]) for p in v)
    if t == "files":
        return ", ".join(f.get("name", "") for f in v)
    if t == "formula":
        ft = v["type"]
        return str(v.get(ft, ""))
    if t == "relation":
        if not v:
            return ""
        return f"<{len(v)} relations>"
    if t == "rollup":
        rt = v["type"]
        if rt == "array":
            return "<array>"
        return str(v.get(rt, ""))
    return str(v)


def rows_to_markdown_table(rows):
    """Render rows as a markdown table.

    Columns are taken from first row's properties keys in API response order.
    """
    if not rows:
        return "_(no rows)_"
    cols = list(rows[0]["properties"].keys())
    _verbose_print(f"[rows_to_markdown_table] cols={cols}")
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


def render_database_block(db_url: str) -> str:
    m = ID_RE.search(db_url)
    if not m:
        return f"_(could not parse database id from {db_url})_"
    db_id = to_uuid(m.group(1))

    views = list_views(db_id)
    if not views:
        return "_(no views accessible)_"
    view_id = views[0]["id"]
    view = retrieve_view(view_id)

    ds_id = view.get("data_source_id")
    if not ds_id:
        return "_(view has no data source — likely a dashboard)_"

    quick_filters = view.get("quick_filters")
    filter = view.get("filter")
    merged_filter = _merge_quick_filters_and_filter(quick_filters, filter)
    sorts = view.get("sorts")

    configuration = view.get("configuration")
    filter_properties = _get_filter_properties(configuration)
    _verbose_print(f"[render_database_block] filter_properties={filter_properties}")

    # try filter_properties optimization, fallback on 400
    _verbose_print(f"[render_database_block] calling query_data_source")
    if filter_properties:
        try:
            rows = query_data_source(
                ds_id,
                filter=merged_filter,
                sorts=sorts,
                filter_properties=filter_properties
            )
        except urllib.error.HTTPError as e:
            if e.code == 400:
                _verbose_print("[render_database_block] filter_properties caused 400, falling back to all columns")
                rows = query_data_source(ds_id, filter=merged_filter, sorts=sorts)
            else:
                raise
    else:
        rows = query_data_source(ds_id, filter=merged_filter, sorts=sorts)

    # Compare with view query result count; intersect rows if counts differ.
    # view query executes filter/sort server-side, matching web UI behavior.
    query_id = None
    try:
        vq = create_view_query(view_id)
        query_id = vq["id"]
        view_count = vq["total_count"]
        view_page_ids = [r["id"] for r in vq["results"]]
        next_cursor = vq.get("next_cursor")
        ds_count = len(rows)

        if view_count < ds_count:
            # Need full view page ids for intersection
            _verbose_print(f"[render_database_block] ds_count={ds_count}, view_count={view_count} — counts differ, intersecting {view_count} view page ids")
            while next_cursor:
                more = get_view_query_results(view_id, query_id, next_cursor)
                view_page_ids.extend(r["id"] for r in more["results"])
                next_cursor = more.get("next_cursor")
            view_id_set = set(view_page_ids)
            rows = [row for row in rows if row["id"] in view_id_set]
        else:
            _verbose_print(f"[render_database_block] ds_count={ds_count}, view_count={view_count} — counts match, using query_data_source results")
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        _verbose_print(f"[render_database_block] view query API failed ({e.code}: {body}), falling back to query_data_source results")
    finally:
        if query_id:
            try:
                delete_view_query(view_id, query_id)
            except:
                pass  # idempotent, cache auto-expires

    _verbose_print(f"[render_database_block] rows returned: {len(rows)}, first row properties: {list(rows[0]['properties'].keys()) if rows else 'none'}")

    return rows_to_markdown_table(rows)


def expand_databases(md: str) -> str:
    def repl(match: re.Match) -> str:
        url = match.group('url')
        title = match.group('title').strip()
        try:
            table = render_database_block(url)
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8')
            table = f"_(error fetching database: {e.code}: {body})_"

        lines = [match.group('open_tag')]
        if title:
            lines.append(f"## {title}")
        lines.append(table)
        lines.append(match.group('close_tag'))
        return "\n".join(lines)

    return DB_TAG_RE.sub(repl, md)


if __name__ == "__main__":
    if not NOTION_API_KEY:
        print("Error: NOTION_API_KEY environment variable is not set", file=sys.stderr)
        sys.exit(1)
    parser = argparse.ArgumentParser(prog="notion2md")
    parser.add_argument("page_id", help="Notion page ID (URL, UUID with dashes, or compact UUID)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    parser.add_argument("--output", "-o", metavar="FILE", help="Write output to FILE instead of stdout")
    args = parser.parse_args()

    _verbose = args.verbose
    m = ID_RE.search(args.page_id)
    if not m:
        print(f"Invalid page ID: {args.page_id}", file=sys.stderr)
        sys.exit(1)
    page_id = to_uuid(m.group(1))
    try:
        title = get_page_title(page_id)
        md = get_page_markdown(page_id)
        expanded = expand_databases(md)
        output = f"# {title}\n\n{expanded}" if title else expanded
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
        else:
            print(output)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        print(f"HTTPError {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
