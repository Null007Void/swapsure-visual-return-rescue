# Hosted judging access — plan, costs, gaps

Status: **prepared offline, nothing deployed, no credential entered, no unit spent.**

## 1. The shape of the deployment

One small container running this application, listening on `127.0.0.1:<port>` inside the platform's
private network, behind the platform's TLS terminator. Nothing in the app is exposed directly.

    browser ──HTTPS──> platform proxy (TLS + HTTP basic auth + request logging)
                              │ Host: <public judge host>
                              ▼
                       127.0.0.1:8080  src/ui/server.py
                                       --preview-mode LIVE
                                       --bind 127.0.0.1
                                       --public-host <public judge host>
                                       --threaded
                                       --data-dir /data

`--public-host` is the only hostname accepted in the `Host` header; `127.0.0.1`, `localhost` and
`[::1]` stay accepted for health checks. There is no wildcard and the default binding remains loopback,
so the same source still runs as a local demo exactly as the README describes.

The application itself has **no login**. Authentication must come from the proxy. A judge receives a
username and password in the submission's testing instructions; the password is a single shared demo
credential, rotated after judging, and is not the YouCam key.

## 2. Credentials

    YOUCAM_API_KEY                  platform secret store, injected at process start, never in the
                                    repo, never in the image, never in a URL, never logged
    judge username / password       proxy only, supplied in the testing instructions
    SWAPSURE_LIVE_AUTHORIZATION_REF a distinct judging reference, so judging spend is separable
                                    from the owner's recording ledger

The key is readable by anything running as the container user while the process lives. That is stated
plainly here rather than described as sandboxed.

## 3. Ledger and allowance

A **separate** ledger for judging: `SWAPSURE_LIVE_LEDGER=/data/judging-ledger.sqlite3`. It is not the
recording ledger at `<local path>`, and its remainder is not the
provider account balance.

    unit cost            2 per preview (provider's documented price)
    proposal             allow the owner to name a number of units; nothing is allocated here
    option arithmetic    previews at 2 units each: 20 previews = 40 units, 40 = 80 units,
                         100 = 200 units. The number of judges is unknown and is not assumed here;
                         pick a total from the preview count you are willing to fund, not from a
                         guessed headcount. The provider account balance would cap it in practice
                         and has not been verified in this offline pass.
    ceiling              SWAPSURE_LIVE_UNIT_CAP = that number, enforced by the ledger before dispatch
    session cap          a fraction of the total, never equal to it: a session cap equal to the
                         total means one visitor can consume everything, which is the opposite of
                         sharing. Suggest 8 units per session against a larger total.

The ledger refuses an operation identity it has already recorded, so repeated clicks, refreshes and
duplicate tabs cannot create two paid tasks for the same photo and garment. When the ceiling is
reached the page shows the real refusal with the remaining figure and offers the refund route; it never
switches to MOCK silently and never presents a placeholder as a provider result.

Low balance: the remaining figure is already shown on the session page (units left of the cap). A
short operator read-out is available from the ledger file itself; there is no dashboard, and none is
promised.

## 4. Hosting options and indicative cost

Prices are indicative and must be confirmed at purchase; nothing has been bought.

| Option | Shape | Indicative cost | Notes |
| --- | --- | --- | --- |
| Google Cloud e2-small + 10 GB disk (chosen) | us-central1, Caddy in front for TLS and basic auth | under $15 for the judging window, from the public price list | keeps SQLite and uploads on a persistent disk; see docs/DEPLOY-GOOGLE-CLOUD.md |
| Render web service | starter instance + persistent disk | about $7 / month (service) + disk | simpler UI, disk required for the ledger and uploads |
| Railway | container + volume | about $5 / month usage-based | easy, less predictable |
| Hetzner CX22 VPS + Caddy | full VM | about €4–5 / month | cheapest at long horizon, but you own patching, TLS and restarts |
| Tunnel to a personal machine | — | 0 | **excluded by instruction**, and it would put the recording workstation on the internet |

The instance must stay available through the end of judging: **20 November 2026, 16:45 UTC**. Do not
schedule a shutdown before that instant, and do not scale to zero on a schedule that could sleep
through it. Estimates below are estimates, not guaranteed totals.

## 5. Suitability checks, done offline against this tree

    loopback default            unchanged; hosted mode is opt-in through --bind/--public-host
    host allow-list             only the named public host plus loopback; wildcard hosts refused
    concurrency                 --threaded serves requests in parallel (ThreadingWSGIServer);
                                SQLite runs in WAL with a per-store RLock, reservations use
                                BEGIN IMMEDIATE, so two clicks cannot both reserve
    session isolation           session id + request token on every mutation; a foreign session id
                                returns 403/404, not another shopper's page
    upload isolation            photos are stored per data dir, keyed by session, never served from a
                                shared static path; uploads are PNG-only, size- and dimension-checked
    deletion                    "Delete local previews" removes this server's photo and cached result
                                bytes and says so; provider copies follow the provider's terms and the
                                page says that too
    retention                   provider result URLs expire; an expired preview is labelled expired and
                                its element removed, with refund still reachable
    restart recovery            the case store and the ledger are both on disk; a restart resumes
                                pending work without re-billing, and an unfinished reservation stays
                                visible rather than being silently refunded
    durable accounting          reservations are rows with operation identity = photo hash + session +
                                garment, so a restart cannot double-spend
    logs                        the app logs requests, not keys or image bytes; proxy access logs
                                should be trimmed to status and path

Not verified offline, and therefore not claimed: TLS termination behaviour, proxy header rewriting,
real multi-user concurrency under load, and the provider's behaviour under a shared key. Those need
the deployment and a supervised smoke test.

## 6. The judge-facing text

`SWAPSURE_JUDGING_NOTICE=1` renders a short note on every page:

> Shared judging demo. Please start with up to three previews so other reviewers can try it too. This
> is a request, not a limit: you can keep going, and every other feature stays available. Each preview
> costs 2 provider units from a fixed allowance; when it runs out the page says so plainly and offers
> the refund route — it never pretends to be an offline demo.

It is advisory text with no enforcement, asserted by tests: a session is not gated at three, and the
refund route stays visible throughout. It is not a fairness guarantee, and nothing in the product
measures or balances one judge against another.

## 7. Gaps and the conflict you should decide on

1. **Unrestricted testing vs a hard ceiling.** The rules ask that the Project be made available
   "free of charge and without any restriction" for judging, while a spent-unit ceiling is, by
   construction, a restriction. A ceiling is the only responsible way to share a paid key, but this
   plan does **not** claim to satisfy that clause; it may need organiser clarification, or a ceiling
   set high enough that no plausible judging load reaches it.
2. **Shared key, shared risk.** All judges would spend from one provider account. Per-judge accounting
   does not exist in the product; only per-session and total caps do.
3. **No product login.** Authentication is entirely the proxy's job. If the proxy is misconfigured the
   app is open, which is why the app must never be bound to a public interface directly.
4. **Video is still required** by the rules regardless of hosted access.
5. **Provider retention** is unverified here; the hosted instance must say the same thing the local one
   says and no more.

## 8. Approvals needed before anything happens

    A. hosting account and charge              name the provider, the region, the instance size and the
                                               volume; approve the monthly figure and the shutdown
                                               date (on or before 20 November)
    B. API-unit allowance for judging           a number of units, not "whatever is left"; the plan
                                               above suggests 48 for twelve reviewers at two previews
                                               each, and the owner may choose less
    C. the judge credential                     who sets it, how it is delivered in the testing
                                               instructions, and when it is rotated
    D. a supervised first live preview          one attended run against the hosted instance to prove
                                               the proxy, the key path and the ledger before judges
                                               are given the link
    E. the decision on the ceiling conflict     accept the ceiling and say so in the submission, or ask
                                               the organisers first

Until A–E are answered, nothing is deployed, no key is entered, and no unit is spent.

## 9. What this plan does not establish

    - Compliance with the "free of charge and without any restriction" testing clause. Disclosing a
      budget ceiling does not make a capped demo unrestricted; the question is still open.
    - That the chosen platform supplies TLS and authentication by itself. Some do not; a proxy that
      terminates TLS and performs authentication must be part of the topology and must be configured
      explicitly, covering every route including image and status endpoints.
    - Current provider pricing. It must be read from the official price list at decision time; the
      figures in this document are estimates carried from earlier reading.
