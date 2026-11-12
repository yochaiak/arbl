# ABRL · Browser Research Lab

ABRL runs authorized browser workflows, manages independent browser profiles and sessions, schedules reproducible experiments, and compares observable completion with server-verified AdScore evidence. Simulated data is explicitly labeled and cannot produce a live recommendation.

The application supports local macOS development and an isolated Kubernetes browser execution architecture for Hetzner. Source implementation, automated validation, live validation, and outstanding prerequisites are tracked separately in the [delivery ledger](docs/delivery/requirements.md). Infrastructure manifests passing validation do not establish production recovery or 500-browser capacity.

## Run locally

Install Docker with Compose, uv, Node.js 24, pnpm, and Make. On Linux install browser system dependencies with `uv run playwright install --with-deps chromium` after syncing dependencies.

```sh
make setup
make dev
```

Open **http://localhost:8430**. Use the local `ABRL_OPERATOR_TOKEN` from `.env`; never paste it into a workflow. The API binds to **http://127.0.0.1:8400**. Configuration generates local credentials and encryption keys with owner-only permissions. Keep recovery copies of profile encryption keys: losing a key makes its snapshots unrecoverable.

`make dev` starts the API, dedicated scheduler, short-task worker, standard and vendor session runners, and frontend. To run them separately, use `make infra migrate`, then `make api`, `make scheduler`, `make worker`, `make sessions`, `make sessions-vendor`, and `make web`. `make down` stops data services without deleting volumes.

1. Load the starter suite, or register an owned/authorized HTTPS site and verify its ownership marker.
2. Build a workflow with an explicit success assertion. Use `ABRL_SECRET_<NAME>` references for sensitive input.
3. Save or load a versioned session template, then create a batch with independent profiles, browser distribution, persistence, preparation, proxy policy, concurrency, and evidence mode. Automated preparation defaults off; it remains automated activity.
4. Save an experiment to pin multiple workflow groups, validate readiness, then execute. Draft revisions and execution history remain separate. Pilot defaults are 30 trials per candidate; confirmation is a separately declared run.
5. Create one-time, interval, or calendar schedules with an IANA timezone. Jerusalem is the default. Review occurrence history, overlaps and missed-execution policies.
6. Inspect sessions, attempts, observations, encrypted artifacts and comparisons. Export immutable JSON/CSV/HTML/PDF reports.

## Browser and network configuration

Stock Playwright runs without vendor credentials. Manual Chrome requires installed Chrome and a person controlling it. CloakBrowser and Kameleo use their documented SDKs, explicit license/runtime readiness and actual recorded versions. See [browser and proxy contracts](docs/browser-proxy.md).

Oxylabs inventory stores named credential references, product, geography, capacity and continuity limits. Policies explain selection using eligibility, measured transport health, utilization and known cost. Detector verdicts never influence proxy health or preparation. Authentication/quota failures pause inventory. Transport recovery is bounded; site denial does not trigger rotation. Uncertain submitted actions require reconciliation.

External navigation is restricted to the primary origin and separately ownership-verified navigation origins. Resource origins are separately allowed. Direct connections pin validated public addresses. Oxylabs hostname resolution remains an explicit upstream trust boundary. Cloud browsers and brokers run in separate pods with session-specific network policies.

## Instrumented research

Browser-only targets report observable completion and unavailable AdScore evidence. Instrumented targets correlate signed context with a session and attempt, obtain trusted request IP/User-Agent, and use the internal official Node SDK verifier. Code `0` means **Clean**, not human. The default controlled-action gate requires a verified Clean result. Workflow outcomes, verification, raw codes, acceptance, ground truth and provenance remain separate.

Follow the [verifier guide](apps/verifier/README.md) and [integration contract](docs/instrumented-research.md). A compatible zone, public authorized HTTPS integration and successful live smoke test are required before readiness. Local networking never disables IP or User-Agent checks.

Reports include operational failures, first-attempt reliability, recovery, verification coverage, raw verdict distributions, Wilson intervals, cluster-aware analysis, corrected pairwise comparisons and unknown costs. A completed live confirmation qualifies only when its uncertainty bound meets the selected threshold. Ties and insufficient evidence are explicit.

## Team and cloud deployment

Cloud deployments require OIDC and project memberships with owner/operator/analyst/viewer roles. Local bootstrap authentication is disabled in cloud mode. Viewers cannot issue lifecycle commands; manual control has one authenticated operator and explicit handoff.

The [deployment guide](docs/deployment.md) covers Terraform, HA K3s, CloudNativePG, Sentinel, ClickHouse/Keeper, KEDA, Hetzner Cluster Autoscaler, encrypted backups and telemetry. [Recovery procedures](docs/recovery.md) cover failover and restore. Maximum admission is 500 sessions and Terraform limits elastic pools to 80 aggregate browser nodes. Monetary cost is visible when inputs exist; no monetary cap is assumed.

Hetzner credentials/quotas, DNS, OIDC, object storage/recovery keys, Oxylabs and vendor licenses are external prerequisites. Capacity, failover, recovery objectives and live licensed integrations require actual deployment evidence.

## Validate

```sh
make test
make lint typecheck
make build
make integration-test                 # API, scheduler, workers and Compose running
make migration-test                   # fresh temporary PostgreSQL database
make profile-test                     # real Chromium save/restore on owned local fixture
pnpm --dir apps/web generate:api      # regenerate contracts after API changes
PYTHONPATH=. uv run python apps/web/scripts/platform_smoke.py
```

See [architecture](docs/architecture.md) for persistence, fencing, event delivery and operational limits. All source changes are additive; historical browser-only observations are not assigned invented session identities or verdicts.
