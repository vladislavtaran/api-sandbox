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
import sqlite3
import json as jsonlib
from typing import Optional, List
from contextlib import contextmanager

from fastapi import FastAPI, Request, HTTPException, Path, Query, Depends
from fastapi.responses import HTMLResponse
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.security import APIKeyHeader
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
                "(`items`) plus version/health. Use the **Try it out** buttons to "
                "fire real requests. Writes require an **X-API-Key** (click "
                "**Authorize**); reads are open.",
    root_path=ROOT_PATH,
    docs_url=None,   # served by custom routes below (with a back-to-portfolio link)
    redoc_url=None,
)


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
    return {"total": total, "limit": limit, "offset": offset,
            "items": [row_to_item(r) for r in rows]}


@app.post("/items", tags=["items"], status_code=201, summary="Add a new item",
          dependencies=[Depends(require_write)])
def create_item(item: ItemIn):
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
    return row_to_item(row)


@app.get("/items/{item_id}", tags=["items"], summary="Read one item")
def get_item(item_id: int = Path(..., ge=1)):
    with db() as con:
        row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, "item not found")
    return row_to_item(row)


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
