# Cloudflare named tunnel — the committed configuration

**Status: UNVERIFIED AGAINST A LIVE TUNNEL.** Written 2026-08-16 for work
package W1-F2 (ADR-005).

**OA-3 has since been completed by the owner**, so the earlier version of this
paragraph ("no tunnel exists, the token is absent") is out of date. What is
true as of 2026-08-16, measured rather than assumed:

| Claim | Evidence |
|---|---|
| The token exists | `.env` holds a 184-character `CLOUDFLARE_TUNNEL_TOKEN` |
| The hostname exists | `PUBLIC_BASE_URL=https://cxforge.hankholcomb.com`; `dig @1.1.1.1 cxforge.hankholcomb.com` → Cloudflare anycast (`172.67.136.113`, `104.21.7.150`) |
| The edge answers | `curl https://cxforge.hankholcomb.com/health` → **`HTTP/2 502`, `server: cloudflare`** |
| The compose file parses | `docker compose -f deploy/docker-compose.yml config` → exit 0 |

The 502 is the **correct and expected** answer right now: Cloudflare terminates
TLS and then fails to reach an origin, because the `cloudflared` service below
has never been started and the droplet has not been redeployed. It is evidence
that DNS and the edge are wired; it is **not** evidence that this container
works, that the tunnel connects, or that `backend:8000` is reachable.

Nothing on this page has been run. The marker at the top stays until someone
performs the check in the last section and reads a `200` back.

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

## The ingress rule the owner must set (OA-3 step 3)

Completed by the owner; recorded here because the routing lives in the
Cloudflare dashboard, where this repo cannot assert on it.

| Field | Value |
|---|---|
| Tunnel name | `cxforge` |
| Public hostname | `cxforge.hankholcomb.com` |
| Service type | `HTTP` |
| **URL** | **`backend:8000`** |

`backend:8000` is the compose **service name and container port**, resolved by
`cloudflared` on the `othram-deploy` network. It is not the droplet's
published `${BACKEND_PORT}` and not an IP. That is the whole point of the
tunnel: **no inbound port on the droplet is opened**, and the origin is only
reachable from inside the compose network.

## The environment contract

Both are declared in `.env.example` and both are **empty there** (they are
populated in the real, gitignored `.env`):

| Variable | Read by | Default |
|---|---|---|
| `CLOUDFLARE_TUNNEL_TOKEN` | `deploy/docker-compose.yml`, passed to the container as `TUNNEL_TOKEN` | **none** |
| `PUBLIC_BASE_URL` | nothing yet — it is where the hostname is written down | **none** |

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

## Bringing it up (W3-G3 — not done)

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

`docker compose ps` showing `running` proves the process started, not that the
tunnel carries traffic. `restart: unless-stopped` will happily restart a
container that is failing. Three checks, in increasing order of what they
prove:

```bash
# 1. cloudflared's own readiness endpoint (metrics port, container-internal).
#    Run from the droplet; the image ships no shell, so probe it from outside
#    the container.
docker compose -f deploy/docker-compose.yml logs cloudflared | tail -30

# 2. The tunnel is registered: the Cloudflare dashboard shows the connector
#    HEALTHY with active connections.

# 3. The only one that is real: a request from OUTSIDE the droplet that
#    reaches the app.
curl -sS -o /dev/null -w '%{http_code}\n' "$PUBLIC_BASE_URL/health"     # expect 200
```

Check 3 passing means the hostname resolves, Cloudflare terminates TLS, the
tunnel is connected, and `backend:8000` answered. Nothing less than that
should be written down as "the tunnel is up", and none of it has been run.
