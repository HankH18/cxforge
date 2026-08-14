# Deploying to DigitalOcean (T-11)

**Status as of this writing: no droplet exists for this project, and
`DEPLOY_HOST` in `.env` is empty.** `doctl` is authenticated on this
machine and there are two droplets on the account (`ubuntu-s-2vcpu-4gb-nyc1`,
`trainerforge`) — both pre-existing, unrelated to this project, and never
to be touched by anything in this repo. Creating a new droplet costs real
money and is the human's decision to make, not something a build script
does on its own. Everything below is the **procedure to follow when you're
ready to create one** — not something that has already happened.

What *has* been verified, on this machine, without any droplet: the whole
production stack (`deploy/docker-compose.yml`) builds and runs, and
`bash scripts/verify_deploy.sh` passes against it locally (`DEPLOY_HOST`
empty → local path). See that script and `deploy/docker-compose.yml`'s own
header comments for exactly what "local" proves and what it doesn't.

---

## 1. Droplet sizing

Recommended: **`s-2vcpu-4gb`** (2 vCPU / 4 GB RAM / 80 GB SSD, ~$24/mo at
current DigitalOcean pricing — confirmed via `doctl compute size list`).

Why: the backend image is ~960 MB uncompressed (scikit-learn + scipy +
matplotlib + pillow are pulled in by `pyproject.toml`'s pinned dependency
set — T-0's scope, not something this ticket can trim), and building it
**on the droplet** (`docker compose ... up -d --build`, per this doc) means
`uv sync` resolving/installing that stack and `npm ci && vite build` for
the portal both need to fit in RAM alongside Postgres and the running
containers at the same time during the first deploy. `s-1vcpu-2gb` is
likely enough once images are built and just running steady-state, but is
tight for the build step itself — go with `s-2vcpu-4gb` unless you build
images elsewhere and `docker save`/registry-push them instead (see §7).

Image: **Ubuntu 24.04 (LTS) x64** — current, long support window, and
DigitalOcean's own Docker setup guides target it directly.

## 2. What to install on the droplet

```bash
# As root (or with sudo) on a fresh Ubuntu 24.04 droplet:
apt-get update
apt-get install -y ca-certificates curl git

# Docker Engine + the Compose plugin (docker compose v2), from Docker's
# own apt repo — the distro's own `docker.io` package lags too far behind
# for the `--wait` flag scripts/verify_deploy.sh depends on.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

docker compose version   # sanity check: v2.x
```

Optional, only if you go with the cloudflared-tunnel webhook option in §6:

```bash
curl -L --output cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
dpkg -i cloudflared.deb
```

## 3. Getting the repo onto the droplet

Any of these work; pick whichever matches where you're pushing this
project (see the repo's global git conventions for the actual remote —
not repeated here since it's not a deploy concern):

```bash
# Option A — clone directly on the droplet (needs the droplet to have
# credentials for whatever remote you use — an SSH deploy key is the
# usual choice, added via `ssh-keygen` on the droplet + adding the
# public key to the remote).
git clone <your-remote-url> othram-support-agent
cd othram-support-agent

# Option B — push a local clone up over SSH instead (no git credentials
# needed on the droplet at all):
rsync -az --exclude .venv --exclude node_modules --exclude .git \
  ./ root@<droplet-ip>:~/othram-support-agent/
```

## 4. Supplying the env file safely

**Never commit `.env`. Never bake it into an image** (neither
`deploy/Dockerfile.backend` nor `deploy/Dockerfile.portal` does — the
backend reads every secret from `docker compose`'s `environment:` block,
sourced from the shell's env at `up` time; see
`deploy/docker-compose.yml`'s header comment). The one deliberate
exception is `VITE_PORTAL_TOKEN` — see §5.

On the droplet, `.env` needs to exist at the repo root but is never part
of the git history or the Docker build context:

```bash
# From your local machine, copy the repo-root .env to the droplet over
# SSH — never through git, never through a Dockerfile COPY:
scp .env root@<droplet-ip>:~/othram-support-agent/.env
ssh root@<droplet-ip> chmod 600 ~/othram-support-agent/.env
```

Fill in on the droplet's copy (values you already have locally, per
`.env.example`):

- `POSTGRES_*` — can keep the dev defaults; this Postgres is private to
  the deploy stack (see §5's isolation note).
- `PORTAL_TOKEN` — the shared portal auth secret. Pick a real one for a
  public droplet, not the `dev-portal-token` default.
- `ZENDESK_SUBDOMAIN`, `ZENDESK_OAUTH_TOKEN`, `ZENDESK_AI_USER_ID`,
  `ZENDESK_WEBHOOK_SIGNING_SECRET` — from `docs/zendesk-runbook.md`.
  **Empty is fine to start with**: every read of these
  (`backend/src/ingress/__init__.py`, `backend/src/helpdesk/
  zendesk_adapter.py`) happens at request time, not at import/startup, so
  the app comes up and serves `/health` regardless — only the
  Zendesk-specific endpoints degrade (a 500 on the webhook until
  `ZENDESK_WEBHOOK_SIGNING_SECRET` is set; portal *approve* fails until
  the OAuth token is set) until the runbook is done.
- `OPENAI_API_KEY`, `LANGFUSE_*` — same story: absent means those
  integrations are simply unused, not a crash (see
  `backend/src/agent/llm.py`'s lazy-client-construction docstring).
- `DEPLOY_HOST` — set this once the droplet exists, to its public IP or
  domain, so `scripts/verify_deploy.sh` (run from your local machine)
  targets it instead of the local stack.

## 5. Running the stack

On the droplet, from the repo root:

```bash
set -a; source .env; set +a
docker compose -f deploy/docker-compose.yml up -d --build --wait
```

This is exactly what `scripts/verify_deploy.sh` does in local mode, minus
the teardown at the end — the whole point of a real deploy is to leave it
running. `deploy/docker-compose.yml`'s header comment explains the
isolation this stack keeps from the root `docker-compose.yml` (T-0,
dev-only): its own compose project name (`othram-deploy`), its own
container names, its own named Postgres volume, and `db` never publishes a
host port at all — nothing here can collide with or clobber a dev
Postgres, on this droplet or anywhere else.

By default the backend is reachable on host port `8000` and the portal
(which also reverse-proxies `/api/` and `/webhooks/` to the backend — see
`deploy/portal/nginx.conf`) on `8080`. Override with `BACKEND_PORT`/
`PORTAL_PORT` env vars if those collide with anything else on the droplet.
For a real public deploy, put the portal's port behind the droplet's
firewall as the *only* open HTTP port (plus SSH) — everything the app
needs (the API, the webhook) is already reachable through it via the nginx
proxy, so the backend's own port doesn't need to be internet-facing.

On first boot, `deploy/backend/entrypoint.sh` runs
`deploy/backend/bootstrap.py` before starting uvicorn: it creates the DB
schema and (by default, `SEED_ON_START=true`) loads the fixture
cases/KB content, so the portal feed and KB grounding have real content
immediately — confirmed locally: a fresh local run seeded 30 cases and 44
KB chunks. Set `SEED_ON_START=false` on a restart of an already-live,
already-in-use deploy to skip re-seeding `cases`/`kb_chunks` (it never
touches `runs`/`drafts`/`settings`, so this is about avoiding unnecessary
work on restart, not data safety).

### The `VITE_PORTAL_TOKEN` build-time caveat

The portal is a static site — Vite inlines every `VITE_`-prefixed env var
into the built JS bundle at `vite build` time
(`portal/src/api.ts`: `import.meta.env.VITE_PORTAL_TOKEN`). There is no
runtime env for a static file to read. That is why
`deploy/docker-compose.yml` passes it as a Docker **build arg**
(`build.args.VITE_PORTAL_TOKEN`, set from `${PORTAL_TOKEN}`), not a
runtime `environment:` entry — a runtime env var on the `portal` container
would do nothing, because the JS was already built by then.

**Real security implication: the token ships in cleartext inside the
built JS bundle**, readable by anyone who loads the portal page (view
page source, no auth required to fetch it). That's an acceptable tradeoff
*only* because SPEC's own non-goals scope this to "no real portal auth
(single shared token)" for a demo deployment — it would not be acceptable
for a token guarding anything beyond this project's single-shared-secret
portal. Two consequences worth knowing before you deploy:

- If you rotate `PORTAL_TOKEN`, you must **rebuild the portal image**
  (`docker compose -f deploy/docker-compose.yml up -d --build portal`) —
  changing the backend's env var alone does not change what's already
  baked into the shipped JS.
- Don't put a droplet running this portal anywhere the demo audience
  isn't fully trusted with API access — the "auth" is discoverable by
  design.

## 6. Pointing the Zendesk webhook at the droplet

Zendesk requires an **HTTPS** endpoint for webhooks (this is also why
`docs/zendesk-runbook.md`'s dev setup uses `cloudflared` rather than a
bare `http://localhost:8000` URL) — a droplet serving plain HTTP on 8080
is not, by itself, a valid webhook target. Two ways to close that gap,
in order of effort:

**Option A — cloudflared on the droplet (fastest, no domain needed).**
Same tool the dev runbook already uses, just run persistently on the
droplet instead of a laptop:

```bash
cloudflared tunnel --url http://localhost:8080 &
# prints https://random-words-1234.trycloudflare.com — use THIS in Step 6
# of docs/zendesk-runbook.md, with /webhooks/zendesk appended, e.g.:
# https://random-words-1234.trycloudflare.com/webhooks/zendesk
```

Run it under a process supervisor (`systemd`, or even a `tmux`/`screen`
session survives a disconnect) so it stays up for the demo. The tunnel
URL changes if `cloudflared` restarts — re-check Zendesk's webhook
**Endpoint URL** field if that happens.

**Option B — a real domain + TLS (more permanent, more setup).** Point a
domain's `A` record at the droplet's IP, then front the stack with a TLS
terminator — e.g. run `certbot`/`caddy` on the droplet, or add a `caddy`
(or `nginx` + `certbot`) service to `deploy/docker-compose.yml` listening
on 80/443 and reverse-proxying to the `portal` service on 8080. Not
implemented here (SPEC's hosting line only commits to "cloudflared tunnel
in dev; single DigitalOcean droplet (docker-compose) for the demo" — it
doesn't require a permanent domain), but this is the natural next step
past a demo.

Either way, once you have an HTTPS URL, follow `docs/zendesk-runbook.md`
Step 6 exactly as written, substituting that URL for the cloudflared-dev
one it shows.

## 7. Running the verification against the droplet

From your **local machine** (no docker needed locally for this — remote
mode is pure HTTP):

```bash
DEPLOY_HOST=<droplet-ip-or-domain> DEPLOY_PORT=8080 bash scripts/verify_deploy.sh
```

Or set `DEPLOY_HOST` (and `DEPLOY_PORT`/`DEPLOY_SCHEME` if not the
defaults) in your local `.env` and just run `bash scripts/verify_deploy.sh`
— the script sources `.env` automatically and switches to remote mode
whenever `DEPLOY_HOST` is non-empty. It runs the identical four
assertions the local path runs (see the script's own header comment for
exactly what they are), over the network, against whatever's already
running on the droplet — it never builds, starts, or stops anything
remotely.

**This has not been run against a real droplet in this environment** —
there is no droplet to point it at yet (see the status note at the top of
this document). Only the local path (`DEPLOY_HOST` empty) has actually
been executed and passed here.

## 8. Cost control

A running droplet bills continuously. When you're done with the demo:

```bash
doctl compute droplet list                 # find the ID
doctl compute droplet delete <droplet-id>  # destroys it — irreversible
```

Nothing in this repo does this automatically, and nothing in this repo
creates the droplet automatically either — both are deliberate,
budget-affecting human actions.
