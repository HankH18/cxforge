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
   immediately.

   > **CORRECTION (2026-08-17).** An earlier revision of this step said "the
   > resulting token does not expire on its own (Zendesk OAuth tokens are
   > long-lived until revoked), so this is once per trial." **That is false**,
   > and believing it caused the same dead credential to be misdiagnosed as
   > "someone forgot to re-authorize" three separate times.
   >
   > The access token is a JWT (`typ: at+jwt`, `alg: EdDSA`) whose payload
   > carries exactly one claim, `exp`, **1800 seconds** after issue. Measured
   > from `GET /api/v2/oauth/tokens/current.json` on the live account:
   > `created_at 06:35:52` / `expires_at 07:05:52`. This is Zendesk's
   > documented 30-minute default for OAuth clients created after
   > 2026-04-30, and `expires_in` is **not** a lever — minting with 86400,
   > 172800 and 604800 all produced a 1800-second token.
   >
   > So this is **not** once per trial. What makes it workable is the
   > **refresh token**, which Zendesk returns alongside every access token
   > (30-day life, and with plain `scope="read write"` — no `offline_access`
   > scope is involved). `scripts/zendesk_oauth.py` now saves it as
   > `ZENDESK_OAUTH_REFRESH_TOKEN`; the previous revision parsed only
   > `access_token` out of the response and dropped it, which is the whole
   > reason the credential looked un-renewable.

   Once the refresh token is in `.env`, renew with **no browser**:

   ```bash
   uv run python scripts/zendesk_oauth.py --refresh
   ```

   Two things to know about it. Zendesk **rotates** the refresh token on every
   use, so the old value dies the moment it is spent — always let the script
   write both values back rather than copying one by hand. And a container
   that refreshes in-process cannot write to `.env`, so after a `docker
   compose restart` the environment's copy may already be spent; see the
   WARNING in `backend/src/helpdesk/zendesk_credentials.py`.

   Long-running processes do not need this command at all —
   `ZendeskCredentials` refreshes on its own when the access token is stale or
   a call 401s, and raises loudly (never silently) when it cannot.

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
4. Find the numeric user id for `ZENDESK_AI_USER_ID` — and read this carefully,
   because the obvious answer is the wrong one.

   > **CORRECTION (2026-08-17).** This step used to say: look up the team
   > member you just created, by email, with
   > `users/search.json?query=email:...`, and use that id. **That is wrong**,
   > and it put a live loop guard out of action. `ZENDESK_AI_USER_ID` is not
   > "the id of the AI's user account" — it is **the id the OAuth token
   > actually acts as**, because ingress compares it against the
   > `comment_author_id` on incoming webhooks, and Zendesk attributes a comment
   > to whoever authorized the token, not to whoever you intended.
   >
   > On this account those were two different people: the variable named
   > "Othram AI Agent" (`54404962250395`) while the token had been authorized
   > by the owner's admin user (`54402664002843`), so the AI's own reply was
   > authored by `54402664002843` and the self-event guard was comparing
   > against an id that appears in no event this system can ever receive. Two
   > green suites and a fully working end-to-end run showed nothing.

   So ask the token who it is, rather than asking Zendesk about a user:

   ```bash
   curl -s -H "Authorization: Bearer <ZENDESK_OAUTH_TOKEN>" \
     "https://<subdomain>.zendesk.com/api/v2/users/me.json" \
     | python3 -c "import sys,json; u=json.load(sys.stdin)['user']; print(u['id'], u['name'], u['role'])"
   ```

   That printed id is `ZENDESK_AI_USER_ID`. If the name it prints is a human
   rather than the AI agent user, that is a real finding and not a formality:
   every customer-visible reply will be attributed to that human. Fixing the
   attribution means completing Step 2's browser consent **while signed in as
   the AI agent user**, and then re-running the command above — the id must
   always be whatever the token reports, never what you wish it were.

   You do not have to take this on trust. `ZendeskAdapter.verify_ai_user_id()`
   makes the same call and raises if the configured value disagrees; it runs as
   the first check in `scripts/live_smoke.py`.

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
     "comment_id": "{{ticket.latest_comment.id}}",
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
   | `ticket_id`             | `{{ticket.id}}`                 | `ticket_id: str` (non-empty) |
   | `comment_id`             | `{{ticket.latest_comment.id}}`  | `comment_id: str` (non-empty) |
   | `requester_email`       | `{{ticket.requester.email}}`    | `requester_email: str` |
   | `subject`                | `{{ticket.title}}`              | `subject: str` |
   | `latest_comment_text`   | `{{ticket.latest_comment}}`     | `latest_comment_text: str` |
   | `comment_author_id`     | `{{current_user.id}}`           | `comment_author_id: str \| None` |

   Note the **dot** in `{{ticket.latest_comment.id}}`. `latest_comment` is an
   object with an `id`; `{{ticket.latest_comment_id}}` — with an underscore —
   is **not a placeholder Zendesk has**, and Zendesk renders an unknown
   placeholder as the empty string rather than failing. See the measurement
   under the table.

   Pairing `comment_id` with `{{ticket.latest_comment.id}}` is also the only
   self-consistent choice, because `latest_comment_text` is
   `{{ticket.latest_comment}}`: the id and the text then always describe the
   *same* comment. `{{ticket.latest_public_comment.id}}` also resolves (see
   below) but would let the two disagree whenever the newest comment is a
   private note.

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

   > **CORRECTION (2026-08-17), measured on the live account.** This step used
   > to specify `comment_id: "{{ticket.latest_comment_id}}"` and merely *advise*
   > confirming it resolves. It does not resolve. **It renders as the empty
   > string**, and because ingress keys idempotency on
   > `(ticket_id, comment_id)`, every comment on ticket N collapsed onto the
   > single key `(N, "")`: the first comment was processed, and **every customer
   > follow-up after it was discarded as a duplicate** — `202
   > {"duplicate": true}`, no run, and (at the time) not one log line saying a
   > real message had been thrown away.
   >
   > The values below are verbatim from this account, read out of Zendesk's own
   > delivery record (`GET /api/v2/webhooks/{id}/invocations/{id}/attempts`,
   > which returns the **rendered request payload**, not just a status code).
   > Trigger context, one firing per row, against ticket 3:
   >
   > | Placeholder | Rendered as |
   > |---|---|
   > | `{{ticket.latest_comment.id}}`        | `54509304133531` — the posted comment's real id |
   > | `{{ticket.latest_public_comment.id}}` | `54509304133531` — same id |
   > | `{{ticket.latest_comment_id}}`        | `` (empty string) |
   > | `{{ticket.updated_at_with_timestamp}}`| `2026-08-17T07:34:48Z` (second resolution) |
   > | `{{ticket.updated_at}}`               | `August 17, 2026` (**day** resolution — useless as a key) |
   > | `{{ticket.latest_comment.created_at}}`| `August 17, 2026` (day resolution) |
   > | `{{ticket.comment.id}}` · `{{ticket.audit.id}}` · `{{ticket.audit_id}}` | `` (empty string) |
   >
   > Before/after, from two real trigger firings: `"comment_id": ""` (invocation
   > `01M07AA00MZJ23QBBR5MKZWDEM`, 07:34:48Z) became
   > `"comment_id": "54509363035291"` (invocation `01M07AMDNSHP8D7584XEZDTQMP`)
   > and `"comment_id": "54509451282203"` (invocation
   > `01M07B1QPS4J2TQJSDNPP6ZR65`) — two different comments on the same ticket,
   > each answered `{"status":"accepted","duplicate":false}`, each dispatching
   > its own run.
   >
   > **How to check this yourself, rather than trusting the picker.** The
   > trigger editor's preview is not the authority — do one of these:
   >
   > - Create a throwaway macro whose comment body is a probe like
   >   `A[{{ticket.latest_comment.id}}] B[{{ticket.latest_comment_id}}]`, then
   >   render it without posting anything:
   >   `GET /api/v2/tickets/<id>/macros/<macro id>/apply.json` returns
   >   `result.ticket.comment.body` with every placeholder substituted. Delete
   >   the macro afterwards. This is the cheap screen — no webhook, no run.
   > - Or fire the trigger for real and read
   >   `GET /api/v2/webhooks/<webhook id>/invocations` then
   >   `…/invocations/<invocation id>/attempts`, which shows the exact rendered
   >   body Zendesk sent and the exact response it got back.
   >
   > If you substitute a different placeholder, update **this table and the one
   > above** and nothing else — the pydantic side only cares about the JSON key
   > name, not which placeholder fills it. What it *does* now care about is that
   > the value is non-empty: `ZendeskWebhookPayload` rejects a blank
   > `ticket_id` or `comment_id` with a **400** naming this step, rather than
   > accepting an id that identifies nothing and silently poisoning the dedup
   > key. So a placeholder that does not resolve now fails loudly on the first
   > delivery instead of degrading into "the customer gets one answer and then
   > silence".

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
   different `comment_id`). **Do not skip this step.** It is the one that
   catches a broken comment-id placeholder (Step 7), and it is the step that
   was skipped: a single-comment smoke test passes perfectly against a
   trigger whose `comment_id` is always the empty string.
5. In Admin Center, open the webhook (Step 6) → **Recent deliveries** (or
   equivalent monitoring tab) to see Zendesk's own record of the request
   and response Zendesk received from your endpoint — this is the
   authoritative source if step 2's log line didn't appear (e.g. tunnel
   dropped) and shows the exact HTTP status your endpoint returned.

   The API is better than the UI here, because it hands you the **rendered
   request body** — which is the only way to see what a placeholder actually
   became:

   ```bash
   set -a; source .env; set +a
   WEBHOOK_ID=<the webhook's id, e.g. 01KZZFR8MFA0GNPKCP0F5WJWEM>
   curl -s -H "Authorization: Bearer $ZENDESK_OAUTH_TOKEN" \
     "https://$ZENDESK_SUBDOMAIN.zendesk.com/api/v2/webhooks/$WEBHOOK_ID/invocations"
   # then, for one invocation id from that list:
   curl -s -H "Authorization: Bearer $ZENDESK_OAUTH_TOKEN" \
     "https://$ZENDESK_SUBDOMAIN.zendesk.com/api/v2/webhooks/$WEBHOOK_ID/invocations/<invocation id>/attempts"
   ```

   `attempts[].request.payload` is the exact JSON Zendesk sent, placeholders
   resolved; `attempts[].response.payload` is the exact body your endpoint
   returned. Both survive a dropped tunnel, so this works retrospectively.

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

**A blank `comment_id` (or `ticket_id`) — `400`, and the body tells you
which placeholder to fix:**

```
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"1 validation error for ZendeskWebhookPayload\ncomment_id\n  Value error, comment_id is empty, so this event cannot be deduplicated. … use {{ticket.latest_comment.id}}, NOT {{ticket.latest_comment_id}} (which renders as the empty string). See docs/zendesk-runbook.md step 7. …"}
```

This is the tripwire for a Step 7 misconfiguration, and it is deliberately
a rejection rather than a best-effort accept — see
`backend/src/ingress/models.py`'s docstring for the tradeoff.

A malformed body (missing a required field, or not JSON at all) returns
`400`, never `500` — see `backend/tests/ingress/test_webhook.py` for the
exact cases this is tested against. Every `400`, and every delivery
discarded as a duplicate, now leaves a log line (ERROR and WARNING
respectively): a delivery that produces no run is never silent.

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
- **The first comment on a ticket gets answered and every follow-up is
  ignored**: this is the Step 7 comment-id placeholder. Read the delivered
  body from `…/invocations/<id>/attempts` (above) and look at `comment_id`.
  If it is `""`, the placeholder is wrong; if two different comments show the
  *same* non-empty id, it is pointing at something that isn't per-comment
  (e.g. a date). Ingress now answers such a delivery with a `400` naming this
  step, and logs a WARNING for every delivery it discards as a duplicate —
  `grep` the app log for `discarding webhook delivery` to see follow-ups
  being thrown away.
- **Trigger never fires**: check **Business rules → Triggers →
  (your trigger) → ... → View trigger revision history / Usage** or just
  re-open the ticket and confirm its tags — if a prior test run left
  `ai-processed` on a ticket you're reusing, the nullifying condition will
  correctly (and silently) block it. Use a fresh ticket.
