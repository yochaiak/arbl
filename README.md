# ABRL · Browser Workspace

ABRL runs and schedules browser workflows on authorized sites. The workspace manages independent browser profiles, live sessions, and retained artifacts. Instrumented sites can attach server-verified AdScore observations to their sessions.

ABRL runs locally. Docker Compose runs PostgreSQL, Redis, ClickHouse, and SeaweedFS. The API, scheduler, workers, browser adapters, and web app run on the host so they can use installed browsers and vendor engines.

## Run locally

Install Docker with Compose, uv, Node.js 24, pnpm, Make, and FFmpeg with the `libx264` encoder. On Linux install browser system dependencies with `uv run playwright install --with-deps chromium` after syncing dependencies.

```sh
make setup
make dev
```

Open **http://localhost:8430**. Use the local `ABRL_OPERATOR_TOKEN` from `.env`; never paste it into a workflow. The API binds to **http://127.0.0.1:8400**. Configuration generates local credentials and encryption keys with owner-only permissions. Keep recovery copies of profile encryption keys: losing a key makes its snapshots unrecoverable.

`make dev` starts the API, dedicated scheduler, short-task worker, standard and vendor session runners, and frontend. To run them separately, use `make infra migrate`, then `make api`, `make scheduler`, `make worker`, `make sessions`, `make sessions-vendor`, and `make web`. Start the optional AdScore verifier with `make verifier` after configuring it. `make down` stops data services without deleting volumes.

1. Register an HTTPS site you control or have permission to test. It becomes available for live runs when you save it.
2. Build a workflow. A single Navigate step can check a page load. Finish workflows with multiple steps with a success assertion. Use `ABRL_SECRET_<NAME>` references for sensitive input.
3. Use **Preview workflow** to run one live session or **Sessions → Create batch** to select adapters, profiles, a proxy policy, and concurrency. Manual Chrome opens an operator handoff before the workflow. The server assigns and retains a random seed for each new batch.
4. Create one-time, interval, or calendar schedules with an IANA timezone. Jerusalem is the default. Schedules can reuse pinned templates, and their occurrence history shows overlaps and missed executions. Edit and resume a paused schedule once you have selected a valid live configuration.
5. Inspect sessions, attempts, observations, and encrypted artifacts. Use **Play recording** to replay completed browser attempts.

New batches and schedules capture screenshots, console logs, and JavaScript assets by default. Configure capture and retention when setting up the batch or schedule. The JavaScript ZIP in **Retained artifacts** contains script response bodies, runtime source, and Chromium execution evidence. Its manifest reports missing sources and coverage gaps. Each ZIP is limited to 8 MiB and follows the session's artifact retention.

## Browser and network configuration

Stock Playwright runs without vendor credentials. Manual Chrome requires installed Chrome and a person controlling it. CloakBrowser and Kameleo use their documented SDKs, explicit license/runtime readiness and actual recorded versions. See [browser and proxy contracts](docs/browser-proxy.md).

Phone and tablet presets give all four adapters the same viewport dimensions. Playwright also applies mobile and touch emulation. The User-Agent picker supports the browser default, searchable presets, and a custom value for adapters that advertise override support. Session details display the effective device mode and browser engine.

Proxy inventory supports Oxylabs and Decodo US Residential. It stores named credential references, product, geography, capacity and continuity limits. Policies select a provider and explain selection using eligibility, measured transport health, utilization and known cost. Detector verdicts never influence proxy health or preparation. Authentication/quota failures pause inventory. Transport recovery is bounded; site denial does not trigger rotation. Uncertain submitted actions require reconciliation.

Live browser navigation is limited to the registered primary and additional navigation origins. Resource origins are configured separately. Direct connections pin validated public addresses. Proxy hostname resolution remains an explicit upstream trust boundary.

A Navigate step can use case-sensitive names such as `[CAMPAIGN_ID]`, `[QUERY]`, and `[SUBID]` as complete query values. Enter a URL such as `/qa/check?term=[QUERY]&campaign=[CAMPAIGN_ID]`, enable **Use named URL parameters**, and save defaults for the step. The batch form accepts overrides and previews the encoded URL. A blank override uses the saved default for each step.

Use the **Structured workflow** editor for typed inputs, reusable project blocks, conditions, repeats, and named tabs. A text capture can feed a later step in the same attempt. Use secret references for confidential inputs. An explicit final check verifies the workflow result, and ordinary automated workflows can report that their steps completed. Manual Chrome requires a final check and hands browser actions to the operator.

The batch form checks workflow, browser, profile, and proxy settings before launch. A timing-only schedule edit keeps pinned versions, and **Refresh pinned dependency versions** loads the latest revisions for schedules configured with shared session settings.

## Instrumented sessions

Browser-only targets report observable workflow completion. Instrumented sites attach trusted request metadata and a provider signature to a signed session context through the authenticated site connector. The verifier records its result and raw provider code. The default acceptance gate requires a verified **Clean** result with raw code `0`.

Follow the [verifier guide](apps/verifier/README.md) and [integration contract](docs/instrumented-research.md). A compatible zone, public authorized HTTPS integration and successful live smoke test are required before readiness. Local networking never disables IP or User-Agent checks.

## Local access and roles

The local operator token and optional OIDC sign-in use project memberships with owner, operator, analyst, and viewer roles. A manual browser has one authenticated controller and an explicit handoff. Runner capacity depends on the local host, browser compatibility, and vendor licenses.

## Validate

```sh
make test
make lint typecheck
make build
make migration-test                   # fresh temporary PostgreSQL database
pnpm --dir apps/web generate:api      # regenerate contracts after API changes
```

Start `make dev-fixtures` for integration tests, the Chromium save and restore check, and fixture-based UI smoke scripts. It enables the local test fixture and seeding endpoint. Both development commands use the same ports.

```sh
make integration-test
make profile-test
PYTHONPATH=. uv run python apps/web/scripts/platform_smoke.py
PYTHONPATH=. uv run python apps/web/scripts/workflows_smoke.py
```

`make profile-test` verifies that two independent Chromium profiles preserve their visit cookies after restore and records the snapshot generations in `.abrl/evidence/session-roundtrip.json`.

See [architecture](docs/architecture.md) for persistence, fencing, event delivery and operational limits. Historical sessions retain their original source and observations.
