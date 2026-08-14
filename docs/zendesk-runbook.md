# Zendesk setup runbook (T-4)

> **Every step below is a HUMAN step.** It requires a Zendesk trial account,
> live browser access to the Zendesk Admin Center, and (for the OAuth token
> exchange) a terminal to run a couple of `curl` commands. **No Zendesk
> credentials exist anywhere in this repo or this development environment.**
> An agent cannot sign up for the trial, click through Admin Center, or hold
> the resulting secrets — you do. Follow this runbook yourself, then paste
> the resulting values into `.env` (copied from `.env.example`).
>
> OAuth 2.0 only. Zendesk API tokens are on a staged removal schedule and
> are forbidden by SPEC — nowhere in this runbook do you generate or use
> one.

## What this wires up

```
Zendesk trigger (on ticket create / new comment)
  --Notify active webhook-->  Zendesk webhook (signs the request, HMAC-SHA256)
    --HTTPS via cloudflared-->  your local FastAPI app, POST /webhooks/zendesk
```

`backend/src/ingress/__init__.py` (T-4) verifies the signature, validates
the payload, drops the AI's own events, and dedupes on `(ticket_id,
comment_id)`. This runbook is what makes that endpoint receive real traffic.

## Prerequisites

- A way to run this repo locally: `docker compose up -d db`, `uv sync`.
- [`cloudflared`](https://developer.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
  installed (`brew install cloudflared` on macOS, or the equivalent for your
  OS).
- `curl` and `python3` on your PATH (used below only to exchange an OAuth
  code for a token and to read back a user id — no dependency on this
  repo's virtualenv).

---

## Step 1 — Zendesk trial signup

1. Go to <https://www.zendesk.com/register/> and sign up for a free trial
   (Zendesk Suite, 14 days). Use any email you control — this is a
   throwaway evaluation account, not production.
2. Choose a subdomain when prompted, e.g. `othram-support-agent`. Your
   Zendesk instance is now reachable at
   `https://<subdomain>.zendesk.com`, and its Admin Center at
   `https://<subdomain>.zendesk.com/admin/`.
3. Record the subdomain — it's `ZENDESK_SUBDOMAIN` in `.env`.

## Step 2 — OAuth app + token (OAuth 2.0 only)

Zendesk API tokens are deprecated and forbidden by SPEC. This project
authenticates as an OAuth 2.0 app instead.

1. In Admin Center: **Apps and integrations → APIs → Zendesk API →
   OAuth Clients** → **Add OAuth client**.
2. Fill in:
   - **Client name**: `othram-support-agent`
   - **Unique identifier**: leave the auto-filled value, or set your own
     (e.g. `othram_support_agent`) — this is `ZENDESK_OAUTH_CLIENT_ID`.
   - **Redirect URLs**: `http://localhost:8129/callback` — exactly this.
   - **Client kind**: Zendesk creates these as `kind: "public"`. A public
     client cannot hold a secret, so **PKCE is mandatory**, not optional.
     `scripts/zendesk_oauth.py` sends `code_challenge` +
     `code_challenge_method=S256` and the matching `code_verifier` at
     exchange. Omitting them makes `/oauth/authorizations/new` fail with
     `invalid_request — The request is missing a required parameter`, even
     when the client id, secret and redirect URL are all correct. That
     error was hit for real during this build and cost several rounds of
     misdiagnosis; check `kind` before blaming the credentials. You can
     read the client's true config from a logged-in browser with:
     `fetch('/api/v2/oauth/clients/<id>.json',{credentials:'include'})`.

     > **Do not use `urn:ietf:wg:oauth:2.0:oob`.** An earlier revision of
     > this runbook said to, and it does not work: Zendesk requires redirect
     > URLs to be "absolute and not relative" and "secure (https) unless
     > you're using localhost or 127.0.0.1". The out-of-band URN is neither,
     > so `/oauth/authorizations/new` rejects the request with
     > `invalid_request — The request is missing a required parameter,
     > includes an unsupported parameter or parameter value, or is otherwise
     > malformed`, before any code is issued. localhost is explicitly
     > permitted, which is why the helper below uses it.

3. Save. Zendesk shows the **Secret** exactly once — copy it now. This is
   `ZENDESK_OAUTH_CLIENT_SECRET`. If you lose it, delete and recreate the
   OAuth client.
4. Put `ZENDESK_SUBDOMAIN` (the bare name — **not** the full
   `*.zendesk.com` hostname, which would be doubled into the API base URL)
   and `ZENDESK_OAUTH_CLIENT_ID` (the **Unique identifier** field, not the
   numeric client id) into `.env`.
5. Run the authorization-code flow. From the repo root:

   ```bash
   uv run python scripts/zendesk_oauth.py --serve
   ```

   It prints an authorization URL and starts a listener on
   `localhost:8129`. Open the URL in a browser logged into Zendesk, click
   **Allow**, and Zendesk redirects back to the listener, which captures
   the code, exchanges it, and writes `ZENDESK_OAUTH_TOKEN` into `.env`
   without printing it.

   If you cannot run a listener, use the manual path instead — print the
   URL, approve, then copy the `code=` value out of the address bar of the
   "cannot connect" page you land on:

   ```bash
   uv run python scripts/zendesk_oauth.py --url
   uv run python scripts/zendesk_oauth.py <code>
   ```

   Authorization codes are single-use and short-lived, so exchange
   immediately. The resulting token does not expire on its own (Zendesk
   OAuth tokens are long-lived until revoked), so this is once per trial.

   **Reading the errors**: `invalid_client` means the client id or secret
   is wrong. `invalid_grant` means the credentials were *accepted* and the
   code was the problem — expired, already used, or the redirect URL you
   registered does not match `http://localhost:8129/callback` exactly.

## Step 3 — Dedicated AI agent user (for attribution)

The agent must post replies as its own identity, never impersonating a
human agent, and ingress needs that identity's id to drop the agent's own
webhook events (Step 7's loop guard, layer two).

1. In Admin Center: **People → Team → Team members** → **Add team member**.
2. Name it something unambiguous, e.g. **"Othram AI Agent"**, with an email
   you control (e.g. `ai-agent+othram@<your domain>`). Role: **Agent**.
3. Complete/skip the invite flow (the account doesn't need to ever log in
   interactively — it's used only via the API, as the identity behind
   `ZendeskAdapter`'s writes).
4. Find its numeric user id (needed for `ZENDESK_AI_USER_ID`) with the
   OAuth token from Step 2:

   ```bash
   curl -s -H "Authorization: Bearer <ZENDESK_OAUTH_TOKEN>" \
     "https://<subdomain>.zendesk.com/api/v2/users/search.json?query=email:ai-agent+othram@<your domain>" \
     | python3 -c "import sys,json; print(json.load(sys.stdin)['users'][0]['id'])"
   ```

   That printed number is `ZENDESK_AI_USER_ID`.

## Step 4 — Fill in `.env`

```bash
cp .env.example .env   # if you haven't already
```

Set, from the steps above:

```
ZENDESK_SUBDOMAIN=<subdomain>
ZENDESK_OAUTH_CLIENT_ID=<client id>
ZENDESK_OAUTH_CLIENT_SECRET=<client secret>
ZENDESK_OAUTH_TOKEN=<access token>
ZENDESK_AI_USER_ID=<ai agent user id>
```

`ZENDESK_WEBHOOK_SIGNING_SECRET` is filled in Step 6, after the webhook
exists (Zendesk generates it, you don't choose it).

## Step 5 — Run the app and expose it with cloudflared

In one terminal, from the repo root:

```bash
docker compose up -d db
uv run uvicorn main:app --app-dir backend/src --port 8000
```

In a second terminal:

```bash
cloudflared tunnel --url http://localhost:8000
```

`cloudflared` prints a line like
`https://random-words-1234.trycloudflare.com` — that's your public HTTPS
endpoint for this dev session. It changes every time you restart
`cloudflared` unless you set up a named tunnel, which this dev setup
doesn't need. Your webhook target (Step 6) is:

```
https://random-words-1234.trycloudflare.com/webhooks/zendesk
```

Keep both terminals running for the rest of this runbook and for any live
demo.

## Step 6 — Create the webhook and capture the signing secret

1. In Admin Center: **Apps and integrations → Webhooks → Webhooks** →
   **Create webhook**.
2. **Name**: `othram-ai-ingress`.
3. **Endpoint URL**: the cloudflared URL from Step 5, with `/webhooks/zendesk`
   appended (e.g. `https://random-words-1234.trycloudflare.com/webhooks/zendesk`).
4. **Request method**: `POST`. **Request format**: `JSON`.
5. **Authentication**: `None`. (We don't use a bearer/basic-auth header on
   the webhook itself — every request is verified by the HMAC signature
   Zendesk always attaches, regardless of this setting, which is what
   Step 8 checks.)
6. Save.
7. Open the webhook you just created → find its **Signing Secret** section
   → click **Show**/**Reveal secret** → copy the value. This is
   `ZENDESK_WEBHOOK_SIGNING_SECRET` — paste it into `.env` now. It is
   already base64-encoded by Zendesk; paste it exactly as shown, don't
   re-encode it.

## Step 7 — Create the trigger

This is the piece that actually fires the webhook, and the loop-guard's
**first line of defense**.

1. In Admin Center: **Objects and rules → Business rules → Triggers** →
   **Create trigger**.
2. **Name**: `Othram AI ingress — notify on ticket create/comment`.
3. **Category**: default is fine.
4. **Conditions** — click **Add condition** and build:
   - **Meet ALL of the following conditions:**
     - `Tags` | **Contains none of the following** | `ai-processed`
   - **Meet ANY of the following conditions:**
     - `Ticket` | **Is** | `Created`
     - `Comment` | **Is** | `Public`

   The `Tags … Contains none of the following … ai-processed` condition is
   the **nullifier**: `ZendeskAdapter` (`backend/src/helpdesk/
   zendesk_adapter.py`) folds the tag `ai-processed` into every write it
   makes, unconditionally. Without this condition, the AI's own reply would
   itself satisfy "Comment is Public", the trigger would fire again on the
   AI's own write, ingress would receive another webhook call, the agent
   would run again, post again, re-tag again — an infinite loop that costs
   money and spams the ticket. With this condition, any ticket state the
   AI has already touched is permanently excluded from ever firing this
   trigger again, for any reason.

   > **Note on "Comment is Public"**: this condition fires on a public
   > comment from *anyone* — the requester, a human agent, or (absent the
   > tags condition) the AI. That's intentional and safe here: the
   > `ai-processed` tag condition above is the trigger-level guard against
   > the AI's own writes, and ingress's `comment_author_id` check (Step 7's
   > JSON body, below) is a **second, independent** guard against the same
   > failure mode — see `backend/src/ingress/__init__.py`'s module
   > docstring. Zendesk's trigger condition list doesn't offer a single
   > clean "public comment from the requester, specifically" field across
   > all plan tiers; rather than depend on one that may not be visible in
   > your account, this runbook relies on the two-layer guard the DESIGN
   > doc already specifies instead. If a human agent ever replies publicly
   > by hand during a demo, this trigger will also fire for that reply —
   > acceptable for this project's scope (autonomous-mode demo tickets),
   > but worth knowing before recording a demo video with a human posting
   > interim replies.

5. **Actions** — **Add action**: `Notify active webhook` → select
   `othram-ai-ingress` (created in Step 6) → **JSON body**:

   ```json
   {
     "ticket_id": "{{ticket.id}}",
     "comment_id": "{{ticket.latest_comment_id}}",
     "requester_email": "{{ticket.requester.email}}",
     "subject": "{{ticket.title}}",
     "latest_comment_text": "{{ticket.latest_comment}}",
     "comment_author_id": "{{current_user.id}}"
   }
   ```

   **This body must match `ZendeskWebhookPayload`
   (`backend/src/ingress/models.py`) field-for-field** — it does, by
   construction:

   | JSON key               | Zendesk placeholder            | Pydantic field (models.py) |
   |-------------------------|--------------------------------|------------------------------|
   | `ticket_id`             | `{{ticket.id}}`                 | `ticket_id: str` |
   | `comment_id`             | `{{ticket.latest_comment_id}}`  | `comment_id: str` |
   | `requester_email`       | `{{ticket.requester.email}}`    | `requester_email: str` |
   | `subject`                | `{{ticket.title}}`              | `subject: str` |
   | `latest_comment_text`   | `{{ticket.latest_comment}}`     | `latest_comment_text: str` |
   | `comment_author_id`     | `{{current_user.id}}`           | `comment_author_id: str \| None` |

   `comment_author_id` is **not** one of DESIGN's pinned five fields — it's
   the one addition T-4 needed to implement self-event drop (DESIGN pins
   the *payload shape* but not *how ingress learns the author*; see
   `ingress/models.py`'s docstring for the full rationale). `current_user`
   is Zendesk's placeholder for "whoever just made this update" — for a
   trigger firing on a new comment, that's the comment's author, which is
   exactly the id ingress compares against `ZENDESK_AI_USER_ID`. **If you
   ever rename this JSON key, you must rename `comment_author_id` in
   `ingress/models.py` to match, in the same change** — the two are
   required to stay in lockstep, or self-event drop silently stops
   working.

   Before saving, use the trigger editor's placeholder preview against a
   real or sample ticket to confirm `{{ticket.latest_comment_id}}` resolves
   to a non-empty numeric id in your account. (Zendesk placeholders are
   stable but occasionally vary by plan/version; if that specific token
   isn't offered by your instance's placeholder picker, use the field
   search in the JSON body editor to find whatever your account calls "ID
   of the latest comment" and substitute it — then update this table and
   nothing else, since the pydantic side only cares about the JSON key
   name, not which placeholder fills it.)

6. Save the trigger.

## Step 8 — Verification

### End-to-end, via the real Zendesk UI

1. With `uvicorn` and `cloudflared` still running (Step 5), open your
   Zendesk instance and create a new ticket (as if you were a customer —
   use **Add ticket → your subdomain's end-user view**, or the public
   support form if enabled).
2. Watch the `uvicorn` terminal. Within a few seconds you should see an
   incoming request log line for `POST /webhooks/zendesk` returning
   `202`.
3. Confirm the event was recorded and deduped:

   ```bash
   docker exec -it othram-db psql -U othram -d othram \
     -c "SELECT * FROM tickets_seen ORDER BY ticket_id DESC LIMIT 5;"
   ```

   You should see one row for the ticket you just created.
4. Reply again on the same ticket as the customer (a new comment, same
   ticket) — a **second, distinct** row should appear (same `ticket_id`,
   different `comment_id`).
5. In Admin Center, open the webhook (Step 6) → **Recent deliveries** (or
   equivalent monitoring tab) to see Zendesk's own record of the request
   and response Zendesk received from your endpoint — this is the
   authoritative source if step 2's log line didn't appear (e.g. tunnel
   dropped) and shows the exact HTTP status your endpoint returned.

### What success vs. rejection actually look like

Verified directly against a running instance of this endpoint (values
below are from a real local run — not hypothetical):

**Valid signature, new event — `202 Accepted`:**

```
$ curl -i -X POST http://127.0.0.1:8000/webhooks/zendesk \
    -H "Content-Type: application/json" \
    -H "X-Zendesk-Webhook-Signature: <base64 hmac>" \
    -H "X-Zendesk-Webhook-Signature-Timestamp: 2026-08-13T12:00:00Z" \
    -d '{"ticket_id":"42","comment_id":"501","requester_email":"jane@example.com","subject":"Case update?","latest_comment_text":"Any news on my case?"}'

HTTP/1.1 202 Accepted
content-type: application/json

{"status":"accepted","duplicate":false}
```

**The identical request, posted again — still `202`, but `duplicate: true`,
and no second row is written to `tickets_seen`:**

```
HTTP/1.1 202 Accepted
content-type: application/json

{"status":"accepted","duplicate":true}
```

**Signature present but wrong — `401`, request never reaches the database:**

```
HTTP/1.1 401 Unauthorized
content-type: application/json

{"detail":"signature mismatch"}
```

**Signature headers missing entirely — `401`:**

```
HTTP/1.1 401 Unauthorized
content-type: application/json

{"detail":"missing signature headers"}
```

A malformed body (missing a required field, or not JSON at all) returns
`400`, never `500` — see `backend/tests/ingress/test_webhook.py` for the
exact cases this is tested against.

### If it doesn't work

- **Nothing arrives at `uvicorn` at all**: check the `cloudflared` terminal
  for a live tunnel URL (it re-generates on restart — the webhook's
  Endpoint URL in Step 6 must be updated to match whenever you restart
  `cloudflared`), and check the webhook's **Recent deliveries** view in
  Admin Center for the HTTP status Zendesk actually got (a `000`/timeout
  there usually means the tunnel URL is stale).
- **`401` on every request, including ones you believe are correctly
  signed**: re-copy `ZENDESK_WEBHOOK_SIGNING_SECRET` from the webhook's
  detail page — it's shown once per "reveal" click and is easy to
  truncate/mistype when copying by hand.
- **Trigger never fires**: check **Business rules → Triggers →
  (your trigger) → ... → View trigger revision history / Usage** or just
  re-open the ticket and confirm its tags — if a prior test run left
  `ai-processed` on a ticket you're reusing, the nullifying condition will
  correctly (and silently) block it. Use a fresh ticket.
