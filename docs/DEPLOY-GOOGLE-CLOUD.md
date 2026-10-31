# Google Cloud deployment plan - SwapSure 1.7.12

Status: prepared and tested offline. Nothing created, no billing changed, no key configured.

## Why a single VM and not Cloud Run

The product keeps its accounting in SQLite and its media on a local directory. The Cloud Run container
filesystem is ephemeral, so a restart or a new revision would lose both. The GCP-native answers are
Cloud SQL, which means replacing SQLite, or Filestore, whose smallest share is far larger than this
project needs. A single small Compute Engine VM with a persistent disk keeps the code as it is and keeps
the ledger on disk.

    Internet --HTTPS--> Caddy (TLS, HTTP basic auth, access log)  :443
                                 |  X-Forwarded-Proto: https
                                 v
                        127.0.0.1:8080 python src/ui/server.py
                                             --external-scheme https
                                             --preview-mode LIVE --threaded
                        /mnt/swapsure-data   <- persistent disk (app.db, photos, judging ledger)

`--external-scheme https` is set by the operator, never read from a request header, so a client cannot
talk the server into trusting a scheme. Cookies gain `Secure` in that mode and lose it again on a plain
local run.

## Proposed resources (smallest reliable)

| Resource | Setting | Why |
| --- | --- | --- |
| Project | a new project dedicated to SwapSure, not the existing CivicTrace project | isolation of billing, IAM and quotas |
| Region / zone | us-central1 / us-central1-a | cheapest common region, close to most judging traffic |
| Machine | e2-small (2 vCPU burst, 2 GB) | e2-micro can be free-tier eligible but has 1 GB; a burstable small leaves headroom for a threaded server and SQLite writes |
| Boot disk | 10 GB pd-balanced | OS plus the image |
| Data disk | 10 GB pd-balanced, mounted at /mnt/swapsure-data | SQLite, uploads, judging ledger, surviving reboots and redeploys |
| Networking | default VPC, firewall 80/443 open, 8080 closed | only the proxy is reachable |
| Address | reserved static external IP | the judge hostname must not move |
| Secrets | Secret Manager for YOUCAM_API_KEY | never in the image, repo, env files or logs |
| Snapshots | daily disk snapshot, 7-day retention | recovery from a bad delete |

## Cost estimate through 20 November 2026, 16:45 UTC

Estimates from the public GCP price list for us-central1, about 21 days at 24x7. Confirm in the pricing
calculator before creating anything. These are estimates, not a quoted total.

    e2-small, 730 h/month ............... ~$12.23/month  -> ~$8.60 for 21 days
    10 GB pd-balanced boot disk ......... ~$1.00/month   -> ~$0.70
    10 GB pd-balanced data disk ......... ~$1.00/month   -> ~$0.70
    reserved external IPv4 .............. ~$0.005/hour   -> ~$2.50 for 21 days
    egress, light (25 GB free/month) .... ~$0.00 assumed, $0.12/GB beyond
    Secret Manager, 1 secret ............ ~$0.06/month   -> ~$0.04
    snapshots, 10 GB, 7 days ............ ~$0.26/month   -> ~$0.18
    ------------------------------------------------------------------
    estimated total ..................... under $15 for the window

The $300 credit would cover this many times over. It is available funding, not an authorisation to
spend it. A realistic requested ceiling is **$25** for the window, including slack for a static IP and
egress, with the VM stopped or deleted after 20 November. Budget alerts are a warning, not a spending
stop; the only hard stop is deleting the resources.

## Credit eligibility

The $300 credit is normally tied to a new account and expires 90 days after activation, and the free
trial ends when the credit is consumed or 90 days pass. Whether this account credit is still live and
when it expires must be read from the Billing console. That check has not been made here and cannot be
made without signing in. Treat the credit as "probably available, expiry unknown" until someone looks.

## Deployment sequence (not executed)

    1. create the project, link billing, confirm the credit amount and expiry
    2. enable Compute Engine and Secret Manager
    3. create the data disk and mount it at /mnt/swapsure-data
    4. create the VM with a startup script that installs Docker and Caddy
    5. store the YouCam key in Secret Manager and grant the VM service account read access
    6. point the judge hostname at the reserved address
    7. run deploy/start.sh with the judge host, ACME email and basic-auth hash
    8. smoke test, then hand the link and password to the submission

## What must be verified on the deployed instance

    - https is served, http redirects, HSTS present
    - basic auth is enforced on every path, including /catalog/*/thumb and /ledger
    - a POST with a foreign Origin is refused, and one with the true Origin succeeds
    - cookies carry Secure, HttpOnly and SameSite=Lax
    - the ledger file survives a stop/start and a machine reboot, and the remaining units are unchanged
    - two simultaneous identical preview requests create one task
    - deletion removes local media and says so, and never claims provider erasure
