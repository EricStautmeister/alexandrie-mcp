#!/usr/bin/env python3
"""
Alexandrie MCP Server

Exposes Alexandrie documentation as an MCP (Model Context Protocol) server
with SSE transport for network access.

Tools exposed:
  - list_nodes        list workspace/category children (or every node, flat)
  - get_tree          outline of the folder tree with ids
  - get_document      fetch a document's content
  - search_documents  full-text search
  - create_document   create a new document
  - update_document   update an existing document's content or name
  - replace_in_document  replace one passage of a document without resending it
  - sort_children     set the sidebar order of a folder's children
  - create_category   create a new folder/category
  - delete_node       delete a node (requires confirm=true)
  - compile_documents compile any documents whose content_compiled is missing

content_compiled (the HTML the UI renders) is produced here with the frontend's own
Markdown compiler (compile/), on every create/update, at startup and periodically.

The server logs in to Alexandrie on startup, refreshes the session every 10 minutes
and re-logs in transparently on 401/403 (the backend session expires after < 1 h).
"""

import os
import json
import time
import threading
import subprocess
import requests
import logging
from typing import Optional
from mcp.server.mcpserver import MCPServer

# ── Config (overridden by env vars) ──────────────────────────────────────────

ALEXANDRIE_API = os.environ.get("ALEXANDRIE_API", "http://alexandrie-backend:8201/api")
def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} must be set (no default is provided)")
    return value


USERNAME       = _required("ALEXANDRIE_USER")
PASSWORD       = _required("ALEXANDRIE_PASS")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alexandrie-mcp")

# ── Alexandrie session ────────────────────────────────────────────────────────

class AlexandrieSession:
    """Logged-in HTTP session to the Alexandrie backend.

    The backend session expires after well under an hour. Every request therefore
    re-logs in and retries once when it gets 401/403, and a background thread
    refreshes the session every REFRESH_SECONDS.
    """

    REFRESH_SECONDS = int(os.environ.get("ALEXANDRIE_REFRESH_SECONDS", "600"))

    def __init__(self):
        self.session = requests.Session()
        # Some deployments set the session cookie with Secure and a Domain attribute,
        # but this server talks plain http to alexandrie-backend: a cookie jar would drop or withhold it.
        # So the Set-Cookie values are kept here and sent as an explicit Cookie header.
        self.cookies: dict = {}
        self.user_id: Optional[str] = None
        self._lock = threading.RLock()
        self._login()
        self._schedule_refresh()

    def _store_cookies(self, r: requests.Response):
        import http.cookies
        for h in r.raw.headers.getlist("Set-Cookie"):
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(h)
            except http.cookies.CookieError:
                continue
            for k, m in jar.items():
                if m.value:
                    self.cookies[k] = m.value
                else:
                    self.cookies.pop(k, None)

    def _cookie_header(self) -> dict:
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())} if self.cookies else {}

    def _login(self):
        with self._lock:
            for attempt in range(10):
                try:
                    self.session = requests.Session()   # drop stale state
                    self.cookies = {}
                    r = self.session.post(
                        f"{ALEXANDRIE_API}/auth",
                        data={"username": USERNAME, "password": PASSWORD},
                        timeout=10,
                    )
                    if r.status_code == 200:
                        self._store_cookies(r)
                        data = r.json()
                        user = data.get("result", data.get("data", data))
                        self.user_id = str(user.get("id", ""))
                        log.info("Logged in to Alexandrie (user_id=%s)", self.user_id)
                        return
                    log.warning("Login attempt %d failed: %d", attempt + 1, r.status_code)
                except Exception as e:
                    log.warning("Login attempt %d exception: %s", attempt + 1, e)
                time.sleep(5)
            raise RuntimeError("Could not log in to Alexandrie after 10 attempts")

    def _schedule_refresh(self):
        def refresh():
            while True:
                time.sleep(self.REFRESH_SECONDS)
                try:
                    with self._lock:
                        r = self.session.post(f"{ALEXANDRIE_API}/auth/refresh", headers=self._cookie_header(), timeout=10)
                        if r.status_code != 200:
                            log.info("Session refresh returned %d, re-logging in", r.status_code)
                            self._login()
                        else:
                            self._store_cookies(r)
                except Exception as e:
                    log.warning("Refresh error: %s", e)
        threading.Thread(target=refresh, daemon=True).start()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        with self._lock:
            headers = {**kwargs.pop("headers", {}), **self._cookie_header()}
            r = self.session.request(method, f"{ALEXANDRIE_API}{path}", headers=headers, timeout=15, **kwargs)
            if r.status_code in (401, 403):
                log.info("%s %s -> %d, re-logging in and retrying once", method, path, r.status_code)
                self._login()
                headers = {**headers, **self._cookie_header()}
                r = self.session.request(method, f"{ALEXANDRIE_API}{path}", headers=headers, timeout=15, **kwargs)
            elif r.raw.headers.getlist("Set-Cookie"):
                self._store_cookies(r)
            return r

    def get(self, path: str, **kwargs):
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs):
        return self._request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self._request("DELETE", path, **kwargs)


# ── Bootstrap ─────────────────────────────────────────────────────────────────

log.info("Connecting to Alexandrie at %s ...", ALEXANDRIE_API)
api = AlexandrieSession()

# ── Markdown compilation ──────────────────────────────────────────────────────
# Alexandrie renders content_compiled in the browser; documents written through the API
# would otherwise have none. compile/compile.mjs is the frontend's own compiler.

COMPILE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compile", "compile.mjs")
COMPILE_INTERVAL = int(os.environ.get("ALEXANDRIE_COMPILE_INTERVAL", "300"))


def compile_markdown(contents: list) -> list:
    """Compile a list of Markdown strings to HTML (same output as the frontend)."""
    if not contents:
        return []
    p = subprocess.run(
        ["node", COMPILE_SCRIPT],
        input=json.dumps(contents), capture_output=True, text=True, timeout=120,
    )
    if p.returncode != 0:
        raise RuntimeError(f"Markdown compile failed: {p.stderr[:300]}")
    return json.loads(p.stdout)


_compile_lock = threading.Lock()
_READ_ONLY = {"id", "created_timestamp", "updated_timestamp", "content_compiled", "size", "permissions", "children"}


def compile_missing() -> list:
    """Compile every document that has content but no content_compiled. Returns [(id, name)]."""
    done = []
    with _compile_lock:
        listing = _unwrap(api.get(f"/nodes/user/{api.user_id}"))
        for n in (listing if isinstance(listing, list) else []):
            if n.get("role") != 3:
                continue
            # The list endpoint blanks content/content_compiled, so read each document itself.
            data = _unwrap(api.get(f"/nodes/{n['id']}"))
            node = data.get("node", data)
            content = node.get("content") or ""
            if not content.strip() or (node.get("content_compiled") or "").strip():
                continue
            html = compile_markdown([content])[0]
            payload = {k: v for k, v in node.items() if k not in _READ_ONLY and v is not None}
            payload["content_compiled"] = html
            _unwrap(api.put(f"/nodes/{node['id']}", json=payload))
            log.info("Compiled document id=%s name='%s'", node["id"], node.get("name"))
            done.append((str(node["id"]), node.get("name")))
    return done


def _compile_loop():
    while True:
        try:
            compile_missing()
        except Exception as e:
            log.warning("Background compile failed: %s", e)
        time.sleep(COMPILE_INTERVAL)


# Optional public documentation (ALEXANDRIE_AUTO_PUBLISH=1): nodes get PUBLISH_ACCESSIBILITY on creation and the
# loop below also catches nodes made in the UI or imported. Off by default: enabling it makes EVERY page public.
AUTO_PUBLISH = os.environ.get("ALEXANDRIE_AUTO_PUBLISH", "0") == "1"


def _new_node_access() -> dict:
    """Accessibility for newly created nodes: only set (public) when auto-publish is on; otherwise Alexandrie's own default applies."""
    return {"accessibility": PUBLISH_ACCESSIBILITY} if AUTO_PUBLISH else {}
PUBLISH_ACCESSIBILITY = int(os.environ.get("ALEXANDRIE_PUBLISH_ACCESSIBILITY", "3"))
PUBLISH_INTERVAL = int(os.environ.get("ALEXANDRIE_PUBLISH_INTERVAL", "120"))
_publish_lock = threading.Lock()


def publish_missing() -> list:
    """Set accessibility to PUBLISH_ACCESSIBILITY on every node that has another value. Returns [(id, name)]."""
    done = []
    with _publish_lock:
        listing = _unwrap(api.get(f"/nodes/user/{api.user_id}"))
        for n in (listing if isinstance(listing, list) else []):
            if n.get("accessibility") == PUBLISH_ACCESSIBILITY:
                continue
            data = _unwrap(api.get(f"/nodes/{n['id']}"))
            node = data.get("node", data)
            payload = {k: v for k, v in node.items() if k not in _READ_ONLY and v is not None}
            if (node.get("content_compiled") or "").strip():
                payload["content_compiled"] = node["content_compiled"]  # the PUT overwrites it
            payload["accessibility"] = PUBLISH_ACCESSIBILITY
            _unwrap(api.put(f"/nodes/{node['id']}", json=payload))
            log.info("Published node id=%s name='%s'", node["id"], node.get("name"))
            done.append((str(node["id"]), node.get("name")))
    return done


def _publish_loop():
    while True:
        try:
            publish_missing()
            if PORTAL_PARENT:
                update_portal()
        except Exception as e:
            log.warning("Background publish failed: %s", e)
        time.sleep(PUBLISH_INTERVAL)


# Portal page: one generated index of every node with public /doc/<id> links, so readers without a login
# can browse the whole tree. Rebuilt on every pass; written only when the text changed.
PORTAL_NAME = "🏠 Documentation portal"
PORTAL_PARENT = os.environ.get("ALEXANDRIE_PORTAL_PARENT", "")  # workspace id; the portal page is only maintained when set
_ROLE_ICON = {1: "📚", 2: "📁", 3: "📄"}

# Pinned entry points shown at the top of the portal (node ids; missing ids are skipped, names and
# descriptions always come from the live nodes, so renames follow automatically).
def _id_list(name: str) -> list:
    return [i.strip() for i in os.environ.get(name, "").split(",") if i.strip()]


PORTAL_START = _id_list("ALEXANDRIE_PORTAL_START")          # comma-separated node ids
PORTAL_OVERVIEWS = _id_list("ALEXANDRIE_PORTAL_OVERVIEWS")  # comma-separated node ids


def _portal_markdown(nodes: list, portal_id: Optional[str]) -> str:
    kids: dict = {}
    for n in nodes:
        if str(n["id"]) == portal_id:
            continue
        kids.setdefault(str(n["parent_id"]) if n.get("parent_id") else "", []).append(n)
    for lst in kids.values():
        lst.sort(key=lambda n: (n.get("role") == 3, n.get("order") if n.get("order") is not None else 10**6,
                                (n.get("name") or "").lower()))
    by_id = {str(n["id"]): n for n in nodes}

    def clean(n: dict, limit: int = 150) -> tuple:
        name = (n.get("name") or "Untitled").replace("[", "(").replace("]", ")").replace("|", "/")
        desc = (n.get("description") or "").strip().replace("\n", " ").replace("|", "/")
        return name, (desc[:limit - 1] + "…" if len(desc) > limit else desc)

    def table(title: str, ids: list) -> list:
        rows = [(by_id[i]) for i in ids if i in by_id]
        if not rows:
            return []
        out = [f"## {title}", "", "| Page | What it is |", "|---|---|"]
        for n in rows:
            name, desc = clean(n)
            out.append(f"| 📄 [{name}](/doc/{n['id']}) | {desc} |")
        return out + [""]

    def day(ms) -> str:
        try:
            return time.strftime("%Y-%m-%d", time.gmtime(int(ms) / 1000))
        except (TypeError, ValueError):
            return ""

    docs = [n for n in nodes if n.get("role") == 3 and str(n["id"]) != portal_id]
    folders = sorted((n for n in nodes if n.get("role") in (1, 2) and not n.get("parent_id")),
                     key=lambda n: (n.get("order") if n.get("order") is not None else 10**6, (n.get("name") or "").lower()))
    recent = sorted((n for n in docs if n.get("updated_timestamp")), key=lambda n: -int(n["updated_timestamp"]))[:10]

    lines = ["# 🏠 Documentation portal", "",
             f"**{len(docs)} pages**, all readable without a login. "
             "Start with the pages below; the full folder tree follows at the end. "
             "This page is generated automatically and rebuilt every few minutes.", ""]
    lines += table("🚀 Start here", PORTAL_START)
    lines += table("🧭 Overviews", PORTAL_OVERVIEWS)
    if recent:
        lines += ["## 🕘 Recently updated", ""]
        for n in recent:
            name, _ = clean(n)
            lines.append(f"- {day(n['updated_timestamp'])} — [{name}](/doc/{n['id']})")
        lines.append("")
    if folders:
        lines += ["## 📚 All pages by section", "",
                  "Sections: " + " · ".join(clean(f)[0] for f in folders), ""]

    def walk(parent: str, depth: int):
        for n in kids.get(parent, []):
            name = (n.get("name") or "Untitled").replace("[", "(").replace("]", ")")
            desc = (n.get("description") or "").strip().replace("\n", " ")
            if len(desc) > 140:
                desc = desc[:137] + "…"
            tail = f" — {desc}" if desc else ""
            if n.get("role") == 3:
                lines.append(f"{'  ' * depth}- 📄 [{name}](/doc/{n['id']}){tail}")
            else:
                lines.append(f"{'  ' * depth}- **{_ROLE_ICON.get(n.get('role'), '📁')} {name}**")
                walk(str(n["id"]), depth + 1)

    walk("", 0)
    return "\n".join(lines) + "\n"


def update_portal() -> bool:
    """Create or refresh the portal page. Returns True when it was written."""
    with _publish_lock:
        nodes = _unwrap(api.get(f"/nodes/user/{api.user_id}"))
        nodes = nodes if isinstance(nodes, list) else []
        existing = next((n for n in nodes if n.get("name") == PORTAL_NAME and n.get("role") == 3), None)
        md = _portal_markdown(nodes, str(existing["id"]) if existing else None)
        if existing is None:
            payload = {"name": PORTAL_NAME, "role": 3, "content": md, "content_compiled": compile_markdown([md])[0],
                       "accessibility": PUBLISH_ACCESSIBILITY, "parent_id": PORTAL_PARENT,
                       "description": "Generated index of every page with public links. Rebuilt automatically; do not edit.",
                       "tags": "portal,index,generated"}
            node = _unwrap(api.post("/nodes", json=payload))
            log.info("Created portal page id=%s", node.get("id"))
            return True
        data = _unwrap(api.get(f"/nodes/{existing['id']}"))
        node = data.get("node", data)
        if (node.get("content") or "") == md:
            return False
        payload = {k: v for k, v in node.items() if k not in _READ_ONLY and v is not None}
        payload["content"] = md
        payload["content_compiled"] = compile_markdown([md])[0]
        payload["accessibility"] = PUBLISH_ACCESSIBILITY
        _unwrap(api.put(f"/nodes/{node['id']}", json=payload))
        log.info("Updated portal page id=%s", node["id"])
        return True


# ── MCP Server ────────────────────────────────────────────────────────────────

mcp = MCPServer(
    "Alexandrie Docs",
    instructions=(
        "This server provides read/write access to the Alexandrie documentation knowledge base. "
        "Use list_nodes to explore the structure, get_document to read content, "
        "search_documents for full-text search, create_document / update_document to write, "
        "and delete_node to permanently remove a node. "
        "Node roles: 1=Workspace (top-level), 2=Category (folder), 3=Document (content). "
        "delete_node requires confirm=True and deletes recursively — use with care. "
        "Every node is PUBLISHED (readable without login on the public documentation site): "
        "never write credentials, tokens or private data into any page."
    ),
)


def _unwrap(r: requests.Response) -> dict:
    """Unwrap Alexandrie's { result: ... } response envelope; raise a readable error."""
    if r.status_code >= 400:
        raise RuntimeError(f"Alexandrie API {r.request.method} {r.url} -> {r.status_code}: {r.text[:300]}")
    body = r.json()
    return body.get("result", body.get("data", body))


def _summary(node: dict) -> dict:
    """Node without its (potentially huge) content and compiled HTML, for write responses."""
    out = {k: v for k, v in node.items() if k not in ("content", "content_compiled")}
    out["content_length"] = len(node.get("content") or "")
    out["compiled"] = bool((node.get("content_compiled") or "").strip())
    return out


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_nodes(parent_id: Optional[str] = None) -> str:
    """
    List nodes (workspaces, categories, documents) in Alexandrie.

    Args:
        parent_id: ID of the parent node to list the direct children of.
                   Omit to list every node of the current user (flat, each with its parent_id).

    Returns:
        JSON array of node summaries with fields:
        id, name, role (1=workspace, 2=category, 3=document), parent_id,
        created_timestamp, updated_timestamp.
    """
    nodes = _all_nodes()
    if parent_id:
        nodes = [n for n in nodes if str(n.get("parent_id")) == str(parent_id)]
    summary = [
        {
            "id": n.get("id"),
            "name": n.get("name"),
            "role": n.get("role"),
            "parent_id": n.get("parent_id"),
            "updated_timestamp": n.get("updated_timestamp"),
        }
        for n in nodes
    ]
    return json.dumps(summary, indent=2)


def _all_nodes() -> list:
    """Every node of the current user (flat, content blanked by the backend)."""
    nodes = _unwrap(api.get(f"/nodes/user/{api.user_id}"))
    return nodes if isinstance(nodes, list) else []


@mcp.tool()
def get_tree(parent_id: Optional[str] = None, max_depth: int = 3) -> str:
    """
    Outline of the folder tree as indented text: "name [id] (C=category, D=document, W=workspace)".

    Args:
        parent_id: Start below this node. Omit to start at the top-level nodes.
        max_depth: How many levels to show (default 3, max 8).

    Returns:
        Plain-text outline; the quickest way to see where a page lives.
    """
    max_depth = max(1, min(int(max_depth), 8))
    nodes = _all_nodes()
    ids = {str(n.get("id")) for n in nodes}
    kids: dict = {}
    for n in nodes:
        p = n.get("parent_id")
        key = str(p) if p is not None and str(p) in ids else None
        kids.setdefault(key, []).append(n)
    for lst in kids.values():
        # categories first, then documents, each by name
        lst.sort(key=lambda n: (n.get("role") == 3, (n.get("name") or "").lower()))
    label = {1: "W", 2: "C", 3: "D"}
    lines: list = []

    def walk(key, depth):
        for n in kids.get(key, []):
            lines.append(f"{'  ' * depth}{n.get('name')} [{n.get('id')}] ({label.get(n.get('role'), '?')})")
            if depth + 1 < max_depth:
                walk(str(n.get("id")), depth + 1)

    walk(str(parent_id) if parent_id else None, 0)
    return "\n".join(lines) if lines else "(no children)"


@mcp.tool()
def get_document(node_id: str) -> str:
    """
    Retrieve the full content of a document node by its ID.

    Args:
        node_id: The ID of the document node.

    Returns:
        JSON object with fields: id, name, role, parent_id, content (Markdown),
        tags, description, created_timestamp, updated_timestamp.
        Returns content_compiled (HTML) as well if available.
    """
    r = api.get(f"/nodes/{node_id}")
    data = _unwrap(r)
    node = data.get("node", data)
    return json.dumps({
        "id":                node.get("id"),
        "name":              node.get("name"),
        "role":              node.get("role"),
        "parent_id":         node.get("parent_id"),
        "tags":              node.get("tags"),
        "description":       node.get("description"),
        "content":           node.get("content"),
        "created_timestamp": node.get("created_timestamp"),
        "updated_timestamp": node.get("updated_timestamp"),
    }, indent=2)


@mcp.tool()
def search_documents(query: str) -> str:
    """
    Full-text search across all documents in Alexandrie.

    Args:
        query: Search string.

    Returns:
        JSON array of matching documents with id, name, role, relevance score,
        and a short content snippet.
    """
    r = api.get("/nodes/search", params={"q": query})
    results = _unwrap(r)
    return json.dumps(results if isinstance(results, list) else [], indent=2)


@mcp.tool()
def create_document(
    name: str,
    content: str,
    parent_id: Optional[str] = None,
    tags: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """
    Create a new document in Alexandrie.

    Args:
        name:        Title of the document (max 50 characters).
        content:     Document body in Markdown.
        parent_id:   ID of the parent category or workspace.
                     If omitted the document will be created at the root level.
        tags:        Comma-separated tags string (optional).
        description: Short description (max 250 characters, optional).

    Returns:
        JSON summary of the new node including its assigned ID (content and compiled HTML omitted).
    """
    payload: dict = {
        "name":          name[:50],
        "role":          3,          # Document
        "content":       content,
        "content_compiled": compile_markdown([content])[0],
        **_new_node_access(),
    }
    if parent_id:
        payload["parent_id"] = parent_id
    if tags:
        payload["tags"] = tags[:250]
    if description:
        payload["description"] = description[:250]

    r = api.post("/nodes", json=payload)
    node = _unwrap(r)
    log.info("Created document '%s' (id=%s)", name, node.get("id"))
    return json.dumps(_summary(node), indent=2)


def _update_document(
    node_id: str,
    content: Optional[str] = None,
    name: Optional[str] = None,
    tags: Optional[str] = None,
    description: Optional[str] = None,
    parent_id: Optional[str] = None,
    order: Optional[int] = None,
) -> str:
    # Fetch current node first so we only overwrite what is provided
    r = api.get(f"/nodes/{node_id}")
    data = _unwrap(r)
    current = data.get("node", data)

    # Start from the full current node so nothing is lost (parent_id was dropped before,
    # which moved every updated page to the root), then override only what was given.
    read_only = {"id", "created_timestamp", "updated_timestamp", "content_compiled", "size", "permissions", "children"}
    payload = {k: v for k, v in current.items() if k not in read_only and v is not None}
    if name:
        payload["name"] = name[:50]
    if content is not None:
        payload["content"] = content
        payload["content_compiled"] = compile_markdown([content])[0]
    elif (current.get("content_compiled") or "").strip():
        # The PUT overwrites content_compiled, so carry the existing HTML over.
        payload["content_compiled"] = current["content_compiled"]
    elif (current.get("content") or "").strip():
        payload["content_compiled"] = compile_markdown([current["content"]])[0]
    if tags:
        payload["tags"] = tags[:250]
    if description:
        payload["description"] = description[:250]
    if parent_id:
        payload["parent_id"] = parent_id
    if order is not None:
        payload["order"] = int(order)
    payload.setdefault("role", 3)
    for _k, _v in _new_node_access().items():
        payload.setdefault(_k, _v)

    r = api.put(f"/nodes/{node_id}", json=payload)
    node = _unwrap(r)
    log.info("Updated document id=%s", node_id)
    return json.dumps(_summary(node), indent=2)


@mcp.tool()
def update_document(
    node_id: str,
    content: Optional[str] = None,
    name: Optional[str] = None,
    tags: Optional[str] = None,
    description: Optional[str] = None,
    parent_id: Optional[str] = None,
    order: Optional[int] = None,
) -> str:
    """
    Update an existing document's content and/or metadata, or move it.
    The whole content is replaced - for a small change use replace_in_document.

    Args:
        node_id:     ID of the document to update.
        content:     New Markdown content (omit to leave unchanged).
        name:        New title (omit to leave unchanged, max 50 chars).
        tags:        New comma-separated tags string (omit to leave unchanged).
        description: New description (omit to leave unchanged, max 250 chars).
        parent_id:   Move the document into this category/workspace (omit to keep its place).
        order:       Sort position among siblings in the sidebar: lower first, unset counts as 0.
                     -1 PINS the node on the dashboard - avoid. Prefer sort_children.

    Returns:
        JSON summary of the updated node (content and compiled HTML omitted).
    """
    return _update_document(node_id, content, name, tags, description, parent_id, order)


@mcp.tool()
def sort_children(parent_id: str, ordered_ids: list[str]) -> str:
    """
    Put the children of a folder (or workspace) in a given order in the sidebar.

    Sets `order` = 1, 2, 3 ... on the listed nodes, in the list's sequence. Nodes of a folder
    that are not listed keep their value (unset counts as 0, so they sort BEFORE the listed
    ones - list every child you care about). Documents are always shown before folders
    regardless of order. Does not change content, name or place.

    Args:
        parent_id:   ID of the folder/workspace whose children are sorted.
        ordered_ids: Child node IDs, first = top of the list. Every ID must be a direct child
                     of parent_id, otherwise nothing is changed.

    Returns:
        JSON list of {id, name, order} as written.
    """
    wanted = [str(i) for i in ordered_ids]
    if len(set(wanted)) != len(wanted):
        raise ValueError("ordered_ids contains duplicates")
    children = {str(n.get("id")): n for n in _all_nodes() if str(n.get("parent_id")) == str(parent_id)}
    foreign = [i for i in wanted if i not in children]
    if foreign:
        raise ValueError(f"not direct children of {parent_id}: {foreign}")
    out = []
    for pos, nid in enumerate(wanted, start=1):
        node = json.loads(_update_document(nid, order=pos))
        out.append({"id": nid, "name": node.get("name"), "order": node.get("order")})
    log.info("Sorted %d children of %s", len(out), parent_id)
    return json.dumps(out, indent=2)


@mcp.tool()
def replace_in_document(
    node_id: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """
    Replace a passage in a document's Markdown without resending the whole page.

    Fails (and changes nothing) when old_string is empty, not found, or occurs more
    than once while replace_all is False. Metadata and place are kept.

    Args:
        node_id:     ID of the document.
        old_string:  Exact text to replace (copy it from get_document; include enough
                     context to be unique).
        new_string:  Replacement text (may be empty to delete the passage).
        replace_all: Replace every occurrence instead of requiring a unique match.

    Returns:
        JSON summary of the updated node plus the number of replacements.
    """
    if not old_string:
        raise ValueError("old_string must not be empty")
    if old_string == new_string:
        raise ValueError("old_string and new_string are identical")
    data = _unwrap(api.get(f"/nodes/{node_id}"))
    current = data.get("node", data)
    if current.get("role") != 3:
        raise ValueError(f"node {node_id} is not a document")
    text = current.get("content") or ""
    count = text.count(old_string)
    if count == 0:
        raise ValueError("old_string not found in the document (check whitespace; copy it from get_document)")
    if count > 1 and not replace_all:
        raise ValueError(f"old_string occurs {count} times; add more context or pass replace_all=true")
    updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    result = json.loads(_update_document(node_id, content=updated))
    result["replacements"] = count if replace_all else 1
    return json.dumps(result, indent=2)


@mcp.tool()
def create_category(
    name: str,
    parent_id: Optional[str] = None,
    role: int = 2,
) -> str:
    """
    Create a new category (folder) or workspace in Alexandrie.

    Args:
        name:      Name of the category (max 50 characters).
        parent_id: ID of the parent workspace or category.
                   Omit to create a top-level workspace.
        role:      2 = Category (folder inside a workspace, default).
                   1 = Workspace (top-level container, use when parent_id is omitted).

    Returns:
        JSON object of the newly created node including its ID.
    """
    payload: dict = {
        "name":          name[:50],
        "role":          role,
        **_new_node_access(),
    }
    if parent_id:
        payload["parent_id"] = parent_id

    r = api.post("/nodes", json=payload)
    node = _unwrap(r)
    log.info("Created category '%s' (id=%s, role=%d)", name, node.get("id"), role)
    return json.dumps(node, indent=2)


@mcp.tool()
def delete_node(node_id: str, confirm: bool = False) -> str:
    """
    Permanently delete a node (document or category) and all its children.

    This action is IRREVERSIBLE. A category delete removes every document nested
    inside it. Always call get_document or list_nodes first to confirm what will
    be removed.

    Args:
        node_id: ID of the node to delete.
        confirm: Must be set to True to actually perform the deletion.
                 When False (default) the tool returns a dry-run summary
                 showing the node name and role without deleting anything.

    Returns:
        On confirm=False: JSON summary of the node that would be deleted.
        On confirm=True:  JSON with {"deleted": true, "id": node_id, "name": ...}.
    """
    # Always fetch the node first so we can show what would be deleted
    r = api.get(f"/nodes/{node_id}")
    data = _unwrap(r)
    node = data.get("node", data)
    name = node.get("name", node_id)
    role = node.get("role")
    role_label = {1: "Workspace", 2: "Category", 3: "Document"}.get(role, str(role))

    if not confirm:
        return json.dumps({
            "dry_run": True,
            "id": node_id,
            "name": name,
            "role": role_label,
            "message": (
                f"Would delete {role_label} '{name}' (id={node_id}) and all its children. "
                "Call again with confirm=True to proceed."
            ),
        }, indent=2)

    r = api.delete(f"/nodes/{node_id}")
    if r.status_code not in (200, 204):
        raise RuntimeError(
            f"Alexandrie DELETE /nodes/{node_id} returned {r.status_code}: {r.text[:300]}"
        )
    log.info("Deleted node id=%s name='%s' role=%s", node_id, name, role_label)
    return json.dumps({"deleted": True, "id": node_id, "name": name, "role": role_label}, indent=2)


@mcp.tool()
def compile_documents() -> str:
    """
    Compile every document whose compiled HTML (content_compiled) is missing.

    This also runs automatically at startup and every few minutes; call it to force a pass
    right after pages were added by other means (UI import, direct DB inserts).

    Returns:
        JSON object with the number and list of documents compiled.
    """
    done = compile_missing()
    return json.dumps({"compiled": len(done), "documents": [{"id": i, "name": n} for i, n in done]}, indent=2)


@mcp.tool()
def publish_documents() -> str:
    """
    Publish every node (set accessibility to the public value) that is not published yet.

    When ALEXANDRIE_AUTO_PUBLISH=1, new pages created through this server are published
    and a background pass runs every few minutes.
    Call this to force a pass right after pages were added by other means (UI, import).

    Returns:
        JSON object with the number and list of nodes published.
    """
    done = publish_missing()
    portal = update_portal()
    return json.dumps({"published": len(done), "portal_updated": portal,
                       "nodes": [{"id": i, "name": n} for i, n in done]}, indent=2)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("MCP_PORT", "8202"))
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    log.info("Starting Alexandrie MCP server on %s:%d (SSE transport)", host, port)
    threading.Thread(target=_compile_loop, daemon=True, name="compile-loop").start()
    if AUTO_PUBLISH:
        threading.Thread(target=_publish_loop, daemon=True, name="publish-loop").start()
    mcp.run(transport="sse", host=host, port=port)
