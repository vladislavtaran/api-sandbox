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

## Security
- Bound to **127.0.0.1** only; nginx is the sole public entry.
- nginx **rate limiting** (`limit_req`) + **1 MB body cap**.
- Bounded inputs: delay ≤ 10 s, bytes ≤ 100 KB, redirects ≤ 20, validated types.
- No SSRF surface — the API never fetches user-supplied URLs.
- `API_SECRET` (token signing) lives only in `/etc/testapi.env` (root-owned, 600).

## Run locally

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8092
# open http://127.0.0.1:8092/docs
```

## License

[MIT](LICENSE) © 2026 Vladyslav Taran
