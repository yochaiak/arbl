# ABRL · Browser Workflow Lab

Run authorized browser workflows and measure how often they complete, how long they take, and where they stop. ABRL provides an editable site registry, versioned workflows, and reproducible local trials using standard Playwright Chromium. AdScore verification is a separate, manually initiated diagnostic for an authorized integration.

## Start locally

Install Docker with Compose, `uv`, Node.js 22.15+, pnpm, and Make. Apple Silicon macOS runs Chromium natively. On Linux, install Playwright's browser dependencies with `uv run playwright install --with-deps chromium` after `uv sync --locked`.

```sh
make setup
make dev
```

Open **http://localhost:8430** and enter the value of `ABRL_OPERATOR_TOKEN` from the local `.env`. `make configure` generates local credentials once with owner-only file permissions. The API listens on **http://127.0.0.1:8400**. Both bind to loopback. For separate processes use `make infra migrate`, then `make api`, `make worker`, and `make web` in separate terminals.

1. Select **Load starter suite** to add the controlled test site and example workflows.
2. Select **New run**, choose the site and **Sample form**, and set the trial count to one. Live mode uses actual Chromium. Demo mode uses repeatable simulated observations and makes no browser requests.
3. Inspect the trial, download a report in JSON, CSV, HTML, or PDF, or archive the finished report summary to the local object store.
4. When you have a representative workflow, run a pilot and then a separately configured confirmation run.

`make down` stops the infrastructure and preserves its data volumes. Stop the development processes from their terminals.

## Use another site

Register a site you own or are explicitly authorized to test. For an external origin, publish the one-time ownership marker shown in the registry at the exact HTTPS path and select **Verify**. An edit requires re-verification. The origin must resolve to public IPv4 and use HTTPS on port 443. Secondary origins are for read-only resources; they cannot be navigation destinations.

Author the steps in the dashboard or import a workflow JSON document. Start with a relative navigation path and end with an explicit success assertion. A trial uses a fresh browser context; steps within that trial share its cookies. Use `ABRL_SECRET_<NAME>` in the worker environment for sensitive fill values and reference the name in the workflow. Use test accounts and fixtures for workflows that submit data.

Remote pages run through scoped browser request checks and a connection guard that resolves and pins public destination addresses. The worker closes the run on recognized access challenges or denial. It does not follow popups or WebSockets. Browser and proxy controls do not replace host-level sandboxing for untrusted targets.

## Read the evidence

- Completion includes real workflow failures and operational failures in the denominator. Canceled and pending work is reported separately.
- The report shows first-attempt reliability, completion after eligible infrastructure retries, latency, and a 95% Wilson interval. A completed confirmation run qualifies only when the lower confidence bound meets its configured target. Repeat-visit intervals are descriptive.
- Pilot, incomplete, and simulated runs do not qualify a workflow. Reports contain the source, run configuration hash, and traceable trial identifiers.
- Automated runs do not receive AdScore verdicts. A separate manual diagnostic records verification state, vendor code, and verdict without attaching them to automated runs. A verified Clean verdict is a provider classification.

The [verifier setup guide](apps/verifier/README.md) describes the optional live diagnostic. It requires a compatible zone, credentials, a dedicated authorized HTTPS origin, and trusted IP/User-Agent metadata. Live vendor verification is not part of CI.

## Development and operations

```sh
make test
make lint typecheck
make build
make integration-test    # With the loopback API, worker, and Compose stack running
```

FastAPI exposes OpenAPI at `/openapi.json`; run `pnpm --dir apps/web generate:api` against the local API after changing its contract. See [Architecture and operations](docs/architecture.md) for queue recovery, diagnostics, monitoring, and current limits.
