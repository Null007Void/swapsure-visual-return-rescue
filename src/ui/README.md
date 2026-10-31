# Phase C offline web experience

This package is the offline shopper workflow over the frozen Phase B contract. It
is framework-free WSGI (standard library only), server-rendered HTML with no
script element, and it opens no outbound socket.

| Module | Responsibility |
|---|---|
| `webapp.py` | routes, same-origin guard, CSRF token check, typed refusals |
| `views.py` | all HTML; every dynamic value escaped; neutral copy |
| `service.py` | workflow over domain/commerce; server-authoritative identity |
| `store.py` | SQLite records (WAL, atomic transactions) + simulator mirror |
| `media.py` | local placeholder SVG, scoped handles, 30-minute TTL, deletion ledger |
| `previews.py` | bounded mock create/poll: 3 candidates, 2 in flight, 60 s, 0 units |
| `uploads.py` | bounded UTF-8 JSON scenario file (64 KiB, strict schema) |
| `security.py` | opaque ids, cookies, origin proof, URL rejection |
| `server.py` | loopback-only demo server (`python -m ui.server`) |
| `demo.py` | reset and status helpers (`python -m ui.demo reset`) |

## Run it

```
python -m ui.server --port 8000 --data-dir data
```

Open http://127.0.0.1:8000/ — the server binds loopback only and makes no external
request. `--preview-mode LIVE` fails closed with `PREVIEW_MODE_UNAVAILABLE` and
makes zero calls; live work needs a separate Phase D authorization.

## Reset it

```
python -m ui.demo reset --data-dir data
python -m ui.demo status --data-dir data
```

The reset refuses paths outside the working directory.

## What is deliberately absent

No photo upload, no real YouCam call, no live credential, no deployment, no
analytics, no JavaScript, no background thread and no timer. Previews are local
SVG placeholders labelled `MOCK PREVIEW - PLACEHOLDER - NOT A YOUCAM RESULT`.

See `docs/15-offline-demo-guide.md` and `SWAP-C-REPORT.md` for the route inventory,
evidence mapping and known limits.
