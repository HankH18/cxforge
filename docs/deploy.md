# Deploying to DigitalOcean (T-11)

**Status as of this writing: this project's droplet exists at
`161.35.2.250`, and `DEPLOY_HOST` in `.env` is set to it.** The stack is
live there and passes `scripts/verify_deploy.sh` in REMOTE mode, 4/4.
`doctl` is authenticated on this machine, and there are two *other*
droplets on the account (`ubuntu-s-2vcpu-4gb-nyc1`, `trainerforge`) — both
pre-existing, unrelated to this project, and never to be touched by
anything in this repo. Everything below is the **procedure that was
followed** to create and verify this one; it is still the procedure to
follow for a redeploy or a second droplet.

What has been verified: the production stack (`deploy/docker-compose.yml`)
builds and runs both locally — `bash scripts/verify_deploy.sh --local`
passes (`DEPLOY_HOST` empty + explicit `--local` opt-in → local path; the
flag is required, and a bare invocation with an empty `DEPLOY_HOST` now
hard-fails by design, see §7) — and on the droplet, where a bare
`bash scripts/verify_deploy.sh` passes REMOTE mode. Separately, a live
`client.messages.parse()` call against `claude-opus-5` has succeeded from
inside the running backend container, confirming the model credential
reaches the app. See that script and `deploy/docker-compose.yml`'s own
header comments for exactly what each proves and what it doesn't.

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
- `ANTHROPIC_API_KEY` — the client is constructed lazily, so an empty
  value still starts and serves `/health` (see
  `backend/src/agent/llm.py`'s lazy-client-construction docstring). It is
  **not optional for real work**: with it unset the first reply generation
  fails, so a deploy meant to answer tickets must set it. This was
  `OPENAI_API_KEY` before the provider pivot; the backend no longer reads
  that name at all, and `deploy/docker-compose.yml` forwards the Anthropic
  one instead.
- `LANGFUSE_*` — same lazy story: absent means that integration is simply
  unused, not a crash. Note that no Langfuse instrumentation is wired in
  the code regardless (see `docs/demo-script.md`, Shot 8).
- `DEPLOY_HOST` — already set, to this project's droplet
  (`161.35.2.250`), so `scripts/verify_deploy.sh` (run from your local
  machine) targets it instead of the local stack.

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

**Precedence, exactly:** an already-exported `DEPLOY_HOST` in your shell
always wins over `.env` — the script captures it before sourcing `.env`
and restores it afterward, so `.env`'s own (often empty) `DEPLOY_HOST=`
line can never silently clobber a value you exported. `.env`'s
`DEPLOY_HOST=` line only takes effect when your shell does not already
have `DEPLOY_HOST` exported — i.e. either export it yourself, or edit
that line in `.env`, but an export always beats the file.

**Local mode is opt-in, not a fallback.** A bare `bash
scripts/verify_deploy.sh` with `DEPLOY_HOST` empty (the default —
`.env.example` ships it empty) is now a **hard failure** (non-zero exit,
`FAIL:` on stderr, no `PASS` printed) rather than a silent drop into the
local docker-compose check — a droplet-verification script that could
quietly "pass" against your own laptop was the exact bug T-17 closed. To
run the local-only check on purpose, pass `--local` explicitly:
`bash scripts/verify_deploy.sh --local`. Its success line is prefixed
`LOCAL-MODE PASS` (never a bare `PASS`) precisely so it can't be mistaken
for droplet evidence — only a REMOTE-mode run, with `DEPLOY_HOST` set,
satisfies T-11's droplet criterion, and only remote mode's `PASS` line
says `(REMOTE: verified droplet at ...)`.

**This has been run against the real droplet at `161.35.2.250` and
passed** — its `PASS` line reads `(REMOTE: verified droplet at
161.35.2.250)`. Both paths have now actually been executed and passed
here: the local one (`--local`, `DEPLOY_HOST` empty) and the remote one
against the live droplet.

One trap worth knowing before you redeploy: §5's
`set -a; source .env; set +a` is **required**, not decorative. Compose
resolves every `${VAR}` in `deploy/docker-compose.yml` from the shell
environment, and its project directory is `deploy/`, which has no `.env`
of its own. Skip the `source` and each variable silently falls back to its
compose default — the stack comes back up healthy on the literal
`dev-portal-token`, and `verify_deploy.sh` then fails assertion 4 with a
401. `PORTAL_TOKEN` is also a **build arg** for the portal image
(`VITE_PORTAL_TOKEN`), so changing it needs `--build`, not just a
recreate.

## 8. Cost control

A running droplet bills continuously. When you're done with the demo:

```bash
doctl compute droplet list                 # find the ID
doctl compute droplet delete <droplet-id>  # destroys it — irreversible
```

Nothing in this repo does this automatically, and nothing in this repo
creates the droplet automatically either — both are deliberate,
budget-affecting human actions.
