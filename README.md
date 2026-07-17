# API Sandbox

A small self-hosted **testing API** — a real **SQLite-backed CRUD** resource
(`items`) plus version/health. Built with **FastAPI**, so it ships an
interactive **Swagger UI** where you can fire real requests from the browser.

Deliberately focused: just the endpoints you actually reach for when testing —
version, status, and CRUD — but built to production conventions
(**RFC 9457 errors, rate-limit headers, ETag/304, idempotency, Link pagination**;
see [API design](#api-design)).

**Live:** https://chrome.net.ua/api/  ·  **Docs (try it out):** https://chrome.net.ua/api/docs

## Endpoints

### Meta
| Method | Path | Description |
|---|---|---|
| GET | `/api/` | index + links |
| GET | `/api/health` | liveness / status probe |
| GET | `/api/version` | api version, db schema, git commit, uptime |
| GET | `/api/docs` · `/api/redoc` | Swagger UI / ReDoc |
| GET | `/api/openapi.json` | machine-readable OpenAPI spec |

### Items — CRUD (persisted in SQLite)
| Method | Path | Description |
|---|---|---|
| GET | `/api/items?limit=&offset=&q=` | list / search |
| POST | `/api/items` | **create** — body `{"name","qty","tags":[]}` |
| GET | `/api/items/{id}` | read one |
| PUT | `/api/items/{id}` | **update** (replace) |
| PATCH | `/api/items/{id}` | partial update |
| DELETE | `/api/items/{id}` | **delete** one |

> Writes (`POST/PUT/PATCH/DELETE`) require an `X-API-Key` header — see [Security](#security--abuse-limits). Reads are open.

## Examples

```bash
# service status & version
curl https://chrome.net.ua/api/health
curl https://chrome.net.ua/api/version

# list, then read one
curl https://chrome.net.ua/api/items
curl https://chrome.net.ua/api/items/1

# create / update / delete  (writes need the key)
curl -X POST https://chrome.net.ua/api/items \
  -H 'Content-Type: application/json' -H 'X-API-Key: <key>' \
  -d '{"name":"widget","qty":5,"tags":["demo"]}'
curl -X PATCH https://chrome.net.ua/api/items/1 \
  -H 'Content-Type: application/json' -H 'X-API-Key: <key>' -d '{"qty":9}'
curl -X DELETE https://chrome.net.ua/api/items/1 -H 'X-API-Key: <key>'
```

## API design

The API deliberately follows the conventions engineers expect from a
production REST service:

- **Standard error model — [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)
  `application/problem+json`.** Every 4xx/5xx returns a machine-readable problem
  document (`type`, `title`, `status`, `detail`, `instance`); validation errors
  add a structured `errors` array.
  ```jsonc
  { "type": "about:blank", "title": "Not Found", "status": 404,
    "detail": "item not found", "instance": "/items/999" }
  ```
- **Rate limiting with headers.** nginx enforces the hard edge; the app adds a
  courtesy per-IP window surfaced as `X-RateLimit-Limit / -Remaining / -Reset`,
  and returns **`429`** + `Retry-After` when exceeded.
- **Conditional requests / caching.** `GET /items/{id}` returns an **`ETag`**;
  send it back as `If-None-Match` to get a **`304 Not Modified`** (no body).
- **Idempotent creates.** `POST /items` accepts an **`Idempotency-Key`** header —
  a repeat with the same key replays the original result (`Idempotency-Replayed:
  true`) instead of creating a duplicate.
- **Pagination — [RFC 8288](https://www.rfc-editor.org/rfc/rfc8288) `Link`.**
  `GET /items` returns `X-Total-Count` and a `Link` header with
  `first`/`prev`/`next`/`last` (on top of the `limit`/`offset` body fields).
- **Versioning.** Every response carries an **`API-Version`** header;
  `GET /version` reports the running build's git commit.

```bash
# problem+json error
curl -i https://chrome.net.ua/api/items/999            # 404 application/problem+json

# conditional GET (304)
ETAG=$(curl -sD- -o/dev/null https://chrome.net.ua/api/items/1 | grep -i etag | awk '{print $2}' | tr -d '\r')
curl -i -H "If-None-Match: $ETAG" https://chrome.net.ua/api/items/1   # 304

# idempotent create (same key => same row)
curl -X POST https://chrome.net.ua/api/items -H 'X-API-Key: <key>' \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: abc-123' \
  -d '{"name":"widget"}'

# pagination headers
curl -sD- -o/dev/null 'https://chrome.net.ua/api/items?limit=1' | grep -iE 'x-total-count|link'
```

**Security posture** (aligned with the OWASP API Security Top 10): key-gated
mutations (API1/API5 — broken auth / function-level authz), strict Pydantic
input validation + row/size caps (API4 — resource consumption), problem+json
that leaks no internals (API8), no SSRF surface, and full request auditing.

## Architecture

```
browser ──HTTPS──► nginx (chrome.net.ua, existing cert, limit_req)
                     └─ /api/ → uvicorn app.main:app  (127.0.0.1:8092)
                                   └─ SQLite  /opt/testapi/data.db
```

- **Backend:** `app/main.py` — FastAPI, one file. Persistence via stdlib `sqlite3`.
- Runs as a hardened **systemd** service (`www-data`, `ProtectSystem=strict`).

## Security & abuse limits

Defense in depth, so a public sandbox can't be exhausted or vandalised — e.g.
someone trying to insert 10 million rows or wipe the data:

- **Writes require a key.** `POST/PUT/PATCH/DELETE /items` need an `X-API-Key`
  header (`API_WRITE_KEY`); reads stay open. No anonymous mutation. In Swagger,
  click **Authorize** to paste the key.
- **Hard row cap** — `POST /items` returns **409** once the table reaches
  `API_MAX_ITEMS` (default **1000**), so unbounded growth is impossible.
- **Storage guard** — inserts return **507** if `data.db` exceeds
  `API_MAX_DB_BYTES` (default **50 MB**); the disk can't be filled.
- **Field caps** — `name` ≤ 200 chars, ≤ 20 tags, each tag ≤ 50 chars, `qty`
  bounded → invalid input is **422**.
- **Rate limiting (nginx)** — 10 req/s per IP overall, **1 req/s for writes**
  (`POST/PUT/PATCH/DELETE`), and a **10-connection** cap per IP.
- **1 MB body cap**.
- **Nightly auto-reset** — `/etc/cron.d/testapi-reset` runs `reset.py`: backs up
  `data.db` (keeps 7), wipes `items`, reseeds demo rows — so any pollution is
  temporary.
- **Isolation** — bound to `127.0.0.1` (nginx is the only public entry); runs as
  a hardened **systemd** service (`www-data`, `ProtectSystem=strict`, writes only
  `/opt/testapi`). No SSRF surface — it never fetches user-supplied URLs.
- **Secrets** — `API_SECRET` (token signing) and `API_WRITE_KEY` live only in
  `/etc/testapi.env` (root-owned, `600`) — never in the repo.

### Configuration (env — `/etc/testapi.env`)
| Var | Default | Purpose |
|---|---|---|
| `API_ROOT_PATH` | `""` | external path prefix (`/api` in prod) |
| `API_DB` | `./data.db` | SQLite file path |
| `API_SECRET` | dev value | HMAC key for `/token` |
| `API_WRITE_KEY` | `""` (open) | required `X-API-Key` for mutations |
| `API_MAX_ITEMS` | `1000` | hard row ceiling |
| `API_MAX_DB_BYTES` | `52428800` | storage guard (50 MB) |
| `API_RATE_LIMIT` | `120` | in-app requests per window per IP |
| `API_RATE_WINDOW` | `60` | rate-limit window (seconds) |

## Logs & audit

Every request is recorded — **who** (client IP + user-agent), **when**
(UTC timestamp), and **what** (method, path, status, duration) — including failed
and unauthorized (`401`) attempts:

```
2026-07-05T19:10:09Z 24.5.138.137 GET /items 200 3ms "curl/8.7.1"
2026-07-05T19:10:09Z 24.5.138.137 POST /items 401 1ms "curl/8.7.1"
```

- **Via the API** (needs the key): `GET /api/logs?limit=100` with `X-API-Key`.
  Hidden from the docs list.
- **On the server:** `tail -f /opt/testapi/access.log` — a **rotating** file
  (5 MB × 3, so it can't grow unbounded). The shared nginx edge log also has it:
  `grep ' /api/' /var/log/nginx/access.log`.

```bash
curl -s "https://chrome.net.ua/api/logs?limit=50" -H "X-API-Key: <key>"
```

## Run locally

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8092
# open http://127.0.0.1:8092/docs
```

## License

[MIT](LICENSE) © 2026 Vladyslav Taran
