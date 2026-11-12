# ABRL web workspace

The Next.js application connects to the versioned FastAPI API. Request types in
`lib/schema.d.ts` are generated with `pnpm generate:api`; `lib/platform.ts` adds
response view models and form defaults. All requests include the selected
`X-Project-ID`, use the local bearer token when present, and include cookies for
configured OIDC deployments.

## Run locally

From the repository root:

```sh
pnpm --dir apps/web install
pnpm --dir apps/web dev
```

The UI listens on `http://127.0.0.1:8430`; the default API is
`http://127.0.0.1:8400`. Set `NEXT_PUBLIC_API_URL` at build time for a deployment.
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
  Browser preparation is explicitly none, automated, or manual. Confirmation
  defaults to 300 samples per selected browser group; concurrency remains bounded
  independently of total sample count.
- Profile details provide restore/reset and explicit, evidence-backed recovery for
  quarantined owners. The server remains authoritative for ownership fencing.
- The manual viewer fetches authenticated binary frames only while its detail
  panel is open. Claim, input, release, and handback use idempotent commands; the
  server enforces the single-controller lease. Session details keep provider
  verification, raw codes, acceptance decisions, ground truth, and retained
  authenticated artifacts separate.
- Sites and workflows retain their versioned editors, ownership verification,
  import/export, and preview. Site concurrency and instrumented acceptance gates
  are explicit settings. Additional navigation origins require ownership proof;
  resource-only origins cannot host documents or accept form submissions.
  Workflows include absence assertions for explicit failure guards and named
  checkpoints after successful assertions. Checkpoints record progress only:
  they do not reset action history, resume execution, or authorize replay.
- Schedules support one-time, interval and calendar triggers, IANA timezones,
  execution previews, overlap/misfire policy, expiration, versioned edits, and
  occurrence history. The UI does not own a scheduling timer.
- Research includes versioned, multi-workflow experiment definitions with declared
  hypotheses, baseline browsers, readiness validation, cloning, and immutable run
  history. Its decision reports show completion/evidence groups, uncertainty, first-attempt versus
  recovery, corrected pairwise tests, limitations, and immutable report downloads.
  Demonstration evidence is labeled and cannot qualify a live recommendation.
  Charts include verified verdict distribution, reliability intervals, observed
  completion trends, latency distributions, progress, matched environments, a
  paginated trial outcome matrix, and recorded attempt timelines. Missing worker
  utilization history is shown as unavailable.
  Entire-execution reports combine matched groups across batches, keep different
  target/workflow/environment conditions separate, and export all conditions.
  Run concurrency caps the entire experiment in addition to per-group limits.
- Network exposes versioned proxy policy constraints, product-specific inventory,
  safe drained inventory edits, capacity, and stored lease-selection rationales.
  Credentials are named environment references; missing costs remain unknown.
- Fleet and settings show persisted worker heartbeats, capacity hypotheses,
  adapter readiness reasons, project creation, current-project concurrency
  settings, and role membership administration.
- Lists use server-side search, filters, stable ordering, and pagination. Site
  and workflow endpoints retain their legacy array shape and expose filtered
  totals in `X-Total-Count`.
- The keyboard-accessible weekly schedule calendar shows each enabled schedule's
  persisted next occurrence from the current filtered page in Asia/Jerusalem.
  Coverage is explicit; future repeats are not invented by the client.
- Session configurations can be saved as immutable templates, loaded by version,
  and reused in batches, experiments, and schedules. Pinned settings are locked
  until explicitly detached; saving a revision pins current dependency versions.
  HTTP-request, transport, time, and concurrency budgets remain finite.
- Network capacity shows reserved connection units; each policy enforces its
  per-session connection load. Cost panels separate partial proxy/compute
  estimates from unknown total workflow costs and provider invoices.

## Verification

```sh
pnpm --dir apps/web typecheck
pnpm --dir apps/web lint
pnpm --dir apps/web build
uv run python apps/web/scripts/smoke.py
uv run python apps/web/scripts/platform_smoke.py
uv run python apps/web/scripts/experiments_smoke.py
uv run python apps/web/scripts/release_smoke.py
uv run python apps/web/scripts/workflows_smoke.py
uv run python apps/web/scripts/templates_smoke.py
```

The scripts use `ABRL_OPERATOR_TOKEN` from the root `.env` without printing it.
They require a running local API, scheduler, session worker, web server, and
Playwright Chromium. The platform script creates named `UI acceptance` demo
batches, persistent profiles, proxy policies, disabled proxy inventory, and
paused schedules. It validates a dropped post-commit response followed by an
idempotent retry, JSON report consistency, partial live execution when an adapter
is unavailable, keyboard access, and narrow-screen layout.

The 500-row virtualization and viewer command tests use isolated network fixtures;
they do not represent 500 live browsers or licensed-adapter validation. Screenshots
are written to `.abrl/ui-evidence` by default. Licensed-browser, live AdScore,
Oxylabs, OIDC, and cloud-scale verification require their external dependencies.
