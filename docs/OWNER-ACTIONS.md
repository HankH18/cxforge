# Owner actions — the things only Hank can do

Five items. Two of them gate whole parallel tracks, so do those first even though the
deadline pressure is on the Zendesk one.

Nothing here can be done from inside an agent session: each needs a browser consent, an
account signup, a purchase decision, or a camera.

> **Status 2026-08-17.** OA-1 ✅ · OA-2 ✅ · OA-3 ✅ · OA-4 ✅ · **OA-5 (the video) is the only
> item left.** Two optional decisions remain and are *not* actions: Voyage billing
> (`docs/BUILD-PLAN.md §10.3b`) and the reply-author identity (§10.7b). Re-measure with the
> **Quick status check** at the bottom before trusting any ✅ — this page has flipped from ✅ to
> ❌ overnight before.

---

## OA-1 — Voyage AI API key ⟵ do this first

**Gates:** Wave 2 Track B (embeddings, retrieval floor) — a whole track idles without it.

1. Sign up at <https://dash.voyageai.com> (free tier is ample for this KB).
2. Create an API key.
3. Add to `.env`:
   ```
   VOYAGE_API_KEY=pa-...
   VOYAGE_MODEL=voyage-4-lite
   VOYAGE_OUTPUT_DIMENSION=1024
   ```

> **`voyage-4-lite` at `output_dimension=1024`** — checked against Voyage's model
> reference on 2026-08-16. The `voyage-4` line takes a configurable output dimension
> (256 / 512 / 1024 / 2048); pinning 1024 matches the existing `EMBEDDING_DIM = 1024` and
> the `kb_chunks.embedding vector(1024)` column exactly, so this is a **reseed with no
> schema migration**.
>
> Avoid the `voyage-3` family — it is now legacy, and `voyage-3-lite` is fixed at 512
> dims, which would force a column change. Step up to `voyage-4` (non-lite) only if
> retrieval quality after the reseed is disappointing.

---

## OA-2 — Langfuse project and keys ✅ **DONE — verified 2026-08-16**

**Gates:** Wave 2 Track C (tracing) and demo shot 8. **UNBLOCKED.**

Verified by reading it back from the live API, not by trusting the paste:

```
public prefix: pk-lf-   secret prefix: sk-lf-   identical: no
GET /api/public/projects → {"name":"cxforge","organization":{"name":"hank-personal"}}
```

Both defects from `docs/STATE.md §3.1` are closed: the pair is a real pair, and it resolves
to the new **`cxforge`** project instead of personal `jarvis`. Demo traces will land clean.

**One caveat that has not changed and is not a problem with your key:** a garbage public key
plus the real secret *still* returns 200. Langfuse Cloud authenticates on the secret alone,
so `auth_check()` remains structurally incapable of proving a pair is correct. Keep checking
the prefixes and the resolved project name, never `auth_check()`.

<details><summary>Original instructions (kept for reference)</summary>

**The problem being fixed:** `.env` currently holds the **identical `sk-lf-…` value in
both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`** — no public key is set at all.
`auth_check()` returns `True` anyway (Langfuse Cloud accepts the secret alone on these
endpoints), which is exactly why this went unnoticed. The keys resolve to your personal
project **"jarvis"** under org **"hank-personal"**.

1. At <https://us.cloud.langfuse.com>, create a new project named **`cxforge`**.
2. Project settings → API keys → create a key pair.
3. Replace **both** values in `.env` — note the different prefixes:
   ```
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=https://us.cloud.langfuse.com
   ```
4. Confirm:
   ```bash
   set -a; source .env; set +a
   uv run python -c "
   import os
   from langfuse import Langfuse
   lf = Langfuse(public_key=os.environ['LANGFUSE_PUBLIC_KEY'],
                 secret_key=os.environ['LANGFUSE_SECRET_KEY'],
                 host=os.environ['LANGFUSE_HOST'])
   print('auth_check:', lf.auth_check())"
   ```
   Then check the key prefixes really differ:
   ```bash
   set -a; source .env; set +a
   echo "public: ${LANGFUSE_PUBLIC_KEY:0:6}   secret: ${LANGFUSE_SECRET_KEY:0:6}"
   # expect:  public: pk-lf-   secret: sk-lf-
   ```

</details>

---

## OA-3 — Cloudflare domain and named tunnel ✅ **DONE — verified live 2026-08-17**

> ### The tunnel is live and one hostname serves everything.
>
> Measured from the public internet, not from the dashboard:
>
> | `https://cxforge.hankholcomb.com` | Result |
> |---|---|
> | `/` | **200** — the portal UI |
> | `/health` | **200** |
> | `/api/*` | **401** without a token |
> | `/webhooks/zendesk` | **401** unsigned |
>
> **The fix was not the dropdown described below.** The owner repointed the tunnel's Service to
> **`portal:80`** — not `HTTP → backend:8000` — and the portal container's nginx proxies
> `/api/`, `/webhooks/` and `/health` to the backend while serving the SPA at `/`. No droplet
> port is exposed.
>
> **The Zendesk webhook URL did not change**, and that was verified *before* the change was
> recommended rather than after: a payload signed with the server's own `compute_signature`
> returned **202 through nginx** and **202 direct**, so a valid HMAC signature survives the
> proxy. Nothing in Admin Center needed touching.
>
> A real Zendesk comment has since travelled this path end to end — `docs/STATE.md §1`.

> ### ⚠️ SUPERSEDED by the banner above (kept: it is the record of a correct diagnosis with the wrong fix)
>
> ### The tunnel is up. The ingress rule says `https://backend:8000` and the origin is plain HTTP.
>
> **Measured during W3-G3, after the redeploy.** `cloudflared` is running on the droplet
> and connected — but `https://cxforge.hankholcomb.com/health` is **still 502**, and the
> cause is one dropdown in the Cloudflare dashboard, not anything in this repo.
>
> The configuration Cloudflare pushes down to the connector is in its own log:
>
> ```
> INF Updated to new configuration config="{\"ingress\":[
>       {\"hostname\":\"cxforge.hankholcomb.com\",\"service\":\"https://backend:8000\"},
>       {\"service\":\"http_status:404\"}],\"warp-routing\":{\"enabled\":false}}" version=1
> ```
>
> `https://`. Step 3 of the original instructions below says Service **`HTTP`** →
> `backend:8000`, and `deploy/cloudflared/README.md` records the same. The dashboard has it
> as HTTPS. `backend` is uvicorn with no TLS, so the connector's handshake fails and
> Cloudflare returns 502. Proven from inside the compose network, both schemes, same origin:
>
> ```
> curl http://backend:8000/health   ->  200
> curl -k https://backend:8000/health -> 000  curl: (35) TLS connect error:
>                                              error:0A00010B:SSL routines::wrong version number
> ```
>
> And the connector itself is healthy — this is not a tunnel that failed to come up:
>
> ```
> GET http://cloudflared:2000/ready
>   ->  {"status":200,"readyConnections":4,"connectorId":"32e954ff-1f92-4f8f-90c2-e8e094a18e1d"}
> ```
>
> **Correction, later on 2026-08-17: that inference does not hold.** `/ready` reporting
> `readyConnections: 4` is *not* evidence the tunnel is serving — it reported exactly this
> through a separate 7.5-minute total outage in which
> `cloudflared_tunnel_total_requests` stayed at **0**. It happens to have been true here,
> but the reasoning was invalid. `docs/BUILD-PLAN.md §10.6g`.
>
> **Fix (owner, dashboard, ~30 seconds).** Zero Trust → Networks → Tunnels → `cxforge` →
> Public Hostnames → `cxforge.hankholcomb.com` → change **Service type** from `HTTPS` to
> **`HTTP`** (URL stays `backend:8000`). Save. No redeploy is needed: the connector picks up
> the new configuration within seconds and logs another `Updated to new configuration`.
>
> **Why it was not fixed here.** The rule lives in the Cloudflare dashboard, not in this
> repo — this is a *token*-managed tunnel, so `cloudflared` ignores local ingress config.
> Two things were tried and are recorded so nobody repeats them: there is **no Cloudflare
> API token** anywhere on this machine (no `~/.cloudflared`, nothing in the keychain, only
> `CLOUDFLARE_TUNNEL_TOKEN` in `.env`, which is not an API credential); and a connector
> started with **`run --url http://backend:8000`** logs `Settings: map[... url:http://backend:8000]`
> and then immediately `Updated to new configuration ... "service":"https://backend:8000"`,
> and the endpoint stays 502. Remote configuration wins, as documented.
>
> **What this blocks:** the public hostname, and therefore the Zendesk webhook — which is
> already re-pointed at `https://cxforge.hankholcomb.com/webhooks/zendesk` (read back from
> the API, status `active`). Until this dropdown changes, no Zendesk event can reach the
> droplet. ~~It does **not** block `scripts/verify_deploy.sh`, which targets
> `http://161.35.2.250:8080` directly.~~
>
> **Corrected 2026-08-17.** That last sentence was written as reassurance and it
> described a blind spot. `verify_deploy.sh` targeting the droplet port *directly* is
> not a reason the outage was survivable — it is the reason the outage was **invisible
> to the gate**. Zendesk reaches this app only through `PUBLIC_BASE_URL`; a check that
> talks to `161.35.2.250:8080` cannot fail when that route is down, which is exactly
> what happened next: the public path returned **502 for ~64% of real deliveries**
> while the droplet port answered every request (§10.6g). "It does not block the
> verify script" was true and worthless.
>
> `scripts/verify_deploy.sh` now runs a **public-path stage** by default in remote
> mode: it samples `PUBLIC_BASE_URL` 20 times per probe (`GET /health` → 200 and an
> unsigned `POST /webhooks/zendesk` → 401, the endpoint Zendesk actually uses),
> reports the success **rate** rather than a boolean, fails the run on any miss, and
> skips loudly — never silently — when `PUBLIC_BASE_URL` is unset. `--public` makes it
> mandatory. So a repeat of this defect now turns the gate red. See
> `docs/deploy.md §7` and `docs/BUILD-PLAN.md §10.7e`.

**Gates:** Wave 1 F2, Wave 3 redeploy, and all of Wave 4. **UNBLOCKED.**

Verified end to end from the public internet, not from the dashboard:

```
dig +short cxforge.hankholcomb.com @1.1.1.1  →  172.67.136.113, 104.21.7.150   (Cloudflare anycast)
curl https://cxforge.hankholcomb.com/health  →  502
```

**The 502 is the correct answer and is not a defect.** It means Cloudflare's edge resolved
the hostname, found the tunnel configuration, and could not reach the origin — because
`cloudflared` exists only as committed config in the working tree and nothing is deployed.
~~It becomes 200 when W3-G3 redeploys the droplet.~~ Getting a 502 rather than a 1033/530 is
positive evidence the tunnel and hostname are wired correctly.

> **Corrected 2026-08-17 by W3-G3.** The sentence "it becomes 200 when W3-G3 redeploys the
> droplet" was wrong, and wrong in an instructive way: it predicted an effect instead of
> reading one back. The droplet has now been redeployed, `cloudflared` is running with 4
> ready connections, and the endpoint is **still 502** — for a completely different reason
> than the one this section describes (the ingress rule's scheme; see the banner above).
> A 502 is consistent with at least three distinct causes, which is exactly why it could not
> carry the claim "the tunnel and hostname are wired correctly" on its own.
>
> **Corrected again, same day.** It did not become 200 from the redeploy *or* from the scheme
> dropdown either: it took a **Service-type change to `portal:80`**. Two predictions in a row
> about what would turn this 200 were wrong; the thing that settled it was reading `/`,
> `/health`, `/api/*` and `/webhooks/zendesk` back through the hostname.

`CLOUDFLARE_TUNNEL_TOKEN` (184 chars) and `PUBLIC_BASE_URL=https://cxforge.hankholcomb.com`
are both in `.env`.

~~**Still to do, after W3-G3:** re-point the Zendesk webhook (Admin Center → Apps and
integrations → Webhooks) to `https://cxforge.hankholcomb.com/webhooks/zendesk`.~~
**Nothing left to do** — the webhook already pointed there, and the `portal:80` change did not
alter the URL (signature verified through the proxy; see the banner). What *was* missing was a
**trigger** to fire it — `docs/STATE.md §6.16`, now created.

<details><summary>Original instructions (kept for reference)</summary>

You said you'd handle the DNS. What the build needs from you is a **named tunnel token**
and the hostname it terminates at, so the runbook and compose config can be written
against a stable URL.

1. Cloudflare dashboard → Zero Trust → Networks → Tunnels → **Create a tunnel** →
   type *Cloudflared*. Name it `cxforge`.
2. Copy the **tunnel token** (the long `eyJ...` string).
3. Add a **public hostname** on that tunnel:
   - Subdomain/domain: whatever you choose, e.g. `cxforge.<your-domain>`
   - Service: `HTTP` → `backend:8000` (the compose service name — the tunnel container
     resolves it on the compose network, so **no droplet port is exposed**)
4. Add to `.env` on the droplet:
   ```
   CLOUDFLARE_TUNNEL_TOKEN=eyJ...
   PUBLIC_BASE_URL=https://cxforge.<your-domain>
   ```
5. Tell me the hostname — the Zendesk webhook endpoint becomes
   `https://cxforge.<your-domain>/webhooks/zendesk`, and it goes in the runbook.

**Why named and not a quick tunnel:** a quick tunnel's URL changes on every restart, which
would mean re-pasting the endpoint into Zendesk Admin Center before every take.

</details>

---

## OA-4 — Zendesk credential ✅ **SOLVED — 2026-08-17. Not a recurring chore.**

> ### Standing procedure — renewal needs no browser
>
> ```bash
> uv run python scripts/zendesk_oauth.py --refresh   # renews; then re-source .env
> ```
>
> **Re-source `.env` afterwards: both values rotate.** Zendesk invalidates the spent refresh
> token, so `ZENDESK_OAUTH_TOKEN` *and* `ZENDESK_OAUTH_REFRESH_TOKEN` are both new. Browser
> consent (`--serve`) is needed **only** if the 30-day refresh token itself lapses.
>
> **This page was wrong about the mechanism three times, so here is what it actually is:**
>
> - The access token is a **JWT with exactly one claim (`exp`)** and a lifetime of **exactly
>   1800s**. It was never "expired or revoked" — it simply expires. `expires_in` is not a
>   lever: minting with 86400 / 172800 / 604800 returned 1800 every time.
> - **A refresh token was being issued all along.** `scope` is `read write` with no
>   `offline_access`, and Zendesk returns a **30-day** `refresh_token` regardless. The old
>   `scripts/zendesk_oauth.py` did `.get("access_token")` and discarded the rest — and the
>   grant response is the **only** place the refresh token is readable in full
>   (`oauth/tokens/current.json` masks it) — so every one was thrown away unrecoverably.
>   **That is why this was misdiagnosed three times.**
> - **Rotation is proven, not assumed.** `--refresh` was run twice: the access token rotated
>   both times, the refresh token rotated both times, and each new access token returned
>   **200**.
>
> **Limitation, recorded as an open question rather than a defect** (`docs/BUILD-PLAN.md`
> §10.7a): rotation is durable for a *process's* lifetime but **not across a container
> restart** — a container's copy of the refresh token dies the first time that container
> refreshes it. Fixing it properly needs a store the containers share (the database), which
> is a **scope decision**.
>
> **API tokens were never an alternative.** `docs/SPEC.md:147` forbids them, *and* this
> account (admin user created 2026-08-14) falls after Zendesk's **2026-07-28** cutoff that
> blocks creating them at all.
>
> A real Zendesk ticket has since been answered end to end on the droplet with this
> credential — `docs/STATE.md §1`.

<details><summary>Previous status, 2026-08-17 morning (kept — it is the record of the last wrong theory)</summary>

> **Status, 2026-08-17.** The *cause* is fixed in code: the access token's 30-minute
> expiry is now renewed automatically from a 30-day refresh token
> (`backend/src/helpdesk/zendesk_credentials.py`), and a credential that cannot be renewed
> fails loudly instead of 401ing invisibly. What is left is **one** browser consent, to
> seed `ZENDESK_OAUTH_REFRESH_TOKEN` in `.env` — the token currently there was minted by
> the old script, which discarded the refresh token. Details and the exact command are
> under "The permanent fix" below.

</details>

**Gates:** all of Wave 4 (live e2e), demo shots 1–5, **W3-G3's redeploy being worth
anything**, and W3-G2's deep check ever passing against a real deployment. **UNBLOCKED.**

*Everything from here down is the historical record of how this was diagnosed. It is accurate
about what was measured and stale about the status; the banner above is current.*

Measured 2026-08-17 while building W3-G2, twice, from the token in `.env`:

```
GET https://hank-43016.zendesk.com/api/v2/users/me.json
  →  HTTP/2 401
     error: invalid_token
     error_description: The access token provided is expired, revoked, malformed
                        or invalid for other reasons.
GET /api/v2/tickets.json?per_page=3                        →  401
```

The subdomain is alive (Zendesk sets session cookies and answers with its own
`www-authenticate: Bearer realm=Zendesk::OAuth`), so this is the token, not the account.

**Why this is more urgent than "Wave 4 needs it".** `agent.nodes.ingest`'s first statement
is `deps.port.fetch_ticket(ticket_id)`. With a dead token that call is a 401
`HelpdeskAPIError`, `worker.main.run_ticket` catches it, releases the dedup row (ADR-003)
and returns — so **every** run fails and **no `runs` row is ever written**, on the droplet
or anywhere else. A redeploy (W3-G3) with a connected core loop and this token will look
healthy in `docker compose ps`, answer `verify_deploy.sh` 4/4, and answer no tickets. The
only signal is the worker's ERROR log, because arq books a swallowed failure as
`success = True` (`worker/main.py`'s docstring).

~~**Fix:** step 2 of the collapsed section below (`uv run python scripts/zendesk_oauth.py
--serve`), then step 3 to verify. Two minutes, needs a browser login.~~ **Superseded — the fix
is `--refresh`, and it needs no browser. See the banner at the top of OA-4.**

### ⚠️ The reason this keeps reopening: the token lives ~25 minutes

**Measured 2026-08-17 during W3-G3, and this is the finding that changes what OA-4 *is*.**
`ZENDESK_OAUTH_TOKEN` is not an opaque, long-lived Zendesk token. It is a **JWT**
(`alg: EdDSA`) whose payload carries exactly one claim:

```
payload keys : ['exp']
exp          = 1786946593  ->  2026-08-17T06:03:13Z
```

Observed timeline on one afternoon, all three points read back from the API rather than
inferred:

| Time (UTC) | Event |
|---|---|
| 05:38 | `GET /api/v2/users/me.json` → **200**, Hank Holcomb / admin |
| **06:03:13** | **the `exp` claim in the token** |
| 06:05:36 | the droplet's arq worker calls `fetch_ticket` → **401 `invalid_token`** |
| 06:07 | the same token from the developer's laptop → **401** |

So the token is not being revoked by anything; it simply expires, on the order of
**25 minutes** from issue. That is why the ✅ on this page has now flipped to ❌ three
times with "nothing announcing the change".

**Consequences that matter more than the re-auth itself:**

- **A re-auth buys a 30-minute window** (measured 1800s exactly, not the ~25 min first
  estimated — that figure was the remainder from first *observed use*, not from issue).
  Before the fix below, `scripts/verify_deploy.sh --deep` at up to 4 minutes had to run
  *immediately* after a re-auth.
- **Wave 4 was not runnable on a single token.** ADR-015's 20–30-ticket scenario run, and
  every demo take, cross an expiry boundary. This is what the refresh path below is for:
  with `ZENDESK_OAUTH_REFRESH_TOKEN` seeded, a long-running worker renews itself and the
  30-minute boundary stops being a filming constraint.
- ~~**Nothing in the app refreshes it.**~~ **Fixed 2026-08-17.**
  `backend/src/helpdesk/zendesk_credentials.py` now renews the token in-process, before a
  stale call goes out and again in response to a 401.

**The permanent fix — implemented 2026-08-17, and the standing theory about it was wrong.**

The theory recorded here was: the client would honour a refresh, but
`scripts/zendesk_oauth.py` never requests `offline_access`, so no refresh token is ever
issued. **The first half is right and the second half is false.** Measured against
`GET /api/v2/oauth/tokens/current.json` on a live token:

```
scopes                   : ['read', 'write']          <- no offline_access anywhere
refresh_token            : "...DM7OM4PKDA"            <- issued anyway
refresh_token_expires_at : 2026-09-16T06:35:52Z       <- 30 days
expires_at               : 2026-08-17T07:05:52Z       <- 30 min (created_at 06:35:52)
```

A refresh token **was being issued on every exchange all along**. The bug was that
`scripts/zendesk_oauth.py` parsed `access_token` out of the grant response and dropped the
rest of it. That endpoint is also the only place the refresh token is ever readable in
full — `oauth/tokens/current.json` reports it masked, as above — so each dropped value was
unrecoverable, which is what made the credential look un-renewable. **Nobody needs to
change the scope, and adding `offline_access` would have risked a 400 for no benefit.**

That the grant is authorized is now a three-way control, not a two-way inference:

```bash
POST /oauth/tokens  grant_type=refresh_token      →  400 invalid_grant          # supported + authorized; the VALUE was bad
POST /oauth/tokens  grant_type=client_credentials  →  400 unauthorized_client    # supported; client not authorized for it
POST /oauth/tokens  grant_type=banana_grant        →  400 unsupported_grant_type # server does not know it
```

Three distinct errors, so the server does separate those cases, and `refresh_token` lands
in the one that means "we accept this grant from this client".

Also measured, and worth knowing because it closes off the obvious shortcut: **`expires_in`
is not a lever.** Minting tokens with `expires_in` of 86400, 172800 and 604800 produced a
1800-second token every time. There is no "just ask for a 2-day token" option.

~~**What you still have to do once:**~~ **DONE — nothing outstanding.** `.env` now carries
`ZENDESK_OAUTH_REFRESH_TOKEN`, and rotation was demonstrated twice with a 200 on each new
access token. Both commands, for the record:

```bash
uv run python scripts/zendesk_oauth.py --serve     # writes BOTH tokens; only if the 30-day refresh token lapses
uv run python scripts/zendesk_oauth.py --refresh   # ordinary renewal, no browser; re-source .env after
```

**Caveat that is not fixed and cannot be fixed in `.env`:** Zendesk rotates the refresh
token on every use, so the copy forwarded into a container is invalidated the first time
that container refreshes. In-process renewal is therefore durable for the life of the
process and **not** across a `docker compose restart`, which falls back to the now-spent
value in the environment. The worker logs a WARNING when it rotates a value it cannot
persist. Persisting it properly needs a store the containers share (the DB), which is a
**scope decision, not a bug fix** — tracked as an open question in `docs/BUILD-PLAN.md §10.7a`.

The trial lapses around **2026-08-27**, unchanged.

> Worth noticing about the previous status line, which read "✅ DONE — verified
> 2026-08-16": it was true when written and false a day later, and nothing would have said
> so. The `Quick status check` at the bottom of this file is the only thing here that
> re-measures rather than remembers — run it before trusting any ✅ on this page.

~~**Still to do here after OA-3 lands:** re-point the webhook in Admin Center → Apps and
integrations → Webhooks to `https://cxforge.<your-domain>/webhooks/zendesk`.~~ **Not needed —
the webhook URL never changed (OA-3). The missing piece was a *trigger* to fire it, now created:
`docs/STATE.md §6.16`.**

<details><summary>Original instructions (kept for reference)</summary>

**Resolved 2026-08-16 — this IS just a token problem.** The earlier worry was that the
OAuth **client** had been deleted, in which case re-running the flow would fail with
`invalid_client` and fix nothing. That was measured rather than assumed:

```bash
# real client creds + a deliberately bogus code
POST https://$ZENDESK_SUBDOMAIN.zendesk.com/oauth/tokens  →  invalid_grant (400)
# control: wrong client_id, same everything else
                                                          →  invalid_client (401)
```

`invalid_grant` means the client authenticated and only the *grant* was bad — so **the
OAuth client is alive**. The control proves the two cases are distinguishable. The
browser check below is therefore no longer required; skip to step 2.

1. ~~Admin Center → OAuth Clients → confirm the client exists.~~ **Not needed** (above).
   Still worth a glance only if step 2 fails: its **Redirect URLs** must contain exactly
   ```
   http://localhost:8129/callback
   ```
   which is what `ZENDESK_OAUTH_REDIRECT_URI` is already set to.
2. Re-authorize — the listener flow needs no copy-paste:
   ```bash
   uv run python scripts/zendesk_oauth.py --serve
   ```
   Manual fallback if port 8129 is busy:
   ```bash
   uv run python scripts/zendesk_oauth.py --url     # open it, click Allow
   uv run python scripts/zendesk_oauth.py <code>    # paste the code= value
   ```
3. Verify:
   ```bash
   set -a; source .env; set +a
   curl -s -o /dev/null -w '%{http_code}\n' \
     -H "Authorization: Bearer $ZENDESK_OAUTH_TOKEN" \
     "https://$ZENDESK_SUBDOMAIN.zendesk.com/api/v2/users/me.json"
   # expect 200
   ```

**Trial deadline:** signed up around 2026-08-13, Suite trials run 14 days, so it lapses
around **2026-08-27**. Everything live must be recorded before then.

**After OA-3 lands**, the webhook endpoint in Admin Center → Apps and integrations →
Webhooks must be re-pointed to `https://cxforge.<your-domain>/webhooks/zendesk`.

</details>

---

## OA-5 — Record the demo video

**Gates:** submission. Last thing.

`docs/demo-script.md` gets rewritten in Wave 5 against what is actually recordable by
then. Do not film from the current version — it is accurate about today, and today
almost nothing is recordable.

Before booking camera time, run the route-accuracy harness (Wave 1, E3). It measures
whether the **real** model picks the right route for each scenario — something nothing in
the repo checks today, because every canonical test hands the route in via a fake. It is
the cheapest way to avoid a scenario behaving differently on camera than in tests.

**One decision to settle before filming, not during it:** every customer-visible reply is
currently authored by **"Hank Holcomb", admin** — the identity the OAuth token acts as — not by
the dedicated "Othram AI Agent" user. Switching is **demo optics, not correctness**, and needs an
OAuth consent as that user, which is `role: agent` and may lose permissions the admin token has.
If you want it, do it early and prove it with a `scripts/live_smoke.py` run.
`docs/BUILD-PLAN.md §10.7b`.

---

## Quick status check

Run this any time to see which owner actions are still outstanding:

```bash
set -a; source .env 2>/dev/null; set +a
st() { [ -n "$1" ] && echo SET || echo MISSING; }
echo "Voyage key:      $(st "$VOYAGE_API_KEY")"
echo "Langfuse public: ${LANGFUSE_PUBLIC_KEY:0:6} (want pk-lf-)"
echo "Langfuse secret: ${LANGFUSE_SECRET_KEY:0:6} (want sk-lf-)"
echo "Tunnel token:    $(st "$CLOUDFLARE_TUNNEL_TOKEN")"
echo "Anthropic key:   $(st "$ANTHROPIC_API_KEY")"
printf "Zendesk token:   "
curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 \
  -H "Authorization: Bearer $ZENDESK_OAUTH_TOKEN" \
  "https://$ZENDESK_SUBDOMAIN.zendesk.com/api/v2/users/me.json"
echo "Public hostname: $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://cxforge.hankholcomb.com/health) (want 200)"
```

**If the Zendesk line prints 401, that is normal** — the access token lives 1800s. Run
`uv run python scripts/zendesk_oauth.py --refresh`, re-source `.env` (both values rotate), and
re-check. No browser needed unless the 30-day refresh token has lapsed (OA-4).

> **Never use `${VAR:+SET}${VAR:-MISSING}`** for this. When `VAR` *is* set, `${VAR:-MISSING}`
> expands to the **secret itself**, so the line prints `SET` followed by the full key. The
> earlier revision of this snippet did exactly that and printed `ANTHROPIC_API_KEY` in
> full. It matters most in the one place you are most likely to run it: on camera.
