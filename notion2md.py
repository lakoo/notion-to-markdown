import os
import re
import json
import urllib.request
import urllib.error

NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_VERSION = "2026-03-11"
BASE = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS, method="GET")
    try:
        r = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        raise urllib.error.HTTPError(
            url, e.code, e.msg, dict(e.headers), None
        ) from e
    return json.loads(r.read())


def _post(url: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=HEADERS, method="POST")
    if body is not None:
        req.data = json.dumps(body).encode()
    try:
        r = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        raise urllib.error.HTTPError(
            url, e.code, e.msg, dict(e.headers), None
        ) from e
    return json.loads(r.read())


DB_TAG_RE = re.compile(r'<database\s+[^>]*url="([^"]+)"[^>]*>\s*</database>', re.I)
ID_RE = re.compile(r'([0-9a-f]{32}|[0-9a-f-]{36})', re.I)


def to_uuid(raw: str) -> str:
    s = raw.replace("-", "")
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"


def get_page_markdown(page_id: str) -> str:
    return _get(f"{BASE}/pages/{page_id}/markdown")["markdown"]


def list_views(database_id: str):
    url = f"{BASE}/views?database_id={database_id}&page_size=100"
    return _get(url)["results"]


def retrieve_view(view_id: str):
    return _get(f"{BASE}/views/{view_id}")


def query_data_source(data_source_id: str, filter_obj=None, sorts=None):
    body = {"page_size": 100}
    if filter_obj:
        body["filter"] = filter_obj
    if sorts:
        body["sorts"] = sorts
    results, cursor = [], None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        data = _post(f"{BASE}/data_sources/{data_source_id}/query", body=body)
        results.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return results


def get_data_source_schema(data_source_id: str):
    return _get(f"{BASE}/data_sources/{data_source_id}")["properties"]


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
        if not v: return ""
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
        return ", ".join(r["id"] for r in v)
    if t == "rollup":
        rt = v["type"]
        return str(v.get(rt, ""))
    return str(v)


def rows_to_markdown_table(rows, schema, visible_prop_ids=None):
    # keep schema order; optionally filter to visible_prop_ids
    cols = [(name, meta) for name, meta in schema.items()
            if not visible_prop_ids or meta["id"] in visible_prop_ids]
    if not cols:
        return "_(no columns)_"
    header = "| " + " | ".join(n for n, _ in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    lines = [header, sep]
    for row in rows:
        props = row["properties"]
        cells = []
        for name, _ in cols:
            p = props.get(name)
            cells.append(render_prop(p) if p else "")
        lines.append("| " + " | ".join(c.replace("\n", " ") for c in cells) + " |")
    return "\n".join(lines)


def render_database_block(db_url: str) -> str:
    m = ID_RE.search(db_url)
    if not m:
        return f"_(could not parse database id from {db_url})_"
    db_id = to_uuid(m.group(1))

    views = list_views(db_id)
    if not views:
        return "_(no views accessible)_"
    view = retrieve_view(views[0]["id"])

    ds_id = view.get("data_source_id")
    if not ds_id:
        return "_(view has no data source — likely a dashboard)_"

    filter_obj = view.get("filter")
    sorts = view.get("sorts")
    schema = get_data_source_schema(ds_id)
    rows = query_data_source(ds_id, filter_obj=filter_obj, sorts=sorts)

    # `filter` field already encodes which properties drive filtering;
    # for "filtered properties" displayed in the view, the public View
    # object does not currently expose a visible-columns list, so we
    # render the full schema. Restrict here if you maintain your own list.
    return rows_to_markdown_table(rows, schema)


def expand_databases(md: str) -> str:
    def repl(match: re.Match) -> str:
        url = match.group(1)
        try:
            table = render_database_block(url)
        except urllib.error.HTTPError as e:
            table = f"_(error fetching database: {e.code})_"
        return f"{match.group(0)}\n\n{table}\n"

    return DB_TAG_RE.sub(repl, md)


if __name__ == "__main__":
    import sys
    page_id = sys.argv[1]
    md = get_page_markdown(page_id)
    print(expand_databases(md))
