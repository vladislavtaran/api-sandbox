# API Sandbox

A small self-hosted **testing API** — a request inspector, a behaviour
simulator, an auth playground, and a real **SQLite-backed CRUD** resource. Built
with **FastAPI**, so it ships an interactive **Swagger UI** where you can fire
real requests from the browser.

**Live:** https://chrome.net.ua/api/  ·  **Docs (try it out):** https://chrome.net.ua/api/docs

## Endpoints

### Meta
| Method | Path | Description |
|---|---|---|
| GET | `/api/` | index + links |
| GET | `/api/health` | liveness probe |
| GET | `/api/version` | api version, db schema, git commit, uptime |
| GET | `/api/docs` · `/api/redoc` | Swagger UI / ReDoc |
| GET | `/api/openapi.json` | machine-readable OpenAPI spec |

### Items — CRUD (persisted in SQLite)
| Method | Path | Description |
|---|---|---|
| GET | `/api/items?limit=&offset=&q=` | list / search |
| POST | `/api/items` | **create** — body `{"name","qty","tags":[]}` |
| GET | `/api/items/{id}` | read one |
| PUT | `/api/items/{id}` | **replace** |
| PATCH | `/api/items/{id}` | partial **update** |
| DELETE | `/api/items/{id}` | **delete** one |
| DELETE | `/api/items` | reset all |

> Writes (`POST/PUT/PATCH/DELETE`) require an `X-API-Key` header — see [Security](#security--abuse-limits). Reads are open.

### Request inspector
| Method | Path | Description |
|---|---|---|
| GET | `/api/get` | echo query, headers, your IP, URL |
| POST/PUT/PATCH/DELETE | `/api/post` `/put` `/patch` `/delete` | echo body + metadata |
| ANY | `/api/anything/{path}` | reflect everything |
| GET | `/api/headers` · `/api/ip` · `/api/user-agent` · `/api/echo?msg=` | single facts |

### Behaviour simulation
| Method | Path | Description |
|---|---|---|
| GET | `/api/status/{code}` | return that HTTP status |
| GET | `/api/delay/{sec}` | respond after N s (max 10) |
| GET | `/api/redirect/{n}` | chain N redirects (max 20) |
| GET | `/api/bytes/{n}` | N random bytes (max 100 KB) |
| GET | `/api/uuid` · `/api/base64/{value}` | generate / decode |

### Auth
| Method | Path | Description |
|---|---|---|
| GET | `/api/basic-auth/{user}/{pass}` | 401 until correct Basic creds |
| POST | `/api/token` | issue a signed bearer token |
| GET | `/api/bearer` | require any Bearer token |
| GET | `/api/protected` | require a valid token from `/token` |

### Cookies
| Method | Path | Description |
|---|---|---|
| GET | `/api/cookies` | show received cookies |
| GET | `/api/cookies/set?k=v` | set cookies |

## Examples

```bash
# add an item, then read it back
curl -X POST https://chrome.net.ua/api/items \
  -H 'Content-Type: application/json' -d '{"name":"widget","qty":5,"tags":["demo"]}'
curl https://chrome.net.ua/api/items/1

# update / delete
curl -X PATCH https://chrome.net.ua/api/items/1 -H 'Content-Type: application/json' -d '{"qty":9}'
curl -X DELETE https://chrome.net.ua/api/items/1

# version, request inspector, simulate
curl https://chrome.net.ua/api/version
curl https://chrome.net.ua/api/get?foo=bar
curl https://chrome.net.ua/api/status/418
curl -s -o /dev/null -w '%{http_code}\n' https://chrome.net.ua/api/delay/2

# auth: mint a token then use it
TOKEN=$(curl -s -X POST https://chrome.net.ua/api/token -d '{"user":"demo"}' -H 'Content-Type: application/json' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl https://chrome.net.ua/api/protected -H "Authorization: Bearer $TOKEN"
```

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
- **1 MB body cap**; bounded sim endpoints (delay ≤ 10 s, bytes ≤ 100 KB,
  redirects ≤ 20).
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

## Run locally

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8092
# open http://127.0.0.1:8092/docs
```

## License

[MIT](LICENSE) © 2026 Vladyslav Taran
