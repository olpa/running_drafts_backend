# Test-control handover to the product E2E journey

This handover connects backend
[`olpa/running_drafts_backend#27`](https://github.com/olpa/running_drafts_backend/issues/27),
the sandbox-manager extension
[`olpa/running_drafts_product#22`](https://github.com/olpa/running_drafts_product/issues/22),
and the data-bearing happy path
[`olpa/running_drafts_product#4`](https://github.com/olpa/running_drafts_product/issues/4).

## Required sequencing

1. Land backend #27 so the selected backend checkout exposes the settled
   challenge, reset, and create contracts.
2. Land product #22 so sandbox creation derives an instance control token and
   writes only the ready-to-send `X-Test-Control` header to the protected
   credentials file.
3. Extend product #4's existing `e2e/test_happy_path.py` through the
   authenticated data-bearing journey.

Do not add challenge handling or HMAC derivation to pytest. If #22 is not
available, keep #4 blocked on that dependency rather than introducing a second
credential path.

## Consumer boundary

The sandbox description supplies `backendUrl` and `credentialsFile`. Read the
protected credentials document and use
`backendTestControl.headers` unchanged. Its expected shape is:

```json
{
  "schemaVersion": 1,
  "backendTestControl": {
    "headers": {
      "X-Test-Control": "<derived instance token>"
    }
  }
}
```

Never print, snapshot, attach, or otherwise retain the header value. Pytest must
not receive either configured backend secret, call `/challenge`, derive a token,
access SQLite, invoke backend tools, or gain process knowledge.

## Per-test fixture lifecycle

Apply this lifecycle to every data-bearing E2E test, whether pytest created the
sandbox or received an existing sandbox ID:

1. Use an independent HTTP client to call
   `POST {backendUrl}/api/v1/test-control/reset` with the protected headers.
   Require HTTP 200 and an exact `{"status":"reset"}` body.
2. Create a fresh, non-persistent Playwright browser context.
3. Call `POST {backendUrl}/api/v1/test-control/create` through that browser
   context's associated request client, again with the protected headers. This
   installs the normal HttpOnly `running_drafts_session` cookie directly into
   the context without exposing it to page JavaScript.
4. Require HTTP 200, `status: created`, Account ID
   `4316a559-d21f-5ee7-8a0a-9d09039f79ba`, and a parseable
   `sessionExpiresAt`. Confirm ordinary authentication through `/api/v1/me`
   rather than inspecting the cookie.
5. Run the browser journey.
6. In `finally`, call `/reset` through the independent HTTP client and validate
   the same exact success response. Do this before relinquishing access to the
   protected credentials, even if browser setup or the journey failed.

The independent cleanup client is intentional: final cleanup must still work
if a browser or browser context has crashed or already closed. If both the
journey and final cleanup fail, report both without replacing the original test
failure.

## Fresh-browser sign-in

After Browser A logs out and closes, create Browser B with a fresh
non-persistent context. Call `/create` through Browser B's associated request
client. Do not copy cookies, local storage, session storage, or Playwright auth
state from Browser A.

Repeated `/create` preserves the fixed fixture Account's Projects and other
owned product data while issuing a new ordinary Application Session. This lets
Browser B find the Project created by Browser A. `/reset` remains the only test
operation that deletes fixture-owned product state and invalidates all of its
sessions.

## Sandbox ownership

Keep the lifecycle rules established by product #18:

- stop a pytest-created sandbox during teardown unless `--keep-sandbox` was
  selected;
- never stop, repair, or prune an explicitly supplied sandbox;
- always perform the data reset described above, regardless of who owns the
  sandbox process lifecycle.

Fixture cleanup and sandbox shutdown are separate responsibilities. A failed
fixture cleanup does not grant permission to stop an externally owned sandbox.

## Journey and evidence

Extend the single existing `e2e/test_happy_path.py`; do not create a competing
happy-path suite. Preserve the product #4 requirements for:

- Chromium and Firefox runs;
- multiple acknowledged transport frames and a final partial tail;
- mono WebM/Opus capture;
- exact ordered-byte or SHA-256 equality after download;
- decode, continuity, duration, and content checks;
- logout followed by a genuinely fresh browser context;
- no inference, transcription, normalization, or persistent assembled-file
  dependency.

Standard pytest reporting remains authoritative. Evidence and diagnostics may
identify the sandbox, backend/editor provenance, safe URLs, and log directory,
but must never contain control headers, configured secrets, challenges, derived
tokens, session cookies, provider tokens, or provider claims.
