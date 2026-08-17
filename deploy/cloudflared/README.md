# Cloudflare named tunnel — the committed configuration

**Status: VERIFIED AGAINST A LIVE TUNNEL, 2026-08-17.** Written 2026-08-16 for
work package W1-F2 (ADR-005); brought up and read back under W3-G3. The tunnel
carries real traffic, including a Zendesk webhook that drove a complete agent
run.

The check that counts — a request from OUTSIDE the droplet that reaches the
app (last section). Every response below came back through the
Cloudflare edge (`server: cloudflare`, `cf-ray` present):

| Request | Result |
|---|---|
| `GET /` | **200**, `text/html` — the portal SPA index |
| `GET /health` | **200** `{"status":"ok"}` — the backend, through nginx |
| `GET /api/metrics` | **401** without a token |
| `POST /webhooks/zendesk` unsigned | **401** `{"detail":"missing signature headers"}` |

The supporting facts, also measured rather than assumed: `.env` holds a
184-character `CLOUDFLARE_TUNNEL_TOKEN`; `PUBLIC_BASE_URL=https://cxforge.hankholcomb.com`
resolves to Cloudflare anycast; the connector registers 4 QUIC connections
(`ewr01/08/11/12`).

`GET http://cloudflared:2000/ready` used to be listed here as a fourth
supporting fact. It has been removed, because it is **not evidence of anything**
— it returned `{"status":200,"readyConnections":4}` throughout a later 7.5-minute
total outage. See "`/ready` and the dashboard are false greens" below.

---

## Why there is no `config.yml` in this directory

This is a **token-managed** (dashboard-managed) named tunnel: `cloudflared`
receives its routing configuration from Cloudflare along with the token, and
**ignores a local `ingress:` block entirely** in that mode. A committed
`config.yml` here would look like configuration and configure nothing — the
same class of artifact as a docstring that describes behaviour the code does
not have, which `docs/STATE.md §2` catalogues at length.

So the committed configuration is this file plus the `cloudflared` service in
`deploy/docker-compose.yml`. The parts a file could not carry — the ingress
rule — are written down here and asserted by
`backend/tests/deploy/test_compose_topology.py`, so they cannot silently drift
away from what the owner was told to configure.

If the flow ever changes to a **locally-managed** tunnel (a `credentials-file`
plus a real `ingress:` block), that config belongs in this directory and the
service's `command` changes to `tunnel --config /etc/cloudflared/config.yml
run`. That would be a deliberate change to ADR-005, not a tidy-up.

## The ingress rule (OA-3 step 3)

Set by the owner; recorded here because the routing lives in the Cloudflare
dashboard, where this repo cannot assert on it.

| Field | Value |
|---|---|
| Tunnel name | `cxforge` |
| Public hostname | `cxforge.hankholcomb.com` |
| Service type | `HTTP` |
| **URL** | **`portal:80`** |

`portal:80` is the compose **service name and container port**, resolved by
`cloudflared` on the `othram-deploy` network. It is not the droplet's published
`${PORTAL_PORT}` (8080) and not an IP. That is the whole point of the tunnel:
**no inbound port on the droplet is opened**, and the origin is only reachable
from inside the compose network.

**Not `backend:8000`, which is what this file and OA-3 originally specified.**
The `portal` container's nginx (`deploy/portal/nginx.conf`) serves the built SPA
at `/` and proxies `/api/`, `/webhooks/` and `/health` to `backend:8000` on the
same network. So `portal:80` gives **one hostname for everything** — UI, API and
the Zendesk webhook — where `backend:8000` gives an origin that serves the API
and cannot serve the UI at all. The only reason two hostnames were ever on the
table is that this page said `backend:8000` before anyone had run it.

Two things the change does **not** touch:

- **The Zendesk webhook URL.** Still `${PUBLIC_BASE_URL}/webhooks/zendesk`.
  Checked *before* the change was recommended, not after, because the HMAC
  signature is computed over the **raw body** and a proxy that re-serialised it
  would break every webhook silently: a payload signed with the server's own
  `ingress.signature.compute_signature` returned **202 through nginx** and
  **202 direct to the backend**.
- **ADR-005's guarantee.** No droplet port is published to the internet either
  way; only the container port the connector dials changes.

### History: the 502, and why the fix was not `HTTP → backend:8000`

For several hours the hostname returned **502**. Cloudflare was pushing down
`service: https://backend:8000` — an `https://` origin at plain-HTTP uvicorn.
From inside the compose network:

```
curl    http://backend:8000/health  ->  200
curl -k https://backend:8000/health ->  000   curl: (35) TLS connect error:
                                               error:0A00010B:SSL routines::wrong version number
```

The narrow repair was to flip the dashboard's Service **type** to `HTTP`; the
owner repointed the **URL** to `portal:80` in the same edit, which fixes the
scheme mismatch and collapses UI, API and webhook onto one hostname. Full record:
`docs/OWNER-ACTIONS.md` OA-3 and `docs/BUILD-PLAN.md` §10.6.

Recorded so it is not retried from this directory: there is no Cloudflare **API**
token on the build machine (`CLOUDFLARE_TUNNEL_TOKEN` is not one), and a
connector started with `run --url http://backend:8000` logs that `--url` and
then immediately overrides it with the remote configuration — remote
configuration wins for a token-managed tunnel, exactly as the section above says
it does.

## The environment contract

Both are declared in `.env.example` and both are **empty there** (they are
populated in the real, gitignored `.env`):

| Variable | Read by | Default |
|---|---|---|
| `CLOUDFLARE_TUNNEL_TOKEN` | `deploy/docker-compose.yml`, passed to the container as `TUNNEL_TOKEN` | **none** |
| `PUBLIC_BASE_URL` | `scripts/verify_deploy.sh`'s public-path stage (from the operator's machine, never inside a container) | **none** |

`CLOUDFLARE_TUNNEL_TOKEN` has **no `:-` default** deliberately. A `:-` default
renders an empty string, and an empty token starts a `cloudflared` that serves
nothing while looking like it is up — the exact failure mode
`docs/STATE.md §6.2` describes for `ANTHROPIC_API_KEY`. With no default,
`docker compose` prints

```
warning: The "CLOUDFLARE_TUNNEL_TOKEN" variable is not set. Defaulting to a blank string.
```

and `cloudflared` exits non-zero at startup, so the failure is visible in
`docker compose ps` and in the container log.

**Open decision, now that OA-3 has landed.** `${CLOUDFLARE_TUNNEL_TOKEN}` could
become `${CLOUDFLARE_TUNNEL_TOKEN:?...}`, turning a missing token into a hard
failure at parse time instead of a warning. The original reason not to — that
it would block work on an owner action nobody had taken — is gone.

It is still not written that way, because the remaining consequence is a real
one and belongs to the owner, not to this file: `:?` fails interpolation for
the **whole file**, every service. Since `docker compose -f
deploy/docker-compose.yml …` does not read the repo-root `.env` (see below),
that would mean the production stack could no longer be parsed or started at
all except through `deploy/compose.sh` — including by
`scripts/verify_deploy.sh --local`. That is arguably the right trade, because
it makes the source-your-env trap structurally impossible for this stack. It
is a change to how every service behaves, so it is W3-G3's call to make
deliberately, not a tidy-up.

`PUBLIC_BASE_URL` is not forwarded into any container, because no container
reads it. It is the single place the public origin is recorded, and the
Zendesk webhook endpoint is `${PUBLIC_BASE_URL}/webhooks/zendesk`.

What **does** read it is `scripts/verify_deploy.sh`, on the operator's machine:
since 2026-08-17 its public-path stage samples this hostname (20x `GET /health`
→ 200 and 20x unsigned `POST /webhooks/zendesk` → 401) and fails the run with a
rate when the transport is only partly serving. That is the automated form of
the read-back described below — before it existed, every assertion in that
script went to the droplet's own port and none of them could fail while this
tunnel was down.

## Bringing it up

```bash
# On the droplet, from the repo root. The source step is not optional:
# without it every ${VAR} in the compose file falls back to its default and
# the stack deploys with no credentials and a `dev-portal-token`.
set -a; source .env; set +a
docker compose -f deploy/docker-compose.yml up -d --build --wait

# or, equivalently and without the chance to forget the source step:
deploy/compose.sh up -d --build --wait
```

## Reading the effect back — the only thing that counts as "it works"

There is exactly one check that is evidence: **a request from OUTSIDE the
droplet that reaches the app.**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "$PUBLIC_BASE_URL/health"     # expect 200
```

A 200 there means the hostname resolves, Cloudflare terminates TLS, the tunnel
is connected, **the edge is routing to a connection that is actually alive**, and
`portal:80` answered. Nothing less than that should be written down as "the
tunnel is up".

Everything else is a diagnostic, not evidence. `docker compose ps` showing
`running` proves the process started; `restart: unless-stopped` will happily
restart a container that is failing. The logs
(`docker compose -f deploy/docker-compose.yml logs cloudflared | tail -30`) are
worth reading *after* the public check has already failed.

### `/ready` and the dashboard are false greens

`GET http://cloudflared:2000/ready` returning `{"status":200,"readyConnections":4}`
and a HEALTHY connector in the Cloudflare dashboard are both fully compatible
with a **total** outage, and were measured that way on 2026-08-17: four
registered connections, config v3 loaded, every precheck PASS — while every
request returned 502 for 7.5 straight minutes and
`cloudflared_tunnel_total_requests` stayed at **0**. The 502s never reached this
connector; Cloudflare's edge was still routing to a *previous* connector's dead
connections, because each `cloudflared` start mints a new connector ID. Full
diagnosis and the three independent proofs: `docs/BUILD-PLAN.md §10.6g`.

The operational consequence, which is live for Wave 4: this service has
`restart: unless-stopped` and **no healthcheck** (the image ships no shell or
curl, so a container-local probe is not expressible), so a restart can leave the
tunnel non-serving for minutes with every instrument reporting healthy. A clean
`up -d --force-recreate` recovers it in ~3 seconds. The practical rule is
therefore: **do not restart `cloudflared` near an ADR-015 scenario run, and
verify publicly after any deploy.** A sidecar probe or an external monitor is an
owner decision, not something to add in passing.

**Run 2026-08-17 (W3-G3): the public check passed** — the responses are at the
top of this file. The distinction this section draws is the one that has now
mattered twice. Both times the cheap checks were green while the public URL
returned 502: first the dashboard ingress rule's scheme (history section above),
then the dead-connection routing described here.
