# Instrumented session observations

An instrumented session can attach an AdScore observation from an authorized site's backend. ABRL verifies the provider signature against trusted request metadata. Configure the internal verifier with a compatible response key and zone. Keep the verifier endpoint, token, and response key on the server. [Verifier setup](../apps/verifier/README.md).

## Site setup

1. Register the authorized HTTPS site as **Instrumented**. Register the navigation and resource origins required by your integration. New instrumented sites include `https://c.adsco.re` for the provider script.
2. Set a strong `ABRL_CORRELATION_SECRET` on the API and session runners. Set a separate `ABRL_OBSERVATION_CONNECTOR_TOKEN` of at least 32 characters on the API and on the site's backend. Use the connector token for server-to-server authentication.
3. Retain the runner's `abrl_context` parameter from the first navigation with the site's workflow request. This signed, short-lived context identifies the live session, attempt, and ownership epoch. Reserve the parameter name for the runner.
4. Send the provider signature to the site's backend through your integration. Use the actual public request IP and User-Agent from trusted server metadata. If a reverse proxy supplies client metadata, configure the site server to trust only the designated proxy and have the proxy replace incoming forwarded headers.

## Submit an observation

The trusted site backend sends the context, signature, and metadata through the authenticated connector.

```http
POST /api/v1/integration-observations
Authorization: Bearer <ABRL_OBSERVATION_CONNECTOR_TOKEN>
Content-Type: application/json
```

```json
{
  "context": "signed context supplied for this attempt",
  "signature": "provider signature",
  "idempotency_key": "one stable key per attempt observation",
  "client_ip": "public address obtained from trusted server metadata",
  "user_agent": "User-Agent obtained from the same request"
}
```

The connector records one immutable observation per active instrumented attempt. An identical idempotent request returns the existing observation. A changed payload using the same key conflicts. Project members can read observations from `GET /api/v1/sessions/{session_id}/observations`.

Use the returned `accepted` decision to gate the site's workflow action. The default `verified_clean` gate accepts a verified raw code `0`, which is the provider's **Clean** verdict. `observe_only` accepts a recorded observation regardless of the verdict. ABRL records workflow completion and provider verification independently. A single Navigate step waits up to 40 seconds within the workflow time budget for the site to submit an observation. End a multi-step workflow with an explicit success assertion.

## Confirm readiness

Run an instrumented session against the authorized HTTPS site and check the session's observation. Successful live signature verification moves the configured verifier from `pending_smoke_test` to `ready`. The authenticated API reports verifier readiness at `/api/v1/status`.

Session details include a bounded browser network trace for instrumented workflows. It distinguishes browser responses and failures from policy blocks and links consecutive requests in an HTTP redirect. The trace records sanitized origins and paths and omits credentials, query strings, fragments, headers, and bodies.

Automated connector tests use fixture responses. Live verification uses the site's compatible zone and trusted public request metadata. Preserve raw provider codes, including future codes unknown to the application. [Official AdScore verdict definitions](https://github.com/Adscore/pyadscore/blob/master/src/pyadscore/definitions/judge.py), [official Node SDK](https://github.com/Adscore/nodejs-common), [JavaScript API](https://docs.adscore.com/js-api/).
