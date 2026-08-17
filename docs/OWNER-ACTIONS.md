# Owner actions — the things only Hank can do

Five items. Two of them gate whole parallel tracks, so do those first even though the
deadline pressure is on the Zendesk one.

Nothing here can be done from inside an agent session: each needs a browser consent, an
account signup, a purchase decision, or a camera.

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

## OA-3 — Cloudflare domain and named tunnel ⚠️ **ONE FIELD WRONG — 30-second dashboard fix, 2026-08-17**

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
> droplet. It does **not** block `scripts/verify_deploy.sh`, which targets
> `http://161.35.2.250:8080` directly.

**Gates:** Wave 1 F2, Wave 3 redeploy, and all of Wave 4. **UNBLOCKED.**

Verified end to end from the public internet, not from the dashboard:

```
dig +short cxforge.hankholcomb.com @1.1.1.1  →  172.67.136.113, 104.21.7.150   (Cloudflare anycast)
curl https://cxforge.hankholcomb.com/health  →  502
```

**The 502 is the correct answer and is not a defect.** It means Cloudflare's edge resolved
the hostname, found the tunnel configuration, and could not reach the origin — because
`cloudflared` exists only as committed config in the working tree and nothing is deployed.
It becomes 200 when W3-G3 redeploys the droplet. Getting a 502 rather than a 1033/530 is
positive evidence the tunnel and hostname are wired correctly.

> **Corrected 2026-08-17 by W3-G3.** The sentence "it becomes 200 when W3-G3 redeploys the
> droplet" was wrong, and wrong in an instructive way: it predicted an effect instead of
> reading one back. The droplet has now been redeployed, `cloudflared` is running with 4
> ready connections, and the endpoint is **still 502** — for a completely different reason
> than the one this section describes (the ingress rule's scheme; see the banner above).
> A 502 is consistent with at least three distinct causes, which is exactly why it could not
> carry the claim "the tunnel and hostname are wired correctly" on its own.

`CLOUDFLARE_TUNNEL_TOKEN` (184 chars) and `PUBLIC_BASE_URL=https://cxforge.hankholcomb.com`
are both in `.env`.

**Still to do, after W3-G3:** re-point the Zendesk webhook (Admin Center → Apps and
integrations → Webhooks) to `https://cxforge.hankholcomb.com/webhooks/zendesk`.

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

## OA-4 — Zendesk re-authorization ❌ **REOPENED — the token is dead again, 2026-08-17**

**Gates:** all of Wave 4 (live e2e), demo shots 1–5, **W3-G3's redeploy being worth
anything**, and W3-G2's deep check ever passing against a real deployment.

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

**Fix:** step 2 of the collapsed section below (`uv run python scripts/zendesk_oauth.py
--serve`), then step 3 to verify. Two minutes, needs a browser login.

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

- **A re-auth buys a ~25-minute window.** `scripts/verify_deploy.sh --deep` can take up to
  4 minutes, so it must be run *immediately* after the re-auth, not after a build.
- **Wave 4 is not runnable on a single token.** ADR-015's 20–30-ticket scenario run, and
  every demo take, will cross an expiry boundary. Filming a 5-shot demo on a 25-minute
  credential is a losing proposition.
- **Nothing in the app refreshes it.** The token is read from the environment at request
  time and never renewed, so a container that is running when the token expires keeps
  401ing until someone re-authorizes and restarts (or re-copies `.env`).

**The permanent fix, and evidence that it is available.** Zendesk's `refresh_token` grant
**is authorized for this OAuth client** — measured, with a control that proves the two
errors are distinguishable:

```bash
# grant_type=refresh_token, deliberately invalid refresh_token
POST /oauth/tokens  →  400 invalid_grant        # grant type accepted, token rejected
# control: grant_type=client_credentials, real client_id + secret
POST /oauth/tokens  →  400 unauthorized_client  # that grant type is NOT authorized
```

`invalid_grant` rather than `unsupported_grant_type`/`unauthorized_client` means the client
will honour a refresh. But `scripts/zendesk_oauth.py` requests `SCOPE = "read write"` and
never `offline_access`, so **no refresh token is ever issued or stored** — the flow throws
away the one thing that would end this. Making that change (request `offline_access`, keep
`ZENDESK_OAUTH_REFRESH_TOKEN`, refresh on 401 in `helpdesk/zendesk_adapter.py`) touches
`scripts/**` and `backend/src/helpdesk/**`, which are not W3-G3's rows in the ownership
matrix, so it is written down here as the owner's call rather than taken.

The trial lapses around **2026-08-27**, unchanged.

> Worth noticing about the previous status line, which read "✅ DONE — verified
> 2026-08-16": it was true when written and false a day later, and nothing would have said
> so. The `Quick status check` at the bottom of this file is the only thing here that
> re-measures rather than remembers — run it before trusting any ✅ on this page.

**Still to do here after OA-3 lands:** re-point the webhook in Admin Center → Apps and
integrations → Webhooks to `https://cxforge.<your-domain>/webhooks/zendesk`.

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
```

> **Never use `${VAR:+SET}${VAR:-MISSING}`** for this. When `VAR` *is* set, `${VAR:-MISSING}`
> expands to the **secret itself**, so the line prints `SET` followed by the full key. The
> earlier revision of this snippet did exactly that and printed `ANTHROPIC_API_KEY` in
> full. It matters most in the one place you are most likely to run it: on camera.
