#!/usr/bin/env python3
"""
chrome.net.ua API Sandbox
=========================
A small self-hosted testing API: a real **SQLite-backed CRUD** resource
("items") plus version/health, with interactive Swagger docs.

Runs behind nginx at https://chrome.net.ua/api/ (root_path=/api), bound to
127.0.0.1:8092. Reads are open; writes require an `X-API-Key`.

Stdlib + FastAPI/Uvicorn only. Persistence is stdlib SQLite in a single file
(API_DB, default ./data.db).
"""
import os
import time
import hmac
import hashlib
import logging
import sqlite3
import threading
import json as jsonlib
from typing import Optional, List
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request, Response, HTTPException, Path, Query, Depends, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.security import APIKeyHeader
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field, field_validator

# ---------- config ----------
ROOT_PATH = os.environ.get("API_ROOT_PATH", "")          # "/api" in production
DB_PATH = os.environ.get("API_DB", os.path.join(os.path.dirname(__file__), "data.db"))
VERSION = "1.0.0"
SCHEMA_VERSION = 1
STARTED = time.time()

# abuse limits
MAX_ITEMS = int(os.environ.get("API_MAX_ITEMS", "1000"))            # hard row ceiling
MAX_DB_BYTES = int(os.environ.get("API_MAX_DB_BYTES", str(50 * 1024 * 1024)))  # 50 MB
WRITE_KEY = os.environ.get("API_WRITE_KEY", "")                     # gate mutations (prod)

# courtesy in-app rate limit (fixed window per IP) — surfaced via X-RateLimit-* headers
RL_LIMIT = int(os.environ.get("API_RATE_LIMIT", "120"))
RL_WINDOW = int(os.environ.get("API_RATE_WINDOW", "60"))
_rl = {}
_rl_lock = threading.Lock()

# ---------- audit log: who / when / what, rotating so it can't grow unbounded ----------
LOG_PATH = os.environ.get("API_LOG", os.path.join(os.path.dirname(DB_PATH) or ".", "access.log"))
access_log = logging.getLogger("api.access")
access_log.setLevel(logging.INFO)
if not access_log.handlers:
    _h = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3)
    _h.setFormatter(logging.Formatter("%(message)s"))
    access_log.addHandler(_h)
    access_log.propagate = False


def client_ip(request):
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "-")


def _commit():
    # provenance: written by CI at deploy time, else "dev"
    for p in (os.path.join(os.path.dirname(__file__), "version.json"), "/opt/testapi/version.json"):
        try:
            return jsonlib.load(open(p)).get("commit", "dev")
        except Exception:
            continue
    return os.environ.get("GIT_COMMIT", "dev")


COMMIT = _commit()

app = FastAPI(
    title="chrome.net.ua API Sandbox",
    version=VERSION,
    description="A small self-hosted testing API — a SQLite-backed CRUD resource "
                "(`items`) plus version/health. Demonstrates production API "
                "patterns: **RFC 9457** `problem+json` errors, **X-RateLimit-\\*** "
                "headers, **ETag / If-None-Match** (304) caching, **Idempotency-Key** "
                "on POST, and **RFC 8288 Link** pagination. Use **Try it out** to "
                "fire real requests. Writes require an **X-API-Key** (click "
                "**Authorize**); reads are open.",
    root_path=ROOT_PATH,
    docs_url=None,   # served by custom routes below (with a back-to-portfolio link)
    redoc_url=None,
)


# ---------- RFC 9457 problem+json errors ----------
HTTP_TITLES = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
               404: "Not Found", 409: "Conflict", 413: "Payload Too Large",
               415: "Unsupported Media Type", 422: "Unprocessable Entity",
               429: "Too Many Requests", 500: "Internal Server Error",
               507: "Insufficient Storage"}


def problem(status, title=None, detail=None, request=None, headers=None, extra=None):
    body = {"type": "about:blank", "title": title or HTTP_TITLES.get(status, "Error"),
            "status": status}
    if detail:
        body["detail"] = detail
    if request is not None:
        body["instance"] = str(request.url.path)
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status,
                        media_type="application/problem+json", headers=headers)


@app.exception_handler(StarletteHTTPException)
async def _http_exc(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else None
    return problem(exc.status_code, detail=detail, request=request,
                   headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")}
              for e in exc.errors()]
    return problem(422, detail="request validation failed", request=request,
                   extra={"errors": errors})


# ---------- audit log + courtesy rate limit + version header, on every request ----------
@app.middleware("http")
async def audit_and_limit(request: Request, call_next):
    started = time.time()
    path = request.url.path
    exempt = path.startswith("/docs") or path.startswith("/redoc") or path == "/openapi.json"
    ip = client_ip(request)
    now_s = int(time.time())
    rl_headers = {}
    over = False
    if not exempt:
        with _rl_lock:
            ws, cnt = _rl.get(ip, (now_s, 0))
            if now_s - ws >= RL_WINDOW:
                ws, cnt = now_s, 0
            cnt += 1
            _rl[ip] = (ws, cnt)
            if len(_rl) > 10000:            # keep the map bounded
                _rl.clear()
                _rl[ip] = (ws, cnt)
            reset = ws + RL_WINDOW
            over = cnt > RL_LIMIT
            rl_headers = {"X-RateLimit-Limit": str(RL_LIMIT),
                          "X-RateLimit-Remaining": str(0 if over else RL_LIMIT - cnt),
                          "X-RateLimit-Reset": str(reset)}
    if over:
        response = problem(429, detail="rate limit exceeded — slow down",
                           request=request,
                           headers={**rl_headers, "Retry-After": str(reset - now_s)})
    else:
        response = await call_next(request)
        for k, v in rl_headers.items():
            response.headers[k] = v
    response.headers["API-Version"] = VERSION
    if not exempt:
        q = ("?" + request.url.query) if request.url.query else ""
        access_log.info('%s %s %s %s%s %d %dms "%s"' % (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ip, request.method, path, q, response.status_code,
            int((time.time() - started) * 1000),
            request.headers.get("user-agent", "-")[:120]))
    return response


# ---------- docs pages with a "back to portfolio" link ----------
BACK_LINK = (
    '<a href="/" title="Back to portfolio" style="position:fixed;top:12px;right:16px;'
    'z-index:9999;font-family:\'IBM Plex Mono\',ui-monospace,monospace;font-size:13px;'
    'font-weight:600;color:#c1440e;text-decoration:none;background:#ece8df;'
    'border:1px solid #cfc9bb;padding:7px 13px;border-radius:4px;'
    'box-shadow:0 1px 4px rgba(0,0,0,.10);">&larr; vladyslav.taran</a>'
)


def _with_back(html: HTMLResponse) -> HTMLResponse:
    body = html.body.decode().replace("</body>", BACK_LINK + "</body>")
    return HTMLResponse(body)


@app.get("/docs", include_in_schema=False)
def swagger_docs(request: Request):
    p = request.scope.get("root_path", "") or ROOT_PATH
    return _with_back(get_swagger_ui_html(
        openapi_url=p + "/openapi.json", title=app.title + " — Swagger UI"))


@app.get("/redoc", include_in_schema=False)
def redoc_docs(request: Request):
    p = request.scope.get("root_path", "") or ROOT_PATH
    return _with_back(get_redoc_html(
        openapi_url=p + "/openapi.json", title=app.title + " — ReDoc"))


# ---------- database ----------
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS items(
                 id       INTEGER PRIMARY KEY AUTOINCREMENT,
                 name     TEXT NOT NULL,
                 qty      INTEGER NOT NULL DEFAULT 0,
                 tags     TEXT NOT NULL DEFAULT '[]',
                 created  TEXT NOT NULL,
                 updated  TEXT NOT NULL)"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS idempotency(
                 key      TEXT PRIMARY KEY,
                 response TEXT NOT NULL,
                 created  TEXT NOT NULL)"""
        )
        con.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        # seed a little demo data so reads always show something
        if con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 0:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            con.executemany(
                "INSERT INTO items(name, qty, tags, created, updated) VALUES(?,?,?,?,?)",
                [("sample-widget", 5, '["demo"]', ts, ts),
                 ("sample-gadget", 2, "[]", ts, ts)])


init_db()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def row_to_item(r):
    return {"id": r["id"], "name": r["name"], "qty": r["qty"],
            "tags": jsonlib.loads(r["tags"]), "created": r["created"], "updated": r["updated"]}


# ---------- write gate (mutations require X-API-Key when API_WRITE_KEY is set) ----------
_write_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_write(key: Optional[str] = Depends(_write_scheme)):
    if not WRITE_KEY:
        return  # dev / unset: writes are open
    if not key or not hmac.compare_digest(key, WRITE_KEY):
        raise HTTPException(401, "writes require a valid X-API-Key")


# ============================================================
# Meta
# ============================================================
@app.get("/", tags=["meta"], summary="API index")
def index(request: Request):
    p = request.scope.get("root_path", "") or ROOT_PATH
    return {
        "name": "chrome.net.ua API Sandbox",
        "version": VERSION,
        "docs": p + "/docs",
        "openapi": p + "/openapi.json",
        "endpoints": ["/health", "/version", "/items"],
    }


@app.get("/health", tags=["meta"], summary="Liveness / status probe")
def health():
    return {"ok": True}


@app.get("/logs", include_in_schema=False, dependencies=[Depends(require_write)])
def logs(limit: int = Query(100, ge=1, le=2000)):
    """Recent audit lines (who/when/what). Hidden; requires the X-API-Key."""
    try:
        with open(LOG_PATH) as f:
            tail = f.readlines()[-limit:]
    except FileNotFoundError:
        tail = []
    return {"log": LOG_PATH, "count": len(tail), "lines": [x.rstrip("\n") for x in tail]}


@app.get("/version", tags=["meta"], summary="Version & provenance")
def version():
    return {
        "api": VERSION,
        "db_schema": SCHEMA_VERSION,
        "commit": COMMIT,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(STARTED)),
        "uptime_seconds": int(time.time() - STARTED),
    }


# ============================================================
# CRUD — items
# ============================================================
def _check_tags(v):
    if v is None:
        return v
    for t in v:
        if len(t) > 50:
            raise ValueError("each tag must be <= 50 characters")
    return v


class ItemIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, examples=["widget"])
    qty: int = Field(0, ge=0, le=1_000_000_000, examples=[5])
    tags: List[str] = Field(default_factory=list, max_length=20, examples=[["demo", "test"]])

    _v_tags = field_validator("tags")(_check_tags)


class ItemPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    qty: Optional[int] = Field(None, ge=0, le=1_000_000_000)
    tags: Optional[List[str]] = Field(None, max_length=20)

    _v_tags = field_validator("tags")(_check_tags)


@app.get("/items", tags=["items"], summary="List / search items")
def list_items(
    request: Request,
    response: Response,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None, description="substring match on name"),
):
    with db() as con:
        if q:
            rows = con.execute(
                "SELECT * FROM items WHERE name LIKE ? ORDER BY id LIMIT ? OFFSET ?",
                ("%" + q + "%", limit, offset)).fetchall()
            total = con.execute("SELECT COUNT(*) c FROM items WHERE name LIKE ?",
                                ("%" + q + "%",)).fetchone()["c"]
        else:
            rows = con.execute("SELECT * FROM items ORDER BY id LIMIT ? OFFSET ?",
                               (limit, offset)).fetchall()
            total = con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    # RFC 8288 Link header pagination + total count
    base = (request.scope.get("root_path", "") or ROOT_PATH) + "/items"
    qs = ("&q=" + q) if q else ""
    links = []

    def link(o, rel):
        links.append('<%s?limit=%d&offset=%d%s>; rel="%s"' % (base, limit, o, qs, rel))

    link(0, "first")
    if offset > 0:
        link(max(0, offset - limit), "prev")
    if offset + limit < total:
        link(offset + limit, "next")
    link(max(0, ((total - 1) // limit) * limit) if total else 0, "last")
    response.headers["X-Total-Count"] = str(total)
    response.headers["Link"] = ", ".join(links)
    return {"total": total, "limit": limit, "offset": offset,
            "items": [row_to_item(r) for r in rows]}


@app.post("/items", tags=["items"], status_code=201, summary="Add a new item",
          dependencies=[Depends(require_write)])
def create_item(item: ItemIn,
                idempotency_key: Optional[str] = Header(
                    None, description="send a unique key to make this POST replay-safe")):
    # Idempotency-Key: replay the stored result instead of creating a duplicate
    if idempotency_key:
        with db() as con:
            prev = con.execute("SELECT response FROM idempotency WHERE key=?",
                               (idempotency_key,)).fetchone()
        if prev:
            return JSONResponse(jsonlib.loads(prev["response"]), status_code=201,
                                headers={"Idempotency-Replayed": "true"})
    with db() as con:
        count = con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    if count >= MAX_ITEMS:
        raise HTTPException(409, "item limit reached (%d) — this is a sandbox and "
                                 "auto-resets daily" % MAX_ITEMS)
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > MAX_DB_BYTES:
        raise HTTPException(507, "storage limit reached")
    ts = now()
    with db() as con:
        cur = con.execute(
            "INSERT INTO items(name, qty, tags, created, updated) VALUES(?,?,?,?,?)",
            (item.name, item.qty, jsonlib.dumps(item.tags), ts, ts))
        new_id = cur.lastrowid
        row = con.execute("SELECT * FROM items WHERE id=?", (new_id,)).fetchone()
    result = row_to_item(row)
    if idempotency_key:
        with db() as con:
            con.execute("INSERT OR IGNORE INTO idempotency(key, response, created) "
                        "VALUES(?,?,?)", (idempotency_key, jsonlib.dumps(result), ts))
    return result


@app.get("/items/{item_id}", tags=["items"], summary="Read one item")
def get_item(response: Response, item_id: int = Path(..., ge=1),
             if_none_match: Optional[str] = Header(None)):
    with db() as con:
        row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, "item not found")
    item = row_to_item(row)
    # ETag / conditional GET — return 304 when the client's copy is current
    etag = '"%s"' % hashlib.sha256(
        jsonlib.dumps(item, sort_keys=True).encode()).hexdigest()[:16]
    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return item


@app.put("/items/{item_id}", tags=["items"], summary="Update (replace) an item",
         dependencies=[Depends(require_write)])
def replace_item(item_id: int, item: ItemIn):
    with db() as con:
        exists = con.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "item not found")
        con.execute("UPDATE items SET name=?, qty=?, tags=?, updated=? WHERE id=?",
                    (item.name, item.qty, jsonlib.dumps(item.tags), now(), item_id))
        row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return row_to_item(row)


@app.patch("/items/{item_id}", tags=["items"], summary="Partially update an item",
           dependencies=[Depends(require_write)])
def patch_item(item_id: int, patch: ItemPatch):
    with db() as con:
        row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "item not found")
        name = patch.name if patch.name is not None else row["name"]
        qty = patch.qty if patch.qty is not None else row["qty"]
        tags = jsonlib.dumps(patch.tags) if patch.tags is not None else row["tags"]
        con.execute("UPDATE items SET name=?, qty=?, tags=?, updated=? WHERE id=?",
                    (name, qty, tags, now(), item_id))
        row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return row_to_item(row)


@app.delete("/items/{item_id}", tags=["items"], summary="Delete one item",
            dependencies=[Depends(require_write)])
def delete_item(item_id: int):
    with db() as con:
        cur = con.execute("DELETE FROM items WHERE id=?", (item_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "item not found")
    return {"deleted": item_id}
