# ABRL implementation and release evidence

Date: 2026-11-12. This delivery extends the existing application with durable session management, scheduling, instrumented research, team access and an executable Hetzner deployment. It can run locally now. Production and provider acceptance remain open until the external gates in the [requirement ledger](requirements.md) are exercised.

## Delivered behavior

- Independent profiles, launches, assignments and attempts; encrypted profile generations; exclusive ownership epochs and quarantine after uncertain runtime termination.
- Bulk batches with per-item allocation errors, idempotent creation and explicit creation retry. Retrying allocation cannot recreate a session that has already launched. Incomplete allocations remain visible and enter research reliability denominators.
- Playwright, native person-controlled Chrome, CloakBrowser and Kameleo lifecycle adapters. Optional seeded preparation records its actions as automated activity. Manual control is authenticated, exclusive and explicitly handed back.
- Product-aware Oxylabs inventory and versioned policies, explainable selection, dedicated credential-holding egress brokers and bounded transport recovery. Site denials and detector verdicts do not trigger proxy rotation. Ambiguous external actions require reconciliation.
- Reusable versioned session templates with immutable site/workflow/proxy pins; standalone launches and schedules retain their selected template revision.
- Dedicated PostgreSQL-led scheduler, pinned definitions, Jerusalem DST behavior, overlap/misfire policies, previews and occurrence history.
- Versioned experiment definitions and seeded ordering across their groups; comparisons keep target, workflow, environment, evidence source and research phase separate. Immutable reports preserve traceability to logical samples and attempts.
- Server-only AdScore verification, trusted request metadata, signed trial correlation, raw vendor codes and the default verified-Clean action gate. Browser-only completion remains independently useful and does not imply an AdScore verdict.
- Wilson intervals, cluster-aware uncertainty, corrected pairwise comparisons, first-attempt versus recovered reliability, operational denominators, cost uncertainty and explicit eligibility rules. Demo, pilot and incomplete evidence cannot produce a final live recommendation.
- Finite HTTP request and broker traffic budgets, measured usage, explicitly estimated proxy cost components and observed-node fleet costs. Unknown components prevent an invented total.
- Project roles, cloud OIDC, encrypted transient manual input, authenticated artifacts, structured logs, tracing, durable outboxes and database-derived fleet metrics.
- Consistent dark interface with session/batch management, profiles, workflows, schedules, experiments, comparisons, network policy explanations, worker capacity and team settings. Lists are paginated/virtualized; manual frames are fetched only for the open viewer.
- Terraform fixed foundation and Kubernetes manifests for HA K3s, CloudNativePG, Sentinel, ClickHouse/Keeper, KEDA and Hetzner Cluster Autoscaler. Separate browser/broker pods, finite admission limits, encrypted cross-location backups, restore tooling and staged capacity scripts are included.

## Local operation

```sh
make setup
make dev
```

The UI is at `http://localhost:8430`, API at `http://127.0.0.1:8400`. Bootstrap uses the local operator token already in the ignored `.env`. No token is included in this report or repository. `make dev` starts the API, scheduler, short-task worker, standard/vendor session runners and frontend alongside Compose data services.

Each site must be owned or explicitly authorized. Additional navigation origins require their own ownership marker; resource permissions do not authorize navigation. New runs pin immutable site/workflow versions. The workflow builder supports explicit presence, text and absence assertions plus named confirmed checkpoints. A checkpoint records evidence; it does not grant permission to replay a transaction.

## Validation

The final full Python run passed **248 tests**. A clean detached checkout of backend commit `11ad011` independently passed **215 non-integration tests** without a local `.env`; the 32 integration tests were included in the full local-stack run. All three Node verifier tests passed.

Final command results are recorded in `release-validation.json` next to this report. The validation performed during delivery includes:

| Check | Observed scope |
|---|---|
| Python test suite | Persistence, roles, ownership fencing, schedules/DST, proxy constraints, real local/remote Chromium, encryption, statistical reference data, recommendation eligibility and cloud contracts |
| Official Node verifier tests | Signature normalization/configuration and error behavior; no live zone |
| Fresh PostgreSQL migration | Empty temporary database upgraded through every revision; Alembic schema comparison clean; temporary database removed |
| Frontend TypeScript, ESLint and production build | Generated OpenAPI client and all application pages |
| Five browser smoke scripts | Registry/workflow preview, batch/session flows, partial adapter availability, profile controls, versioned network/schedules, experiments/reports, quotas, keyboard and narrow-screen layouts |
| Actual persistent-browser roundtrip | Two independent native Chromium profiles, successful save, restore, second execution and second immutable generation; four completed sessions |
| API exports | JSON/CSV/HTML/PDF produced from the same completed immutable snapshot; simulated provenance retained |
| PDF visual QA | Rendered every page of the retained report fixture; reviewed page images |
| Terraform/Helm/operator schemas | Terraform initialization/validation, chart lint/render, six pinned operator charts and six official CRD schemas |
| Docker runtime | API/web/verifier builds; non-root import checks; sandboxed amd64 Chromium execution |

[Local browser evidence](local-browser-evidence.json) records actual browser version, OS, architecture, ownership epoch, preparation and profile restoration without credentials. [Infrastructure evidence](../../deployment/validation.local.json) records container IDs and source hashes. [Batch report fixture](research-report.pdf) and [four-page experiment report](experiment-run.pdf) and its rendered pages are explicitly simulated; they demonstrate report correctness, not browser superiority.

The independent release review identified and fixed paused-batch reopening, stale manual control, expired command input, quarantined capacity undercounting, cancelling-overlap dispatch, artifact publication after lease loss, failed-commit ciphertext cleanup, and conflation of workflow completion with acceptance. A subsequent audit added missing residential sticky/strict wiring, resumable allocation failures, run-wide comparisons/order, workflow guards/checkpoints, additional navigation origins and a cloud headed-Chrome image.

## Remaining acceptance gates

No live Hetzner resources were provisioned. No genuine human participant study, live Oxylabs run, licensed CloakBrowser/Kameleo run, or verified AdScore zone result is asserted.

External prerequisites are Hetzner account access and quotas, DNS/TLS, OIDC tenant configuration, object-storage credentials and independently retained recovery keys, provider access, eligible browser licenses and a compatible AdScore test zone. Configuration files and preflight tools expose these requirements before deployment.

After configuration, execute fresh deployment, network isolation probes, cross-worker restore, scale-up/drain, node/partition/database failure tests and restoration into a separate environment. Then run 100 → 250 → 500 **real** browser capacity stages against owned fixtures and separately measure each licensed adapter at its available entitlement. The two-minute node recovery, zero acknowledged metadata loss under synchronous policy, 15-minute regional RPO and four-hour RTO remain targets until those timed rehearsals pass.

Local Mac workers enforce browser routing but are not an OS-level network sandbox. Cloud NetworkPolicy enforcement still needs deployment probes. Proxy-side DNS remains an explicit provider trust boundary. Profile restoration requires a tested matching browser/OS/architecture boundary. Known costs are reported; missing prices remain unknown. Broad accessibility certification and independent statistical review are not claimed by the local smoke tests.

The final research recommendation depends on completed live confirmation evidence for the selected site, workflow and environment. No candidate has been promoted from demonstration data.
