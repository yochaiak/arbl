# Architecture and operations

The Next.js dashboard talks to the FastAPI operator API using a local bearer token. Alembic manages PostgreSQL schema changes. The installation uses a single trusted local operator rather than a shared tenancy model.

- **Configuration:** sites and workflows have stable IDs and immutable revisions. A run snapshots both revisions. Changing a target invalidates prior external ownership approval, and live workers check the snapshot against the approved current version.
- **Execution:** PostgreSQL holds the durable trial queue and leases. The API scheduler dispatches work through Redis to a one-process, one-thread Dramatiq worker. Browser actions are time bounded. An expired lease records unknown-action-state failure; it does not replay a possibly completed action. Redis publication failure can delay dispatch but does not lose trial records.
- **Observations:** browser execution, simulation, and manual provider diagnostics have distinct provenance. The worker retries at most once after a known pre-action failure. A recognized access denial stops remaining work in that run. Per-target trial rates and global pending run counts are bounded.
- **Events and reports:** state changes and events commit together. The API streams events with resume cursors and forwards the durable outbox to ClickHouse. ClickHouse is a read model; use `FINAL` or group by `sequence` when querying replayed deliveries. A report archive uses a content-specific key in the local SeaweedFS S3 bucket and omits configured workflow values and extracted page data. PostgreSQL remains authoritative.
- **Observability:** operator-authenticated Prometheus metrics are available at `/api/v1/metrics`. API and worker application logs are structured. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to an approved collector to enable API traces. Leave unset for entirely local telemetry.

## Data and deployment limits

This installation is intended for loopback development and authorized test environments. Use named secrets, test inputs, and dedicated authorization for remote sites. Exported workflow JSON contains literal inputs configured in the builder; use secret references for sensitive inputs. The app does not capture browser screenshots, page dumps, or raw signature material.

Object storage archives remain until deleted or until a lifecycle rule on `abrl-reports` expires them. Back up the PostgreSQL volume before changing versions. Logs and object storage are local; configure a retention policy that matches your test data.

Provider readiness depends on a successful manual signature verification with real trusted request metadata. The repository's automated verification uses local fixtures, not credentials or a third-party target. The single-operator installation has one standard browser adapter and reports reliability qualification per run; it does not rank browsers or infer AdScore behavior from a site's visible response.
