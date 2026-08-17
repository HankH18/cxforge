# Deploying to DigitalOcean (T-11)

> ## Status after W3-G3's redeploy — 2026-08-17
>
> The droplet at **`161.35.2.250`** (DO id `592687747`, name `cxforge`) now runs
> the **six**-service stack, not the original three: `db`, `backend`, `portal`,
> plus `redis` + `worker` (ADR-002) and `cloudflared` (ADR-005). Deployed by
> rsync (§3 Option B) + `scp .env` (§4) + `deploy/compose.sh up -d --build
> --wait`, which now exits **0** with all six containers up and five reporting
> `healthy` (`cloudflared` declares no healthcheck; this line used to say "probe
> its `:2000/ready` instead" and that advice was **wrong** — `/ready` reported
> `readyConnections: 4` through a 7.5-minute total outage. The only evidence the
> tunnel serves is `${PUBLIC_BASE_URL}/health` returning 200 from outside the
> droplet: `curl -sS -o /dev/null -w '%{http_code}\n' "$PUBLIC_BASE_URL/health"`.
> See `docs/BUILD-PLAN.md §10.6g`).
>
> **What works, read back:** liveness 4/4 in REMOTE mode; the core loop as far
> as `nodes.ingest` (signed webhook → 202 → real Redis → arq worker in 0.11s →
> `run_agent`); Anthropic `claude-opus-5` from inside the worker container;
> Langfuse keys resolving to project `cxforge`; `search_kb` returning graded
> hits from the seeded KB.
>
> **Two things blocked it at the time of that redeploy, neither of them in this
> repo. Both have since been cleared — this list is kept because the procedure
> below still refers to it:**
> 1. **`ZENDESK_OAUTH_TOKEN` is a JWT that lives ~25 minutes**, so every run
>    died at `ingest`'s `fetch_ticket` with 401 and `runs` was empty on the
>    droplet. **Superseded:** `scripts/zendesk_oauth.py --refresh` now rotates it
>    without a browser and a full run has completed on the droplet —
>    `docs/BUILD-PLAN.md §10.6(a)` and `§10.6(d)`, standing procedure in OA-4.
>    The remaining caveat is that a rotation does not survive a container
>    restart (`§10.7a`, undecided).
> 2. ~~**`https://cxforge.hankholcomb.com` is still 502**~~ — **FIXED
>    2026-08-17, OA-3 completed.** The dashboard ingress rule pointed at
>    `https://backend:8000`, i.e. TLS against a plaintext origin; it now points
>    at **`portal:80`**, whose nginx serves the SPA and proxies `/api/`,
>    `/webhooks/` and `/health` to `backend:8000`. Read back from outside the
>    droplet: **`GET /` 200** serving the portal UI, **`GET /health` 200**.
>    Evidence and the rest of the surface in `deploy/cloudflared/README.md`
>    ("VERIFIED AGAINST A LIVE TUNNEL, 2026-08-17") and
>    `docs/BUILD-PLAN.md §10.6(c)`.
>
> `docs/BUILD-PLAN.md §10.5` has the full evidence for the redeploy and §10.6 for
> the end-to-end run that followed it. §7 below has the corrected `--deep`
> procedure, including how to reach the droplet's Postgres.
>
> This page still describes the original three-service deploy in places. A full
> rewrite for the worker/queue/tunnel topology is **W5-J2**, not done here.

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
- `VOYAGE_API_KEY` — the embeddings key (ADR-008, `voyage-4-lite` @
  `output_dimension=1024`). Read directly by
  `backend/src/data/embeddings.py` through `httpx`, not the `voyageai`
  SDK. **Only needed when `KB_EMBEDDER=voyage`** — and then it is hard
  required: `VoyageEmbedder` refuses to build without it rather than
  quietly degrading to the lexical embedder. With the default
  `KB_EMBEDDER=hashing` an empty value changes nothing.
- `KB_EMBEDDER` — **the one variable on this page you must not change on
  its own.** It picks which embedder the KB is *seeded* with and which one
  *queries* are embedded with, and those are the same setting by design
  (`data.seed.seed_all` and `data.retrieval.search_kb` both resolve through
  `default_embedder()`). Vectors already in `kb_chunks` came from the
  previous embedder and are not in the same space as the new query
  vectors, so **flipping this without reseeding does not error — it returns
  confident, plausible, wrong passages.** Flip it and reseed the KB in the
  same maintenance window, with no worker consuming jobs
  (`docs/BUILD-PLAN.md §10.3`). Accepted values are `hashing` (the default,
  deterministic and fully offline) and `voyage`; anything else raises
  rather than falling back. It is deliberately *not* "use Voyage if
  `VOYAGE_API_KEY` is set" — this repo's `.env` carries the key, and that
  rule would put the offline test suite on the network.
- `LANGFUSE_*` — same lazy story: absent means that integration is simply
  unused, not a crash. Since W2-C1 (ADR-006) the instrumentation is real —
  `agent.llm.emit_trace` reports the `trace_id` that `agent.nodes.act`
  mints, and the portal's trace link resolves (`307 → 200`) — so with keys
  absent the code degrades to a no-op that never imports `langfuse`, and
  the portal's trace links point at nothing. Set `LANGFUSE_HOST` too: its
  default is the EU region while the `cxforge` project is on
  `us.cloud.langfuse.com`, and an unset host builds a syntactically valid
  link into the wrong region rather than failing.
- `DEPLOY_HOST` — already set, to this project's droplet
  (`161.35.2.250`), so `scripts/verify_deploy.sh` (run from your local
  machine) targets it instead of the local stack.

## 5. Running the stack

On the droplet, from the repo root:

```bash
deploy/compose.sh up -d --build --wait
```

`deploy/compose.sh` is `docker compose -f deploy/docker-compose.yml` with the source step
already built in, and **it is the supported path.** The long form below still starts the
same containers, but since 2026-08-17 it is **no longer equivalent** — it has no guard:

```bash
set -a; source .env; set +a
docker compose -f deploy/docker-compose.yml up -d --build --wait
```

The wrapper refuses to start `cloudflared` — named, or implied by an `up` with no service
list — from a machine that does not hold the address this repo deploys to. Raw
`docker compose` does not, and this is precisely the form a human copies onto a laptop:
`CLOUDFLARE_TUNNEL_TOKEN` names the **tunnel, not the host**, so a `cloudflared` started
anywhere with it joins the live tunnel as a second connector. On 2026-08-17 that took the
public site to 10/10 × 502 and `docker stop` did not restore it — recovery was a
force-recreate on the droplet (`deploy/cloudflared/README.md`, BUILD-PLAN §10.6g/§10.7d).
Use the wrapper.

**Do not run the second command without the first line.** `docker compose` reads `.env`
from the directory holding the compose file — here `deploy/`, which has no `.env`.
Measured 2026-08-16: plain `docker compose -f deploy/docker-compose.yml config` renders
`ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, all four `ZENDESK_*` and both `LANGFUSE_*` keys as
the **empty string**, `PORTAL_TOKEN` as the literal `dev-portal-token`, and `KB_EMBEDDER`
as the literal `hashing` — from a repo whose `.env` has every one of them populated.
Nothing fails. It deploys a stack that cannot answer a ticket, and — because
`KB_EMBEDDER`'s fallback is a *value*, not an empty string — one that silently retrieves
lexically no matter what you set. (The *root* `docker-compose.yml` does pick `.env` up
automatically, because its project directory is the repo root — so the trap applies to
exactly the stack that matters and not to the one you test with.)

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
KB chunks.

**`SEED_ON_START`'s semantics changed on 2026-08-16 and the sentence that used
to follow here was stale.** It said to set `SEED_ON_START=false` on a restart of
an already-live deploy. That is no longer necessary, and the reason matters:
`true` (the default, and what a droplet actually gets, since `.env` has no
`SEED_ON_START` line) now means *create the schema, then seed `cases`/`kb_chunks`
**only if both tables are empty***. `false` is schema-only. **`force`** is the
old unconditional behaviour, and it `TRUNCATE`s both tables — only do that with
no worker running, or a retrieval in flight sees an empty knowledge base. No
mode ever touches `runs`/`drafts`/`settings`, and an unrecognised value means
`true`, so a typo cannot truncate anything. See
`deploy/backend/bootstrap.py`'s docstring and
`backend/tests/deploy/test_bootstrap_seeding.py`.

Read the decision back rather than assuming it; the bootstrap says which branch
it took. From the W3-G3 redeploy of an already-seeded droplet:

```
[bootstrap] schema ready; NOT seeding — 30 cases and 44 kb chunks are already present.
            seed_all() TRUNCATEs both tables, and the worker may be mid-run against them.
            Set SEED_ON_START=force to reload the fixtures anyway.
```

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

- If you rotate `PORTAL_TOKEN`, you must **rebuild the portal image** — changing the
  backend's env var alone does not change what is already baked into the shipped JS:
  ```bash
  deploy/compose.sh up -d --build portal
  ```
  Use the wrapper, or `set -a; source .env; set +a` first. Running
  `docker compose -f deploy/docker-compose.yml up -d --build portal` on its own bakes
  **`VITE_PORTAL_TOKEN=dev-portal-token`** into the bundle — so the rotation silently
  produces a portal authenticating with the default token.
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

**`--local` never starts `cloudflared`.** It brings up `db redis backend
worker portal` by name, because a `docker compose up` with no service list
starts *every* service in `deploy/docker-compose.yml` — and a `cloudflared`
started off the droplet joins the **live** tunnel as a second connector,
which is the 2026-08-17 outage (10/10 public 502; `docker stop` did not
restore service). The tunnel cannot help verify a stack on this machine
anyway: the assertions run against `127.0.0.1`, which does not go through
Cloudflare. Consequently a `--local` run says nothing about the public path
and states that on its own `PUBLIC PATH:` and `SCOPE:` lines. Bound by
`backend/tests/deploy/test_local_mode_opt_in.py`.

**This has been run against the real droplet at `161.35.2.250` and
passed** — its `PASS` line reads `(REMOTE: verified droplet at
161.35.2.250)`. Both paths have now actually been executed and passed
here: the local one (`--local`, `DEPLOY_HOST` empty) and the remote one
against the live droplet.

### The public path: the address Zendesk actually uses

**Every assertion described above — and `--deep` below — targets
`${DEPLOY_SCHEME}://${DEPLOY_HOST}:${DEPLOY_PORT}`, the droplet's own
published port. Zendesk cannot reach that address.** It reaches this app only
through `PUBLIC_BASE_URL` (`https://cxforge.hankholcomb.com`), and a request
to the droplet port bypasses Cloudflare entirely. On 2026-08-17 the public
path returned **502 for ~64% of real Zendesk deliveries** — Cloudflare's edge
routing to a prior connector's dead connections, the requests never reaching
the droplet at all — and throughout that outage this script would have
reported 4/4 and `--deep` would have passed (`docs/BUILD-PLAN.md §10.6g`).

The **public-path stage** closes that. It runs **by default in remote mode**
whenever `PUBLIC_BASE_URL` is set — no flag, because a check that only fires
when somebody remembers a flag would have been absent for exactly the
incident that motivated it:

```bash
bash scripts/verify_deploy.sh              # includes the public path
bash scripts/verify_deploy.sh --public     # …and REQUIRES it (see below)
CXFORGE_PUBLIC_SAMPLES=40 bash scripts/verify_deploy.sh   # more samples
```

What it does, and why each part:

- **It samples** — 20 times per probe by default, one `curl` process each so
  no two samples share a TCP connection or a Cloudflare edge connection. The
  failure it exists to catch is probabilistic and varies by colo: at the
  measured 64% failure rate, a single request misses the outage 36% of the
  time and 20 requests miss it 1.3e-9 of the time. It reports the **rate**,
  e.g. `GET /health -> 7/20 = 35.0% [502 x13, 200 x7]`, and any sample
  missing its expected status fails the run.
- **It probes the Zendesk endpoint, not just `/health`.** `POST
  /webhooks/zendesk` with an **unsigned** body must answer **401**. That is a
  pure read — ingress verifies the HMAC before it touches the body, the
  database or the queue — and a 401 is positive proof the request reached the
  application, where a `502`/`530`/`000` proves it did not. Checking only
  `/health` would leave a per-path Cloudflare rule or a mis-pointed public
  hostname invisible.
- **An unset `PUBLIC_BASE_URL` skips loudly** and says so again on the
  `SCOPE:` line — never a silent pass. `--public` turns that skip into a hard
  failure before any request goes out, the way `--deep` treats a missing
  signing secret. `--public` also forces the stage in `--local` mode, where it
  is otherwise skipped (the public hostname fronts the droplet, not your
  laptop).
- **`CXFORGE_PUBLIC_SAMPLES` has an enforced floor of 4**, and a
  loopback `PUBLIC_BASE_URL` is labelled `SIMULATED` on both the pass line and
  the `SCOPE:` line. Both exist because the two ways to neuter this stage back
  into a false green are sampling once and pointing it at something local that
  always answers.

`PUBLIC_BASE_URL` follows the same precedence rule as `DEPLOY_HOST`: an
export from your shell wins over `.env`, so `.env`'s empty
`PUBLIC_BASE_URL=` line cannot clobber it and silently turn the stage into a
skip.

**Measured, both directions, 2026-08-17.** Against a local server returning
502 for 64% of requests the run went red with
`GET /health 9/20 = 45.0% [502 x11, 200 x9]`; against the real hostname it
passed `20/20 = 100.0%` on both probes. A check that has never been seen to
fail is the thing this whole section is about.

To drive the core loop **through the public hostname** in one run — the
strongest check available, since the public origin serves the portal, `/api/*`
and `/webhooks/*` (measured 2026-08-17):

```bash
DEPLOY_SCHEME=https DEPLOY_HOST=cxforge.hankholcomb.com DEPLOY_PORT=443 \
  CXFORGE_VERIFY_TICKET_ID=<disposable-ticket-id> \
  bash scripts/verify_deploy.sh --deep
```

### `--deep`: the only assertion that can fail when the product is broken

Read this before quoting a `PASS` from the four assertions above. **Every
one of them is a liveness check.** Not one makes a model call, writes a
row, or touches the agent path — which is why this script reported 4/4 for
weeks against a stack with no `ANTHROPIC_API_KEY` at all, and why it still
reports 4/4 against the droplet today, whose webhook accepts events and
never starts a run (`docs/STATE.md §6.2`). A pass without `--deep` now
prints a `SCOPE:` line saying exactly that, on the line after the `PASS`,
so the scope travels with the claim.

Every `PASS` is followed by a three-line `SCOPE:` block, because a pass can
be shallow in two unrelated directions and one line could only say one of
them:

```
SCOPE: liveness only. The core loop … was NOT exercised. …        ← WHAT ran
SCOPE: PATH ASSERTED — http://161.35.2.250:8080. Zendesk cannot
       reach that address; it bypasses Cloudflare.                ← WHERE it ran
SCOPE: PUBLIC PATH (https://cxforge.hankholcomb.com, the only
       route Zendesk has) — CHECKED, all green — …
       GET /health 20/20 = 100.0% … POST /webhooks/zendesk 20/20 … ← the real route
```

A green core loop on the droplet port with a 64%-broken public path is a
state this deployment has actually been in, so the two questions are answered
separately and neither answer can be quoted as the other.

`--deep` (W3-G2) POSTs a correctly HMAC-signed synthetic webhook at the
real endpoint and then waits for the **effect**: a NEW `runs` row, read
back through the deployed portal API. It exercises ingress → Redis → the
arq worker → `run_agent` for real, so it is the first check in this
project that fails when the core loop is severed.

```bash
CXFORGE_VERIFY_TICKET_ID=<disposable-ticket-id> \
CXFORGE_VERIFY_DB_URL=postgresql://…            # optional; see below \
  bash scripts/verify_deploy.sh --deep
```

- **`CXFORGE_VERIFY_TICKET_ID` is required and cannot be invented.**
  `agent.nodes.ingest`'s first statement is `port.fetch_ticket()`, so a
  made-up id 404s and the run dies before any `runs` row exists — the
  check would then fail identically whether or not the core loop is
  connected, which is the exact ambiguity it exists to remove. Point it at
  a **disposable** ticket the deployed agent can really fetch, and expect
  the agent to post a real public reply on it. Per-invocation uniqueness
  comes from a fresh `comment_id` plus a baseline snapshot of existing run
  ids, not from this value, so the same ticket is meant to be reused.
- **`--deep` is not read-only.** It spends model tokens and writes rows.
- **`CXFORGE_VERIFY_DB_URL` is optional and is never defaulted from
  `DATABASE_URL`.** It names the database of the *deployment under test*,
  and is used only to delete the rows the check itself created and to
  print the raw `runs` row it asserted on. In REMOTE mode `DATABASE_URL`
  names your own dev Postgres on `localhost`, so a fallback would aim the
  cleanup `DELETE`s at a completely different database. The droplet
  publishes no host port for Postgres, so leave it empty there: the check
  then prints the exact SQL and says out loud that it left its rows
  behind.
- **Missing preconditions are hard failures**, checked before the first
  request goes out. A skipped check that still exits 0 would be the same
  lie the four liveness assertions told for weeks.

Measured 2026-08-17: against the droplet, assertions 1–4 pass and `--deep`
**fails** — "no new runs row … after 240.9s" — because `161.35.2.250` still
runs the pre-Wave-1 image. That is the check working, not the check being
broken. W3-G3 is the redeploy that should turn it green.

**Re-measured 2026-08-17 after W3-G3's redeploy. It did not turn green, and the
reason is worth reading before you assume the deploy is broken.** Assertions
1–4 still pass; `--deep` still fails with "no new runs row for ticket 3 after
241.5s". But the droplet now runs the Wave-2 image and the loop is genuinely
connected: the signed webhook returned `202 {"duplicate":false}`, the arq worker
dequeued the job in **0.11 s**, and `run_agent` reached `nodes.ingest` — whose
`fetch_ticket` got **401 `invalid_token`** from Zendesk, at which point ADR-003
released the dedup row and the run ended with no `runs` row. The
`ZENDESK_OAUTH_TOKEN` is a JWT that **expires about 25 minutes after issue**; it
answered 200 twenty-five minutes earlier and expired 2m23s before the worker
used it. So `--deep`'s failure message names the right symptom and the wrong
cause here — check the worker's ERROR log, which is unambiguous, and see
`docs/BUILD-PLAN.md §10.5` and `docs/OWNER-ACTIONS.md` OA-4.

Two consequences for anyone running `--deep` against the droplet:

- **Re-authorize immediately before the run, not before the build.** `--deep`
  polls for up to 240s; a token issued 20 minutes earlier will expire mid-check.
- **`CXFORGE_VERIFY_DB_URL` can be satisfied** even though the droplet publishes
  no host port for Postgres, by tunnelling to the `db` container over SSH:
  ```bash
  ssh -f -N -L 15432:"$(ssh root@161.35.2.250 \
      'docker inspect -f "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" othram-deploy-db')":5432 \
    root@161.35.2.250
  CXFORGE_VERIFY_TICKET_ID=<disposable-ticket-id> \
  CXFORGE_VERIFY_DB_URL=postgresql://othram:othram@127.0.0.1:15432/othram \
    bash scripts/verify_deploy.sh --deep
  ```
  Port 15432, not 5432, so it can never be confused with the dev Postgres that
  `DATABASE_URL` names. Verified 2026-08-17: the tunnelled connection reported
  `inet_server_addr = 172.18.0.2` and 44 `kb_chunks`, i.e. the droplet's
  database and not the developer's. Close it afterwards.

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
