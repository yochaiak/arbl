# AdScore verifier

The Node service listens on loopback and verifies signatures from a single configured AdScore zone. The FastAPI application sends requests from the authenticated site observation connector. The verifier reports `pending_smoke_test` until it verifies a signature against the configured zone and trusted request metadata.

Install Node 22.15 or later. Supply the following environment variables to both the application and verifier where applicable.

| Variable | Purpose |
| --- | --- |
| `ABRL_VERIFIER_TOKEN` | Shared internal bearer token, at least 32 characters. Generate a separate random value. |
| `ABRL_VERIFIER_PORT` | Loopback verifier port. Defaults to `8001`. |
| `ABRL_VERIFIER_URL` | FastAPI's verifier URL. Defaults to `http://127.0.0.1:8001`. |
| `ABRL_ADSCORE_ZONE_ID` | Numeric zone ID. |
| `ABRL_ADSCORE_RESPONSE_KEY` | Base64-encoded zone response key for the verifier only. |
| `ABRL_ADSCORE_ALGORITHM` | `v5_0101M` (default), `v4_hashing`, or `v4_signing`. `v4` is an alias for `v4_hashing`. |

Run from this directory with the environment variables loaded.

```sh
npm ci
npm start
```

Keep the verifier bound to loopback. Configure your authorized HTTPS site's backend to submit provider observations to ABRL. Use the actual public client IP and original User-Agent from trusted site-side request metadata. [Observation integration](../../docs/instrumented-research.md).

Run a live instrumented session against the site and confirm the session contains a verified observation. Check verifier readiness in the workspace or at the authenticated `/api/v1/status` endpoint. The integration uses a compatible AdScore zone and its documented signature flow. [AdScore JavaScript API](https://docs.adscore.com/js-api/), [official Node SDK](https://github.com/Adscore/nodejs-common).

Run `npm test` for verifier checks with local requests and fixture responses. Live verification requires an eligible zone, valid credentials, and the authorized HTTPS integration.
