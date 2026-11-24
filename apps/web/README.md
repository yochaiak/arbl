# ABRL web workspace

The Next.js application connects to the versioned FastAPI API. The
`pnpm generate:api` command generates `lib/schema.d.ts`. `lib/platform.ts` adds
response view models and form defaults. All requests include the selected
`X-Project-ID`, use the local bearer token when present, and include cookies for
configured OIDC sign-in.

## Run locally

From the repository root, start the frontend against a running local API.

```sh
pnpm --dir apps/web install
pnpm --dir apps/web dev
```

The UI listens on `http://127.0.0.1:8430`; the default API is
`http://127.0.0.1:8400`. Set `NEXT_PUBLIC_API_URL` at build time if you change the local API origin.
Keep the configured web origin, API CORS origin, and OIDC redirect origin aligned.
The local token is read from the bootstrap setup and retained in session storage.
Organization sign-in uses the API's OIDC flow.

## Workspace surfaces

- Overview combines legacy trial history with actual session, batch, and schedule
  summaries. Service failures render errors instead of invented readiness data.
- Sessions provides independent batches, filtered server pagination, a virtualized
  list, bulk stop, batch pause/resume/cancel, profiles and immutable snapshots.
  Batch creation details show each allocation and its errors; an idempotent retry
  retries only failed allocations, preserving every existing session.
  New batches are live. Screenshot, console, and JavaScript asset capture start
  enabled and have retention and opt-out settings. Mobile presets offer common
  phone and tablet viewport dimensions, plus touch emulation in Playwright.
  Adapters with User-Agent override support offer searchable presets and custom values.
- Profile details provide restore/reset and explicit, evidence-backed recovery for
  quarantined owners. Reused profiles keep their saved persistence. The server
  remains authoritative for ownership fencing.
- The manual viewer fetches authenticated binary frames only while its detail
  panel is open. Claim, input, release, and handback use idempotent commands; the
  server enforces the single-controller lease. Session details keep provider
  verification, raw codes, acceptance decisions, ground truth, and retained
  authenticated artifacts separate.
- Sites and workflows retain their versioned editors, import/export, and preview.
  Site concurrency and instrumented acceptance gates are explicit settings.
  Saved sites are immediately available for live runs. Resource origins support read-only subresources.
  Workflows include failure guards and named checkpoints after successful assertions.
  Activate named URL parameters on a Navigate step to save default query values.
  Preview runs a single live session and opens the same form and override fields
  as batch creation.
- Schedules support one-time, interval and calendar triggers, IANA timezones,
  execution previews, overlap/misfire policy, expiration, versioned edits, and
  occurrence history. Schedules can load immutable session template versions.
  A schedule paused during retirement of simulated execution or the Lab fixture
  becomes eligible to resume after an operator saves a valid live configuration.
- Runs retains historical workflow previews and their original result provenance.
- Network exposes versioned proxy policy constraints, product-specific inventory,
  safe drained inventory edits, capacity, and stored lease-selection rationales.
  Credentials are named environment references; missing costs remain unknown.
  Choose Decodo when registering US Residential inventory at `gate.decodo.com:7000`
  and when creating a policy for that inventory. Decodo policies support sticky
  sessions and require Decodo inventory for assignments.
- Fleet and settings show persisted worker heartbeats, capacity hypotheses,
  adapter readiness reasons, project creation, current-project concurrency
  settings, and role membership administration. Browser integrations show
  worker capacity and runtime verification separately from adapter settings.
- Lists use server-side search, filters, stable ordering, and pagination. Site
  and workflow endpoints retain their legacy array shape and expose filtered
  totals in `X-Total-Count`.
- The keyboard-accessible weekly schedule calendar shows each enabled schedule's
  persisted next occurrence from the current filtered page in Asia/Jerusalem.
  Coverage is explicit; future repeats are not invented by the client.
- Saving a schedule template revision pins the current dependency versions.
  Pinned settings stay locked until detached. HTTP-request, transport, time, and
  concurrency budgets remain finite.
- Network capacity shows reserved connection units; each policy enforces its
  per-session connection load. Cost panels separate partial proxy/compute
  estimates from unknown total workflow costs and provider invoices.

## Verification

```sh
pnpm --dir apps/web typecheck
pnpm --dir apps/web lint
pnpm --dir apps/web build
```

Start `make dev-fixtures` for the browser smoke scripts. It uses the same ports
as `make dev`.

```sh
uv run python apps/web/scripts/smoke.py
uv run python apps/web/scripts/platform_smoke.py
uv run python apps/web/scripts/release_smoke.py
uv run python apps/web/scripts/workflows_smoke.py
uv run python apps/web/scripts/templates_smoke.py
uv run python apps/web/scripts/adapter_alignment_smoke.py
```

The scripts use `ABRL_OPERATOR_TOKEN` from the root `.env`. They require the
fixture-enabled local API, scheduler, session worker, web server, and Playwright
Chromium. The platform script creates named live fixture batches, persistent
profiles, proxy policies, disabled proxy inventory, and a paused schedule. It
checks retry after a dropped response, mixed adapter availability, keyboard
access, and narrow-screen layout. The workflow script checks named URL overrides,
single-session preview, and downloads the default capture artifacts.
The adapter alignment smoke uses the seeded fixture site and workflows for a
mixed-adapter preflight. Its session detail checks use isolated UI fixtures.

The 500-row virtualization and viewer command tests use isolated network fixtures.
Screenshots are written to `.abrl/ui-evidence` by default. Licensed-browser, live
AdScore, Oxylabs, and OIDC verification require their external dependencies.
