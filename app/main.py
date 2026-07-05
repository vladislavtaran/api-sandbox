#!/usr/bin/env python3
"""
chrome.net.ua API Sandbox
=========================
A small self-hosted testing API: a request inspector, behaviour simulator,
auth playground, and a real SQLite-backed CRUD resource ("items").

Runs behind nginx at https://chrome.net.ua/api/ (root_path=/api), bound to
127.0.0.1:8092. FastAPI gives an interactive Swagger UI at /api/docs.

Stdlib + FastAPI/Uvicorn only. No external database — persistence is stdlib
SQLite in a single file (API_DB, default ./data.db).
"""
import os
import time
import uuid
import hmac
import base64
import hashlib
import sqlite3
import asyncio
import binascii
import json as jsonlib
from typing import Optional, List
from urllib.parse import parse_qsl
from contextlib import contextmanager

from fastapi import FastAPI, Request, Response, HTTPException, Path, Query, Body, Depends
from fastapi.responses import JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator

# ---------- config ----------
ROOT_PATH = os.environ.get("API_ROOT_PATH", "")          # "/api" in production
DB_PATH = os.environ.get("API_DB", os.path.join(os.path.dirname(__file__), "data.db"))
SECRET = os.environ.get("API_SECRET", "dev-secret-change-me").encode()
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
    description="A self-hosted testing API — request inspector, behaviour "
                "simulator, auth playground, and a SQLite-backed CRUD resource. "
                "Use the **Try it out** buttons below to fire real requests.",
    root_path=ROOT_PATH,
    docs_url="/docs",
    redoc_url="/redoc",
)


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


# ---------- write gate (mutations require X-API-Key when API_WRITE_KEY is set) ----------
_write_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_write(key: Optional[str] = Depends(_write_scheme)):
    if not WRITE_KEY:
        return  # dev / unset: writes are open
    if not key or not hmac.compare_digest(key, WRITE_KEY):
        raise HTTPException(401, "writes require a valid X-API-Key")


def now():
    # avoid Date.now-style nondeterminism concerns; wall clock is fine here
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def row_to_item(r):
    return {"id": r["id"], "name": r["name"], "qty": r["qty"],
            "tags": jsonlib.loads(r["tags"]), "created": r["created"], "updated": r["updated"]}


# ---------- helpers ----------
def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def prefix(request: Request) -> str:
    return request.scope.get("root_path", "") or ROOT_PATH


def external_url(request: Request) -> str:
    """Rebuild the URL the client actually called (nginx strips the root_path)."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    p = request.scope.get("root_path", "") or ROOT_PATH
    q = ("?" + request.url.query) if request.url.query else ""
    return "%s://%s%s%s%s" % (scheme, host, p, request.url.path, q)


async def reflect(request: Request):
    raw = await request.body()
    body_json = None
    form = None
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype and raw:
        try:
            body_json = jsonlib.loads(raw)
        except Exception:
            body_json = None
    elif "application/x-www-form-urlencoded" in ctype and raw:
        form = dict(parse_qsl(raw.decode("latin1")))
    return {
        "method": request.method,
        "url": external_url(request),
        "args": dict(request.query_params),
        "headers": dict(request.headers),
        "origin": client_ip(request),
        "form": form,
        "json": body_json,
        "data": raw.decode("latin1")[:4096] if (raw and body_json is None and form is None) else "",
    }


# ============================================================
# Meta
# ============================================================
@app.get("/", tags=["meta"], summary="API index")
def index(request: Request):
    p = prefix(request)
    return {
        "name": "chrome.net.ua API Sandbox",
        "version": VERSION,
        "docs": p + "/docs",
        "openapi": p + "/openapi.json",
        "groups": ["meta", "items (CRUD)", "inspect", "simulate", "auth", "cookies"],
    }


@app.get("/health", tags=["meta"], summary="Liveness probe")
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


@app.put("/items/{item_id}", tags=["items"], summary="Replace an item",
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


@app.delete("/items", tags=["items"], summary="Delete ALL items (reset)",
            dependencies=[Depends(require_write)])
def reset_items():
    with db() as con:
        n = con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        con.execute("DELETE FROM items")
    return {"deleted": n}


# ============================================================
# Request inspector
# ============================================================
@app.get("/get", tags=["inspect"], summary="Echo a GET request")
async def http_get(request: Request):
    r = await reflect(request)
    return {k: r[k] for k in ("args", "headers", "origin", "url")}


async def _echo_body(request: Request):
    return await reflect(request)


async def _anything(request: Request, path: str = ""):
    r = await reflect(request)
    r["path"] = "/" + path
    return r


# register per-method so every OpenAPI operation gets a unique operationId
for _m in ("POST", "PUT", "PATCH", "DELETE"):
    app.add_api_route("/" + _m.lower(), _echo_body, methods=[_m], tags=["inspect"],
                      summary="Echo a %s request" % _m,
                      operation_id="echo_" + _m.lower(), name="echo_" + _m.lower())

for _m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
    _ml = _m.lower()
    app.add_api_route("/anything", _anything, methods=[_m], tags=["inspect"],
                      summary="Reflect anything (%s)" % _m, operation_id="anything_root_" + _ml)
    app.add_api_route("/anything/{path:path}", _anything, methods=[_m], tags=["inspect"],
                      summary="Reflect anything on a sub-path (%s)" % _m,
                      operation_id="anything_path_" + _ml)


@app.get("/headers", tags=["inspect"], summary="Just the request headers")
def headers(request: Request):
    return {"headers": dict(request.headers)}


@app.get("/ip", tags=["inspect"], summary="Your public IP (server-side)")
def ip(request: Request):
    return {"origin": client_ip(request)}


@app.get("/user-agent", tags=["inspect"], summary="Your User-Agent")
def user_agent(request: Request):
    return {"user-agent": request.headers.get("user-agent", "")}


@app.get("/echo", tags=["inspect"], summary="Echo a message")
def echo(msg: str = Query("", max_length=2000)):
    return {"echo": msg}


# ============================================================
# Behaviour simulation
# ============================================================
@app.get("/status/{code}", tags=["simulate"], summary="Return a chosen HTTP status")
def status_code(code: int = Path(..., ge=100, le=599)):
    if code in (204, 304) or code < 200:
        return Response(status_code=code)
    return JSONResponse({"status": code}, status_code=code)


@app.get("/delay/{seconds}", tags=["simulate"], summary="Respond after N seconds (max 10)")
async def delay(seconds: float = Path(..., ge=0, le=10)):
    await asyncio.sleep(seconds)
    return {"delayed_seconds": seconds}


@app.get("/redirect/{n}", tags=["simulate"], summary="Chain N redirects")
def redirect(request: Request, n: int = Path(..., ge=0, le=20)):
    p = prefix(request)
    if n <= 0:
        return RedirectResponse(url=p + "/get")
    return RedirectResponse(url=p + "/redirect/%d" % (n - 1))


@app.get("/bytes/{n}", tags=["simulate"], summary="Return N random bytes (max 100 KB)")
def rand_bytes(n: int = Path(..., ge=0, le=100_000)):
    return Response(content=os.urandom(n), media_type="application/octet-stream")


@app.get("/uuid", tags=["simulate"], summary="A fresh UUID v4")
def gen_uuid():
    return {"uuid": str(uuid.uuid4())}


@app.get("/base64/{value}", tags=["simulate"], summary="Decode a base64 (urlsafe) value")
def b64_decode(value: str):
    try:
        pad = value + "=" * (-len(value) % 4)
        return {"decoded": base64.urlsafe_b64decode(pad).decode("utf-8", "replace")}
    except (binascii.Error, ValueError):
        raise HTTPException(400, "invalid base64")


# ============================================================
# Auth testing
# ============================================================
@app.get("/basic-auth/{user}/{passwd}", tags=["auth"],
         summary="401 until correct Basic credentials are sent")
def basic_auth(request: Request, user: str, passwd: str):
    hdr = request.headers.get("authorization", "")
    if hdr.startswith("Basic "):
        try:
            got = base64.b64decode(hdr[6:]).decode("utf-8", "replace")
            if got == "%s:%s" % (user, passwd):
                return {"authenticated": True, "user": user}
        except Exception:
            pass
    return JSONResponse({"authenticated": False}, status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="sandbox"'})


class TokenReq(BaseModel):
    user: str = Field("demo", examples=["demo"])
    ttl: int = Field(3600, ge=1, le=86400, description="token lifetime in seconds")


def make_token(user: str, ttl: int) -> str:
    payload = "%s:%d" % (user, int(time.time()) + ttl)
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(("%s:%s" % (payload, sig)).encode()).decode().rstrip("=")


def verify_token(token: str):
    try:
        pad = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(pad).decode()
        user, exp, sig = raw.rsplit(":", 2)
        good = hmac.new(SECRET, ("%s:%s" % (user, exp)).encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            return None, "bad signature"
        if int(exp) < time.time():
            return None, "expired"
        return user, None
    except Exception:
        return None, "malformed"


@app.post("/token", tags=["auth"], summary="Issue a signed bearer token")
def issue_token(req: TokenReq):
    return {"token": make_token(req.user, req.ttl), "token_type": "bearer", "expires_in": req.ttl}


@app.get("/bearer", tags=["auth"], summary="Require any Bearer token")
def bearer(request: Request):
    hdr = request.headers.get("authorization", "")
    if not hdr.startswith("Bearer ") or not hdr[7:].strip():
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return {"authenticated": True, "token": hdr[7:].strip()}


@app.get("/protected", tags=["auth"], summary="Require a valid token from /token")
def protected(request: Request):
    hdr = request.headers.get("authorization", "")
    if not hdr.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    user, err = verify_token(hdr[7:].strip())
    if err:
        raise HTTPException(401, "invalid token: " + err)
    return {"authenticated": True, "user": user}


# ============================================================
# Cookies
# ============================================================
@app.get("/cookies", tags=["cookies"], summary="Show cookies the server received")
def cookies(request: Request):
    return {"cookies": dict(request.cookies)}


@app.get("/cookies/set", tags=["cookies"], summary="Set cookies from query params")
def set_cookies(request: Request):
    resp = JSONResponse({"cookies": dict(request.query_params)})
    for k, v in request.query_params.items():
        resp.set_cookie(k, v, httponly=False, samesite="lax")
    return resp
