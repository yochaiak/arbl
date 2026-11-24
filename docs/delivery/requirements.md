# Historical ABRL delivery evidence ledger

This document records the November 12, 2026 delivery. The current workspace runs locally with Docker Compose. Historical deployment and research entries here describe that earlier delivery.

Last reviewed: 2026-11-12. This ledger distinguishes source implementation, automated checks, observed local execution, and external acceptance gates. **The release is not yet live-certified for Hetzner, licensed browsers, Oxylabs, or AdScore.** No simulated result counts as a live qualification.

“Implemented” means an executable source path exists. “Tested” means the cited check exercised the stated behavior; it does not imply every possible failure was reproduced. “Local” means observed execution on this Mac or its Docker runtime. “Pending” remains an open acceptance gate, not a pass.

## Application, identity, and execution

| Requirement | Implemented | Automatically tested | Live verified | Remaining gate or boundary |
|---|---|---|---|---|
| Additive migrations; preserve historical browser-only records | Yes; nine new Alembic revisions | Fresh PostgreSQL upgrade and schema comparison | Local database migration | Production backup and upgrade rehearsal |
| Projects, roles, membership, local bootstrap | Yes; owner/operator/analyst/viewer | `test_platform.py` role/project isolation; UI permission controls | Local bootstrap and project UI | Real team OIDC login/refresh/logout pending configured issuer |
| Cloud OIDC and explicit bootstrap identity | Yes; cloud preflight rejects missing configuration | Infrastructure configuration and source checks | Pending | OIDC tenant, redirect URI, bootstrap subject |
| Reusable session templates and immutable revisions | Yes; launch configuration and schedule pin the selected template revision | `test_session_templates.py`: old revisions, input mismatch, independent launch IDs and schedule pinning | Local API/UI verification | Cloud deployment remains pending |
| Separate profiles, sessions, batches, assignments, attempts | Yes; persisted domain and generated API contracts | Batch independence, attempts and idempotency tests | Local batches and real Chromium launches | Cloud concurrency validation |
| Fresh and persistent profiles; immutable encrypted snapshots | Yes; AES-GCM, checksums, generations and compatibility metadata | Profile encryption/tamper tests; remote transfer roundtrip | Two real persistent profiles, each saved then restored for a second successful workflow | Cross-worker cloud restore; licensed adapter exports |
| Exclusive profile owner and increasing epoch | Yes; leases, quarantine and explicit runtime-stop confirmation | Lost-lease, stale-worker, manual-input and artifact fencing regressions | Local ownership and cleanup exercised | Actual partition and node-loss fencing rehearsal |
| Resumable batch creation and partial failure | Yes; idempotent requests and per-item outcomes | Per-item allocation failure/retry tests; dropped-response replay and partial live batch UI smoke | Playwright item completes while unavailable vendor item is blocked | Large live batch throughput |
| Admission and independent concurrent sessions | Yes; global/project/target/pool/proxy/license limits | Shared admission, quarantine reservation and independent-profile checks | Local independent sessions | 100/250/500 actual simultaneous browsers |
| Pause, resume, cancel, cleanup and reconciliation | Yes; pause preserves active work; ambiguous actions are not replayed | State transitions, cancelling overlap, stale callbacks and cleanup tests | Local runner workflow completion and clean shutdown | Forced node/partition failure and ambiguous external transaction exercise |
| Durable outbox, ClickHouse delivery and live updates | Yes; PostgreSQL authoritative; recoverable session/legacy outboxes | Existing event/recovery integration checks and platform tests | Local database, Redis, ClickHouse and worker execution | Cloud queue/analytics outage and failover rehearsal |
| Idempotent lifecycle, scheduling, ingestion and batch APIs | Yes; body consistency checks and immutable observation identity | Replay/conflict tests and browser dropped-response test | Local API replay | Multi-replica deployment verification |
| Server pagination/filtering and OpenAPI frontend contracts | Yes; stable allowlisted server ordering, literal text search, resource filters and bounded pagination | List-query regressions, typecheck, generated schema and UI pagination | Local UI incl. 500-row fixture | Fixture scale is not live browser capacity |

## Browsers, control, and networking

| Requirement | Implemented | Automatically tested | Live verified | Remaining gate or boundary |
|---|---|---|---|---|
| Playwright Chromium adapter | Yes; common lifecycle and actual metadata | Actual local and remote Chromium tests | Native Mac Chromium and non-root sandboxed Linux amd64 container | Cloud image/runtime execution |
| Genuine manual Chrome | Yes; native Chrome launch, cloud headed image, authenticated control and explicit handback | Native launch contract, real headed container launch/cleanup and control-state tests | Native headed Chrome launch/cleanup and an isolated viewer UI fixture | A person-operated research run remains pending; CDP attachment is recorded as a confound |
| CloakBrowser | Yes; documented fresh/persistent APIs and entitlement checks | Mocked SDK lifecycle and license/capacity checks | Pending | Installed eligible runtime/license, actual version and concurrency validation |
| Kameleo Chroma | Yes; SDK/engine compatibility, profile stop/export/delete, isolated engine sidecar | Mocked profile/proxy/cleanup and remote contract tests | Pending | amd64 licensed engine, viewer and persistent export/import validation |
| Adapter readiness and version reporting | Yes; configured availability is distinct from verified runtime | Missing package/version/license/capacity checks | Stock Chromium actual metadata | Vendor readiness remains unavailable until verified |
| None/automated/manual preparation | Yes; disabled by default, bounded seeded actions and recorded activity | Determinism, allowed interactions and labeling tests | Workflow execution local; preparation primarily automated contracts | Real human control and vendor preparation experiments |
| Single controller, view-only access, idle/lifetime limits | Yes; short-lived access, 15-minute default idle, explicit handoff | Control ownership, expired input, delayed frame and encrypted command tests | Local manual-viewer UI fixture | End-to-end person-operated session with cloud authentication |
| Sensitive manual inputs and artifacts | Yes; encrypted queue bodies, tombstones, encrypted artifacts, project access and retention | Tamper/context binding, redaction, access, retention and failed-commit cleanup tests | Local captured artifact download | Object-store retention and recovery-key operations in cloud |
| Finite request/traffic budgets and measured usage | Yes; browser HTTP interception counts and broker byte limits are distinct | `test_execution_budgets.py`: limits, observed usage and cleanup | Local owned fixtures | Provider billing reconciliation and cloud traffic probes |
| Known cost components and fleet estimates | Yes; explicit currencies, per-GB transport estimates and observed-node rates | `test_costs.py`: missing components/currency, retries and fleet deduplication | Local configured estimates | Provider invoices and complete deployed-node inventory |
| Product-specific Oxylabs policies and inventory | Yes; product/geography/continuity/capacity/entitlement filters | Selection rationale, continuity, unknown cost, explicit fallback tests | Local policy/inventory UI | Real Oxylabs credentials, inventory and quota evidence |
| Proxy transport health and bounded safe replacement | Yes; auth/quota pause, transport classification and environment epochs | Classification and ambiguous-action no-replay tests | Local broker fixture | Actual provider failures/expiry/rotation and Kameleo stop/update/restart |
| Credential-isolating egress broker | Yes; separate session broker and browser pods; scoped capabilities | Broker authentication/expiry, remote cleanup and credential separation | Real local remote-browser/broker roundtrip | Deployed network-policy probes for all traffic classes |
| Navigation/resource/DNS/redirect restrictions | Yes; primary and additional document origins require deployment approval or ownership proof. Resource origins remain separate | Browser/network guard tests | Owned local fixtures | Upstream proxy DNS is an explicit trust boundary |
| Browser direct egress denied in cloud | Yes; per-session network policies and isolated service accounts | Rendered manifests and capability tests | Pending | CNI enforcement, redirects, WebSockets and auxiliary-client probes on Hetzner |

## Scheduling and research

| Requirement | Implemented | Automatically tested | Live verified | Remaining gate or boundary |
|---|---|---|---|---|
| Dedicated leader-controlled scheduler | Yes; advisory lock and transactional occurrence/run/outbox | Occurrence replay, overlap and scheduling tests | Separate local scheduler process | Kill leader and observe another cloud replica take over |
| One-time, interval, calendar and timezone previews | Yes; pinned versions, history, pause/resume/run-now/expiry | Jerusalem DST, misfire and overlap calculations; schedule UI smoke | Local schedule editing/history | Long-running clock/failover rehearsal; explicit run-now is a user-triggered launch subject to admission |
| AdScore signature correlation and internal official SDK | Yes; signed session/attempt context, trusted IP/UA, V4/V5 configuration | Node verifier tests; failed verification, negative verdict, duplicate/unknown-code tests | No live zone | Compatible AdScore zone, public authorized integration and complete smoke test |
| Controlled acceptance gate | Yes; default verified Clean required | Trusted-metadata callback and gate tests | Local fixture with mocked verifier | Live AdScore end-to-end action/observation correlation |
| Separate workflow result, verification, raw verdict, acceptance, ground truth and provenance | Yes; code 0 labeled Clean | Negative accepted-state regression, unknown code and browser-only tests | Local browser-only and explicit simulated reports | Real verified instrumented observations |
| Immutable versioned experiments and matched seeded groups | Yes; revisions, clone, validate, execute, run-wide order/concurrency and matched-condition reports | Draft/edit/execution idempotency; complete browser experiment smoke | Local experiment and report UI | Live confirmation evidence for specified conditions |
| Wilson, cluster-aware analysis, pairwise correction | Yes; cluster bootstrap plus conservative effective-cluster bounds; Fisher/Holm | Reference values, correlated profiles, environment matching, ties, costs | Rendered fixture reports | Independent statistical review recommended before consequential operational selection |
| First attempt/recovery, failures in denominators and verification coverage | Yes | Research and release-review regression tests | Local reports from stored observations | Live instrumented datasets |
| Qualification and recommendation eligibility | Yes; completed live confirmation, lower-bound threshold, reliability first; unknown costs preserved | Demo/pilot/incomplete exclusion, ties, no qualified candidate | No live recommendation produced | Eligible live confirmation run; detector evidence never changes proxy scoring/preparation |
| HTML/PDF/CSV/JSON immutable snapshots and traceability | Yes; immutable data and analysis hashes | Dataset/export consistency and tamper tests; API format roundtrip | Two-page batch and four-page experiment PDFs rendered and visually inspected | Live report after real confirmation run |

## UX, infrastructure, and release operations

| Requirement | Implemented | Automatically tested | Live verified | Remaining gate or boundary |
|---|---|---|---|---|
| Consistent dark UX and primary product flows | Yes; Overview, Sessions, Sites, Workflows, Runs, Schedules, Research, Network, Fleet | Production build, lint/typecheck and five browser smoke scripts | Local desktop and narrow-screen screenshots reviewed | Broader assisted-technology/accessibility audit |
| Visual workflow builder, validation, JSON and preview | Yes; presence/text/absence assertions, named checkpoints, JSON and versioned preview | Workflow/network tests, real Chromium guard/checkpoint tests and actual preview smoke | Local actual Chromium preview | Site-specific adapted workflows |
| Large list rendering and one shared update strategy | Yes; pagination, virtualization and focused viewer fetching | 500-row fixture and narrow-screen tests | Local UI rendering only | Control-plane responsiveness under 500 real sessions |
| Research charts and failure/cost visibility | Yes; uncertainty, distributions, timelines, matrices and unknown-cost states | Research UI smoke and data checks | Local stored-data charts | Historical CPU/memory charts show unavailable until metrics exist |
| HA Hetzner foundation and elastic pools | Yes; Terraform, K3s, 3 control/2 app/3 data, 2 warm standard workers | Terraform validate, Helm lint, six operator charts and six CRD schema checks | Docker builds only | Fresh Hetzner deployment, quotas, DNS/TLS and account configuration |
| Admission-driven KEDA jobs and Cluster Autoscaler | Yes; 500-session/80-elastic-node ceilings, Terraform does not own elastic instances | Rendered contracts, capacity estimates and ownership checks | Pending | Real scale-up, drain/scale-down and provider quota behavior |
| PostgreSQL HA, Sentinel, ClickHouse/Keeper | Yes; synchronous metadata policy and replicated manifests | Rendered schemas and configuration checks | Single local development services | Node loss/database failover within two minutes and no acknowledged metadata loss |
| Encrypted storage, state locks and cross-location backups | Yes; SSE-C PostgreSQL backups, encrypted artifact/profile snapshots, backup mirror and restore tools | Encryption/corruption/lock-contract/restore safety tests | Local encryption roundtrips | Real object storage conditional operations, restore into new environment and key custody |
| RPO ≤15 minutes, RTO ≤4 hours | Runbooks and tools provided | Restore guard tests | Pending | Timed cross-location disaster-recovery rehearsal; no regional HA claim |
| Structured logs, telemetry, Prometheus and worker health | Yes; DB-derived fleet metrics, tracing and deployment collectors | Source/rendered contracts and local API behavior | Local worker health; cloud collectors not running | Actual dashboards/alerts under deployed failures/load |
| 100 → 250 → 500 browser validation | Executable staged load harness; estimates labeled hypotheses | Capacity math/constraints tested | Pending | Hetzner resources, owned fixture capacity, proxy capacity and actual licensed-browser limits |
| Release reproducibility and source evidence | Yes; migration evidence, report fixtures and image source hashes | Full Python suite, verifier tests, web build/lint and infrastructure checks | Local source hashes match final API/web images | Full operational acceptance remains open until rows above are live verified |

## Evidence index

- [Implementation report](implementation-report.md): exact commands/results and remaining deployment prerequisites.
- [Experiment run PDF](experiment-run.pdf), [run snapshot](experiment-run.json), [run dataset](experiment-run.csv): matched workflow strata and complete-run traceability.
- [Rendered batch PDF](research-report.pdf), [page 1](research-report-1.png), [page 2](research-report-2.png), [dataset](research-report.csv), [snapshot](research-report.json), [HTML](research-report.html): explicitly simulated immutable report fixture.
- `tests/test_platform.py`, `test_release_review.py`, `test_command_secrets.py`: persistence, permissions, scheduling and lifecycle evidence.
- `tests/test_adapters_proxy.py`, `test_remote_browser.py`, `test_browser.py`: browser/proxy/profile and actual Chromium evidence.
- `tests/test_research.py`: statistical and recommendation eligibility evidence.
- `tests/test_infrastructure.py`: 32 cloud contract, encryption and isolation checks.
- `apps/web/scripts/{smoke,platform_smoke,experiments_smoke,release_smoke,templates_smoke}.py`: reproducible browser checks. Local screenshots are under ignored `.abrl/ui-evidence/`.

The platform can be exercised locally now. The delivery contract's full completion condition is still **pending external live validation**, and the operational and comprehensive accessibility boundaries above remain explicit.
