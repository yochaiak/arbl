# Instrumented observation integration

AdScore verification is server-only. Deploy the official Node verifier with a compatible response key and zone; V5 is the new-configuration default. Do not expose its token, response key, or service endpoint to browser code. The deployment runs it as a loopback sidecar so the Python verifier connector keeps its internal-origin restriction.

## Controlled fixture

For a cloud fixture set `ABRL_CONTROLLED_ORIGIN` to its exact deployed HTTPS origin in the shared API/runner runtime configuration. That deployment-owned origin receives the `/lab/research` integration path and does not need a separately hosted ownership marker. Other sites use the authenticated site connector described below.

An instrumented session carries a signed short-lived context containing its session ID, attempt ID and ownership epoch. `/lab/research` binds this context to the configured target origin, sets an HTTP-only context cookie, loads the documented AdScore callback, and posts the signature back to the site server. The server obtains the client IP and User-Agent from trusted request metadata and calls the verifier. A positive client callback with failed verification is recorded as failed verification, never Clean.

After recording an observation, the browser continues to the declared workflow. `/lab/form` checks the active research context and the pinned acceptance gate. `verified_clean` requires verified raw code0; `observe_only` records the verdict while allowing benchmark execution. The default is `verified_clean`. Ordinary browser-only fixtures remain usable without fabricated provider observations.

The local loopback environment cannot supply the public client metadata required by AdScore. Use an owned HTTPS integration and correctly trusted proxy metadata; never disable IP or User-Agent checks. The Hetzner ingress configuration forwards the load balancer PROXY protocol only from its fixed private address.

## External authorized site

The runner appends `abrl_context` to the first navigation URL. The owned site's backend receives it and must retain it with the benchmark request. After the documented AdScore JavaScript callback obtains a signature, forward it to your own backend. Only that trusted backend calls:

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

The public browser must never possess the connector token or supply trusted IP metadata. Do not forward untrusted `X-Forwarded-For` chains. On the controlled callback, client-supplied metadata is ignored in favor of the actual trusted request. A changed idempotency payload conflicts; another observation cannot replace a trial's negative verdict. Wrong/expired attempts and browser-only or simulated sessions cannot ingest live provider evidence.

Store and use the returned `accepted` decision to gate your benchmark action. Keep the workflow success assertion independent from verification: both are required for the primary instrumented research outcome. Register exact additional resource origins needed by your integration. New instrumented targets include `https://c.adsco.re` for the documented script; other provider resources require explicit permission.

## Readiness and evidence

A configured key is insufficient to mark the integration ready. Complete the live verification smoke test described in `apps/verifier/README.md`. Store actual browser/version, public IP routing configuration, target revision and traceable observation IDs. The official definitions label0 Clean,3 Potentially unwanted,6 Proxy and9 Bot; preserve all raw codes, including unknown future codes. Clean is not a ground-truth claim about a human operator.

Automated connector tests cover successful/failed verification, negative and unknown verdicts, duplicate observations, request metadata binding and gate behavior. They are distinct from live AdScore verification, which requires your compatible zone.

Sources: [official AdScore verdict definitions](https://github.com/Adscore/pyadscore/blob/master/src/pyadscore/definitions/judge.py), [official Node SDK](https://github.com/Adscore/nodejs-common), [JavaScript API](https://docs.adscore.com/js-api/).
