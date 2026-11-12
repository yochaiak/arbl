# AdScore diagnostics

The Node service listens on loopback and handles signatures from a single configured AdScore zone. The FastAPI application uses it for manual diagnostics. The service reports `pending_smoke_test` until it verifies a signature against the configured zone and trusted request metadata.

Install Node 22.15 or later. Supply the following environment variables to both the application and verifier where applicable.

| Variable | Purpose |
| --- | --- |
| `ABRL_VERIFIER_TOKEN` | Shared internal bearer token, at least 32 characters. Generate a separate random value. |
| `ABRL_VERIFIER_PORT` | Loopback verifier port. Defaults to `8001`. |
| `ABRL_VERIFIER_URL` | FastAPI's verifier URL. Defaults to `http://127.0.0.1:8001`. |
| `ABRL_ADSCORE_ZONE_ID` | Numeric zone ID. |
| `ABRL_ADSCORE_RESPONSE_KEY` | Base64-encoded zone response key for the verifier only. |
| `ABRL_ADSCORE_ALGORITHM` | `v5_0101M` (default), `v4_hashing`, or `v4_signing`. `v4` is an alias for `v4_hashing`. |
| `ABRL_ADSCORE_ZONE_KEY` | Browser-facing Zone API Key for the manual test page. |
| `ABRL_MANUAL_ORIGIN` | The exact authorized HTTPS origin exposing the manual diagnostic page. |

Run from this directory with environment variables already loaded:

```sh
npm ci
npm start
```

Keep both services bound to loopback. To enable the optional manual test, route the diagnostic pages through an authorized HTTPS reverse proxy and configure Uvicorn to trust proxy headers only from that proxy's address. Have the proxy replace incoming forwarded headers with metadata from its trusted peer. The application needs the actual public browser IP and the original User-Agent. Loopback or private client addresses leave the manual check unavailable.

Visit `/lab/manual` in the browser on the configured HTTPS origin and start a check with the operator token and consent. The browser loads AdScore only after this action. The app records the verification state and, after successful verification, the vendor code and its label. It does not persist the signature, client IP, or User-Agent. The page reports whether the diagnostic was recorded and verified.

The built-in page uses the ordinary callback from the [AdScore JavaScript API](https://docs.adscore.com/js-api/). It requires a zone compatible with that callback configuration. A zone configured to require request signatures needs an integration that provides those signatures. Response verification uses the [official Node SDK](https://github.com/Adscore/nodejs-common). The tests use local requests and fixture responses. A real smoke test requires an eligible zone, valid credentials, and a correctly configured HTTPS origin.
