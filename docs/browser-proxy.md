# Browser execution, profile storage and proxy contracts

The local execution path launches a real browser for each assignment. A reusable
profile is an explicit directory owned by a database lease and an additional
nonblocking filesystem lock. Independent sessions must use independent profiles.
`execute_workflow` retains its original keyword interface and adds `adapter`,
`profile_dir`, `preparation`, `proxy`, `emit`, `control`, `remote`,
`expected_compatibility`, `artifact_policy`, `user_agent`, `referrer`, `device_preset`,
and `batch_preset`.

## Session User-Agent and initial referrer

Set these optional values in the batch configuration. Templates and schedules
preserve them. A User-Agent preset or custom value requires every
session in the batch to use stock Playwright Chromium. It applies for the
life of the browser context, and session metadata records it separately from
the native User-Agent reported by CDP.

All adapters support an initial referrer. Chromium uses it on the first
app-issued navigation of each browser launch, including the page opened
by preparation. Subsequent navigation follows Chromium's referrer behavior.

Session Details lists recorded AdScore measurement requests. For responses to
`POST /p`, it shows the browser-reported `Referer` origin with paths, query strings,
and fragments removed. A recorded absent header and an unavailable capture have
separate labels. Session Details also shows the browser-reported request policy
and any recognized `Referrer-Policy` header on the initiating top-level page.
When an `assert_not_visible` step times out, the run records the supported
warning codes still visible within the assertion's match.

AdScore request initiator diagnostics are available for registered measurement
endpoints. They show the initiator type, reported script or document origins,
and `Referer` presence in Chromium's initial, forwarding, and network headers.
Browser request IDs correlate the observations for each request. Redirect chains
can leave stages uncorrelated. The trace stores origins, fixed labels, and
header presence flags.

Every adapter supports the phone and tablet viewport presets. Stock Playwright
also applies mobile and touch emulation. Session details report the effective
viewport, device mode, and browser engine.

## Responsive QA batch presets

In the batch form, open **Advanced → Batch presets**. API clients can
set `batch_presets` when posting to `/api/v1/batches` or `/api/v1/batches/preflight`.
Presets apply to local sessions, including all tabs. Playwright sets the viewport,
device pixel ratio, locale, and timezone at context launch. Manual Chrome, Kameleo,
and CloakBrowser apply the viewport to each page and use adapter-managed device
class, pixel ratio, locale, and timezone. Each browser supplies its native
User-Agent, operating-system platform, Client Hints, and input capabilities.
Compact viewports exercise responsive layouts. A viewport-only session fails at
launch if the requested viewport exceeds the browser’s screen or the runtime
cannot resize the viewport while preserving its screen metrics. Set `user_agent`
and the legacy `device_preset` to their defaults when using `batch_presets`,
including in session groups.

```json
{
  "target_id": "qa-site",
  "workflow_id": "responsive-layout-check",
  "idempotency_key": "responsive-layout-v1",
  "count": 4,
  "seed": 136,
  "batch_presets": {
    "catalog_version": 1,
    "mode": "round_robin",
    "presets": ["desktop_1440", "compact_390"],
    "locales": ["en-US", "en-GB"]
  }
}
```

| Catalog v1 preset | Viewport class | CSS pixels | Playwright device pixel ratio |
| --- | --- | --- | --- |
| `desktop_1280` | Desktop | 1280 × 720 | 1 |
| `desktop_1440` | Desktop | 1440 × 900 | 2 |
| `compact_390` | Phone | 390 × 844 | 3 |
| `tablet_768` | Tablet | 768 × 1024 | 2 |

The locale list applies to Playwright and defaults to `en-US`. Each locale has a
fixed timezone.

| Locale | Timezone |
| --- | --- |
| `en-US` | `America/New_York` |
| `en-GB` | `Europe/London` |
| `he-IL` | `Asia/Jerusalem` |

- `fixed` requires one preset and one locale for the batch. Viewport-only
  adapters use the single viewport.
- `round_robin` cycles through the choices in request order and wraps at the end.
- `seeded` shuffles each cycle, visiting every choice before it repeats. The same
  seed, catalog version, and ordered lists reproduce the assignments.

Each adapter has its own sequence, continuing across session groups. Playwright
cycles through the requested locales for each viewport. The other adapters
cycle through the requested viewports.

An omitted `seed` is generated and stored in the batch configuration. The initial
allocation records the selected settings by session ordinal. Reattempting allocation
or retrying a browser launch uses those settings again. Use Session Details or
the diagnostic bundle to inspect the catalog version, assigned viewport, preset
application, distribution mode, and seed. Viewport-only assignments report the
device class as adapter-managed and leave the preset DPR, locale, and timezone
empty. Managed browser diagnostics check the browser timezone against an
assigned timezone when present. Proxy preflight reports the observed exit
timezone separately when available.

## Adapters and readiness

`adapter_capabilities()` performs local checks only. `configured` means the package
and local prerequisites exist, not that a licensed browser has passed a live trial.
The adapters API publishes feature support and live worker capacity separately.
A null worker capacity means the pool has yet to report adapter-specific capacity.
Admission uses reporting workers' adapter support and the adapter's session limit.
The launch records CDP's actual browser product, revision and User-Agent, wrapper
version, host OS and architecture. No custom fingerprint flags are accepted.

- `playwright`: stock Chromium, fresh context or `launch_persistent_context`.
- `manual_chrome`: installed Google Chrome with a headed display, launched as a
  native subprocess with a dedicated profile and without Playwright automation
  launch flags. CDP attaches afterward for scope enforcement, observations and
  operator input. An operator performs the actions, and automation evaluates
  assertions after handoff. An explicit success assertion and operator channel
  are required. Preflight identifies workflows with operator-reserved steps.
  Session Events records those steps by action and position. On Linux the host
  needs a headed display such as Xvfb. Chrome
  receives a credential-free local proxy URL.
- `cloakbrowser`: optional current CloakBrowser wrapper and preinstalled binary.
  Set `CLOAKBROWSER_LICENSE_KEY`. License validity and available seats are checked
  before launch; vendor enforcement remains authoritative. Launch uses the SDK's
  `humanize=True` and `geoip=True` options. Free-tier concurrency is one; exact binary
  pinning requires a Pro entitlement. `ABRL_CLOAK_TIER=pro` allows a pin to reach the
  live entitlement check; it does not independently grant a license.
- `kameleo`: optional `kameleo.local-api-client`, with
  `ABRL_KAMELEO_ENDPOINT=http://127.0.0.1:5050`. The worker and engine must share the
  network namespace and exported profile directory. Linux requires amd64. Actual
  engine version comes from the SDK user-info response, and major/minor must match
  the SDK. The Engine profile uses a loopback broker. For local runs, the broker
  permits Kameleo's HTTPS startup checks before enabling traffic to the
  registered target. Chroma's startup callback goes directly to the Engine's
  loopback API port. A single Chroma context belongs to each profile.
  Persistent profiles require Kameleo's import/export entitlement. Stop precedes export,
  and successful export precedes deletion of the loaded engine profile. Import
  retains vendor profile identity. Uncertain saves retain that engine profile for
  reconciliation and quarantine.

`runtime_stopped` and `profile_saved` become true only after confirmed shutdown
(and, for Kameleo persistence, successful export). A failed shutdown is not a saved
profile. A crashed/partitioned runtime requires controller quarantine; lease
expiration alone cannot grant the next writer. A local lock does not provide
cross-host fencing.

Restoring a Chromium-family profile requires a nonempty `Local State` file.
Kameleo requires its `profile.kameleo` archive before starting the Engine profile.
An existing profile supplies its own persistence mode in both flat and grouped
batches. The runner loads snapshots only for persistent assignments.
Startup failure and startup timeout produce fixed error codes. If a running
browser context disappears during a workflow, the runner records `browser_crash`
and skips publication of that attempt's profile state.

## Operator control

New automated sessions start their workflow directly. Manual Chrome waits for
operator handoff, including when it is part of a mixed batch. Legacy pinned
preparation settings remain available to their saved configurations.

Manual preparation requires `control() -> dict | None`. Supported commands are
`continue`, `stop`, `click` (`x`,`y`), `type` (`text`), `key` (`key` from a bounded
navigation-key set), `scroll` (`delta_y`), and `navigate` (`url`). No automation
workflow step runs while manual control is held. Default idle expiry is 900
seconds and lifetime 3600 seconds. The explicit workflow timeout is additional
to the bounded preparation budget.

`emit(event)` receives lifecycle, workflow, preparation, manual-handoff and
`action_started` events. The action event is awaited before a potentially
state-changing browser call, so the controller can durably record the ambiguous
action boundary. A failed assignment after such an action sets
`needs_reconciliation`; it cannot be replayed automatically.

Manual frames are JPEG events at most once per second. **The controller must keep
`manual_frame` bytes only in its short-lived authenticated viewer channel, not
audit records, SSE logs, or report datasets.** Input text is never included in
timeline events. The controller owns operator exclusivity, authorization, token
expiry and revocation. Manual command bodies are AES-256-GCM encrypted in the
queue, bound to session, operator, ownership epoch and idempotency key. Configure
`ABRL_COMMAND_ENCRYPTION_KEY` as a 32-byte URL-safe base64 key, or use the existing
profile key as the fallback. Purpose-separated HMAC keys protect idempotency
without retaining typed input. API and worker must retain the same key while
pending commands or historical idempotency records exist; changing it makes
pending ciphertext unreadable and old retry hashes incomparable. After consumption,
release, stop or a new control claim following expiration, queued bodies are
replaced with command-only tombstones. Historical plaintext commands remain readable
and are wiped when consumed. No typed text enters audit events. A manual preparation
followed by an automated workflow is labelled mixed; only the genuine manual-control
adapter is labelled human.

## Proxy selection and egress

`select_proxy(policy, candidates, existing)` is a pure decision helper returning
status, candidate, rationale, rejected candidates and measured evidence. It does
not reserve inventory; the controller reserves capacity transactionally. Policies
filter provider, geography, explicitly permitted fallback regions, product, continuity,
exclusive inventory, confirmed duration, expiry, adapter compatibility and
capacity. Eligible candidates rank by measured transport success, latency,
utilization and known cost. Unknown data stays null. Detector verdicts never enter
the transport score. An eligible existing binding is retained for continuity.

The Oxylabs binding supports `residential`, `isp`, `dedicated_isp`, `datacenter` and
`dedicated_datacenter`. It requires the documented gateway `host`/`port`,
`username_ref` and `password_ref` resolved from `ABRL_SECRET_<REF>`. Inline
credentials and URLs are rejected. Residential bindings additionally support
`country`, `session_key`, `strict_affinity`, and `ttl_minutes` (1–1440).
Public API inventory automatically derives residential capabilities from the
supported `pr.oxylabs.io:7777` gateway. Claiming a sticky/strict lease creates a
fresh session key and applies the policy duration; strict uses the documented
`-sessid_oneip-<id>` parameter instead of `-sessid-<id>`. Strict affinity returns a
transport failure when the exit disappears; it promises neither uptime nor
exclusive use. Ordinary sticky exits can change before their configured lifetime.
A `none` continuity policy does not silently add a sticky session.

Decodo supports US Residential at `gate.decodo.com:7000` over TLS. In **Network**,
create a Decodo inventory entry and a Decodo policy, then select that policy at
batch launch. The gateway authenticates with `user-<proxy-user>-country-us` and
adds `-session-<id>-sessionduration-<minutes>` for a sticky lease. The named refs
`DECODO_USERNAME` and `DECODO_PASSWORD` resolve from `ABRL_SECRET_DECODO_USERNAME`
and `ABRL_SECRET_DECODO_PASSWORD`. Decodo policies support rotating and ordinary
sticky leases. They stay on Decodo US inventory when selecting and recovering
leases. Existing inventory and policies with no provider field use Oxylabs.
[Decodo gateway documentation](https://decodo.com/faq/getting-started/what-are-backconnect-proxies)

Fixed self-service inventory uses the product's assigned port (8001+) at
`isp.oxylabs.io`, `disp.oxylabs.io`, `dc.oxylabs.io`, or `ddc.oxylabs.io`.
Missing host/port default to the selected product's gateway and port 8001. Register
the actual assigned country and port from the entitled inventory. Rotating
port 8000, residential sticky entry ports and custom/enterprise endpoints are
explicitly unsupported rather than labelled fixed. Exclusivity requires a
dedicated ISP/datacenter product and an explicitly exclusive inventory entry.
Policy health thresholds (`max_latency_ms`, `min_transport_success_rate`) reject
unmeasured candidates when configured. No connection success or entitlement is
invented from a product label.

The per-session broker sends provider authentication only on a TLS-verified
upstream connection. Browser profiles contain only the loopback broker URL.
Target HTTPS remains end-to-end encrypted. Upstream failures are classified
separately from website responses: authentication/quota pause the pool, transport
failure permits controller replacement only before an ambiguous action, and site
denial pauses the assignment. Replacement occurs between browser launches and
creates a new environment epoch; the broker never rotates exits per request.
A failed initial HTTPS navigation can recover only when measured broker evidence proves
that every connection attempt failed before any origin tunnel existed, zero
workflow steps completed, no non-idempotent action started, and shutdown was
confirmed. The durable action-start marker remains intact. Fixed-port recovery
excludes the failed binding (including duplicate registrations); residential
recovery uses a new session key. Authentication, quota, site denials, successful
origin tunnels and ambiguous actions cannot use this exception.

An upstream CONNECT 403 is recorded as a destination denial with the upstream
status. It prevents connection recovery for that attempt even when another
tunnel times out. Broker and session events omit requested and upstream
authorities, including literal addresses.
Register a documented dependency hostname as an exact resource origin to keep
that hostname in CONNECT. Automatically admitted assets use a locally validated
IPv4 address upstream. If the dependency itself requests an IPv4 literal and
the provider denies it, ask the provider about allowing that exact destination on
the existing product. A dependency vendor can also supply a supported
hostname-based integration for that request.

Direct mode validates all DNS answers and dials the validated literal public IPv4
address. With an upstream proxy, the hostname is resolved again by the provider; configurations
must explicitly set `provider_resolved_ack=true`. A local validation cannot prove
which address an upstream resolver used. This boundary is included in metadata.
Navigation and resource origins remain distinct at the browser interception layer.
The foreground page can load public HTTPS scripts, stylesheets, images, fonts,
media, manifests, and text tracks from CDN origins it references. The browser
checks each asset request and redirect. The broker accepts discovered origins
for the session, validates the destination address, and meters the traffic.
Cross-origin read-only API requests use the site's configured resource origins.
For registered AdScore resource origins, including its `https://162.252.214.4`
fallback, the browser also permits the engine's `POST /p` and its CORS preflight.
A proxy policy or binding pinned to one target origin keeps that exact scope.
WebSockets and web service workers are blocked. Attached native Chrome and
Kameleo contexts install download denial and service-worker controls before
target forwarding. The runner blocks unsupported enforcement and suppresses
profile publication after a containment failure.
Auxiliary arbitrary request clients are not exposed to workflow definitions.

The local proxy and Chromium settings enforce application-level network scope.
The adapter also uses Chromium's own sandbox where supported. Arbitrary
`ABRL_BROWSER_WS_ENDPOINT` and `ABRL_EGRESS_PROXY_URL` settings fail readiness.

The browser launch arguments request TCP transport by disabling QUIC. The IPv4 broker rejects IPv6 literals, IPv4-mapped IPv6 literals, and bracketed IPvFuture authorities before DNS. The loopback regression uses a reachable IPv6 HTTP server as a positive control, then checks that a fresh and a persistent Chromium session cannot reach it through the broker. These checks cover HTTP traffic through the browser proxy. Process-wide UDP and IPv6 isolation require a network boundary outside the browser.

The supported upstream transport is HTTPS CONNECT. Local DNS performs gateway discovery and destination safety validation. The provider resolves registered destination hostnames independently. Resolver geography and a remote-DNS-only mode are unavailable on this path.

## Browser and broker transport tests

The normal session runner launches browsers on the host. The authenticated
browser and broker services in `abrl.remote_browser` are exercised by local
transport tests. These tests cover scoped control, lease expiry, clean shutdown,
and encrypted transfers without relying on the database or platform credentials
in the browser process.

The local CloakBrowser adapter routes license checks directly through the session
broker. When a proxy is assigned, the SDK's supported startup IP lookups use that
proxy. Startup lookups count toward the transport budget and are recorded
separately from workflow traffic.

## Diagnostic artifacts

Live batches default to `artifact_policy=all` and bounded JavaScript capture.
Screenshot and console capture can also be limited to failures or turned off.
The runner retains a viewport screenshot and up to 100 console entries. Secret-reference input
selectors, password fields and `[data-abrl-secret]` elements are masked. Known
workflow secrets and correlation tokens are redacted from console text. Artifacts
are emitted through the controller's authenticated persistence callback, never
placed in the ordinary session timeline. Capture failures remain metadata and do
not change a successfully completed workflow. The controller controls encrypted
storage, artifact permissions and retention.

## Encrypted snapshots

`ProfileStore(root, key)` accepts an externally retained 32-byte AES key (or URL-safe
base64 encoding). `save(profile_id, directory, clean_shutdown=True,
compatibility=..., generation=...)` publishes one immutable AES-256-GCM envelope
containing authenticated metadata and encrypted profile contents. SHA-256 checks
cover the plaintext archive and encrypted envelope. Publication uses an exclusive
hard link after fsync. No plaintext archive is stored beside the encrypted file.

`restore` requires the exact compatibility dictionary and a new destination.
Generation identity, GCM authentication and checksum are checked before extraction;
symlinks, traversal, excessive file counts and oversized profiles are rejected.
The local packer buffers up to its 2 GiB profile limit in memory.
Object-storage upload, versioned retention, backup and distributed ownership are
controller responsibilities. Cross-OS portability is not assumed.

## Verification evidence and vendor references

Run the installed runtimes against the owned local fixture with licensed engines
and available seats.

```sh
ABRL_ADAPTER_ALIGNMENT_SMOKE=1 uv run --locked python -m pytest -q -s tests/test_adapter_alignment_smoke.py
```

Each case emits a sanitized result with adapter, SDK, browser version and OS.
The matrix covers viewport checks, workflow completion, cancellation, shutdown,
artifact capture, and profile restore. The native Chrome case exercises the
operator handoff callback with a fixture operator. A missing prerequisite leaves
that capability blocked from verification.

Each session has a finite HTTP request budget (default 1,000, maximum 100,000)
and traffic budget (default 100 MiB, maximum 2 GiB), pinned in the batch or
schedule snapshot. Preparation and workflow share both limits; request counts
also identify the phase. Context interception admits each initial HTTP request
once; CDP interception counts redirected hops because Playwright does not route
them again. HTTP counts are independent of CONNECT tunnel counts. Resource,
document, and form requests all consume the budget. WebSockets remain denied by
the supported workflow policy; opaque TLS/WSS data still passes through the same
byte limiter if a future explicitly supported workflow enables it.

The broker reserves forwarded payload bytes atomically across both directions
and all active connections. A chunk that would exceed the cap is not forwarded,
and the broker closes active transfer writers and refuses subsequent target
connections. It may stop up to one 64-KiB transfer chunk before the exact limit.
`transport_usage` reports admitted upstream/downstream bytes, the limit and
exhaustion flag. These are transport measurements, not provider invoices: TLS
record bytes are included, while provider CONNECT negotiation, broker control,
protocol overhead outside the payload, and unforwarded buffered bytes are not.
Vendor licensing traffic is reported separately as `vendor_control_bytes`; it
consumes the finite session byte budget but never the proxy-cost quantity.
Remote activation checks the deployed broker's cap against the assignment.

Proxy policy `connection_load` (default 1, maximum 100) reserves that number of
inventory connection slots per session and becomes its broker `max_connections`.
Excess outgoing target connections receive an immediate 503, avoiding indefinite
waits behind idle HTTP/2 tunnels. Broker-control and separate vendor-licensing
sockets do not consume experimental provider slots. Connection summaries expose
the cap, peak, active count and rejected attempts. The historical inventory
`active_leases` field represents reserved connection slots; old policies retain
their one-slot weight. Slot rejection and exhausted budgets do not trigger proxy
replacement or clear action ambiguity. Failed attempts retain their measured
traffic and immutable configured rate/currency for separately labeled cost
estimates; recovery never adds a new research sample.
Provider-proxy trials reject the direct local HTTP fixture; use an authorized
HTTPS controlled target so proxy evidence cannot silently describe direct
fixture traffic.

Run `uv run python -m pytest tests/test_browser.py tests/test_adapters_proxy.py tests/test_remote_browser.py`.
Tests include real local Chromium launches, simultaneous independent profiles,
durable localStorage across clean relaunches, manual handoff sequencing, preparation
recording, denial/redirect/cancellation, profile encryption and tamper rejection,
proxy policy reasons, credential redaction, and a mocked TLS upstream handshake.
No external proxy traffic or licensed browser run is represented as live verified.
The remote tests also cover bearer/epoch authentication, a paused broker, expiry
that cannot be renewed, separation of control/proxy capabilities, fresh Chromium
execution through the remote broker, encrypted persistent-profile roundtrips,
expiry during vendor startup, absolute runtime lifetime, and separation of vendor
control-plane routing from target/proxy measurements. Mock vendor contracts verify
CloakBrowser license/capacity checks and Kameleo version/stop/export ordering; a
native Chrome process contract verifies the absence of automation launch flags.

Official API references checked on 2026-11-12:

- [Playwright persistent contexts](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
- [CloakBrowser wrapper and license APIs](https://github.com/CloakHQ/CloakBrowser)
- [Kameleo Playwright integration](https://developer.kameleo.io/integrations/playwright/)
- [Kameleo profile lifecycle](https://developer.kameleo.io/tutorials/managing-profiles/)
- [Kameleo proxy lifecycle](https://developer.kameleo.io/tutorials/using-proxy-servers/)
- [Oxylabs session control](https://developers.oxylabs.io/products/proxies/residential-proxies/session-control)
- [Oxylabs error semantics](https://developers.oxylabs.io/products/proxies/residential-proxies/additional-information)

Proxy capability references checked for this correction on 2026-11-12:

- [Residential session parameters and single-IP binding](https://developers.oxylabs.io/products/proxies/residential-proxies/session-control)
- [ISP assigned ports](https://developers.oxylabs.io/help-center/getting-started/start-using-isp-proxies)
- [Dedicated ISP ports and rotation](https://developers.oxylabs.io/help-center/getting-started/start-using-dedicated-isp-proxies)
- [Datacenter fixed and rotating ports](https://developers.oxylabs.io/help-center/getting-started/start-using-datacenter-proxies-per-ip)
- [Dedicated datacenter assigned ports](https://developers.oxylabs.io/help-center/getting-started/start-using-dedicated-datacenter-proxies)

## Decodo pre-flight and rollout monitoring

The runner probes each live Decodo assignment after claiming the lease and before restoring a profile or launching a browser. The existing batch preview reads configuration and capacity. Each attempt records its pre-flight checks, source evidence, and phase timings in Session Details. The post-run IP check and the instrumented site's observed IP retain their own provenance.

The pre-flight blocks on gateway-confirmed invalid credentials, a confirmed country outside the US, a confirmed ASN outside the policy's optional `allowed_asns` list, or an explicit hosting classification. Credential rejection pauses the inventory entry. MaxMind datasets are optional. When the configured files are missing, pre-flight still checks the proxy and target TLS. Provider-only country and ASN values remain unconfirmed, and network classification remains unavailable. Missing datasets alone do not block a run. Sticky leases receive a second IP lookup on the same binding. Reachability, continuity and latency findings are advisory.

One transient retry can use the same binding after a 200 ms backoff within the 10-second pre-flight budget. Its timing row separates `retry_backoff` from probe latency. Gateway authentication, quota, TLS validation, rejected DNS answers, and malformed CONNECT responses skip the retry. The target reachability probe performs only a TLS handshake on the authorized HTTPS origin.

For independent lookup evidence, place `GeoLite2-City.mmdb`, `GeoLite2-ASN.mmdb`, and `GeoIP2-Anonymous-IP.mmdb` under the ignored `.abrl/geoip/` directory and set `ABRL_GEOIP_CITY_DB`, `ABRL_GEOIP_ASN_DB`, and `ABRL_GEOIP_ANONYMOUS_DB` to their paths on the runner. GeoLite downloads require a MaxMind account. GeoIP Anonymous IP requires a separate entitlement. In a diagnostic bundle, `proxy_network.exit_lookup.evidence` reports each field's pre-flight reason and dataset status. A passing sticky check confirms continuity across the probe samples. [MaxMind download setup](https://dev.maxmind.com/geoip/updating-databases/)

The authenticated `GET /api/v1/proxy-preflight/summary?limit=200` endpoint summarizes the most recent recorded attempts in the current project. It includes missing-evidence rates, block reasons, sticky comparisons and changes, and p50/p95 timings in milliseconds. The sample limit is 1–1000. A cancelled or early-blocked attempt can have checks that were never reached.

For multiple inventory entries sharing a provider account, set the same `ABRL_PROXY_ACCOUNT_GROUPS` JSON on the API, scheduler and runners. Each named group specifies `provider`, an array of `username_refs`, and `max_sessions`. An active lease takes one group slot regardless of its reserved socket count. A quarantined session holds its slot until recovery releases the lease. Configure group membership using credential reference names. See `.env.example` for an example.

### Owned browser diagnostic

Run the dedicated fixture locally with the following commands in separate terminals. An existing `make dev-fixtures` process that includes the diagnostic routes can also serve the page.

```sh
uv run uvicorn abrl.diagnostic_fixture:app --host 127.0.0.1 --port 8400 --no-proxy-headers --no-access-log
uv run python -m abrl.browser_diagnostics
```

The command drives the normal browser adapter through the session broker. Optional `--expected-language`, `--expected-timezone`, and `--device-preset` arguments declare the QA case. The report includes browser and HTTP observations, agreement checks, and the clock measurement.

#### Live Decodo smoke in report mode

1. On the fixture host, replace the example origin with a public HTTPS origin you own. Use a trusted TLS certificate and public IPv4 DNS. Point your HTTPS reverse proxy or temporary tunnel at the standalone fixture on port 8401. The fixture serves only the diagnostic page and its echo.

   ```sh
   export ABRL_DIAGNOSTIC_URL='https://diagnostics.example.com'
   uv run --locked uvicorn abrl.diagnostic_fixture:app --host 127.0.0.1 --port 8401 --no-proxy-headers --no-access-log
   ```

2. In the runner shell, set `ABRL_DIAGNOSTIC_URL` to the same owned origin. The CLI also accepts its exact `/lab/diagnostics` URL. Check that both HTTPS paths return `200` without a redirect. The CLI rejects placeholder hosts, query strings, fragments, and other paths.

   ```sh
   export ABRL_DIAGNOSTIC_URL='https://diagnostics.example.com'
   origin=${ABRL_DIAGNOSTIC_URL%/lab/diagnostics}
   origin=${origin%/}
   for path in /lab/diagnostics /lab/diagnostics/echo; do
     printf '%s ' "$path"
     curl --fail --silent --show-error --output /dev/null --write-out '%{http_code}\n' "$origin$path"
   done
   ```

3. On the runner, load `ABRL_SECRET_DECODO_USERNAME` and `ABRL_SECRET_DECODO_PASSWORD` from the host's secret store. Use the raw dashboard proxy username. The runner needs outbound TLS access to `gate.decodo.com:7000`. Install Chromium with `uv run --locked playwright install chromium` if it is missing.
4. Run the smoke from the repository root. The standalone runner uses a fresh browser context and a sticky Decodo binding by default.

   ```sh
   umask 077
   mkdir -p .abrl
   report_dir=$(mktemp -d .abrl/decodo-smoke.XXXXXX)
   report="$report_dir/report.json"
   smoke_status=0
   uv run --locked python -m abrl.browser_diagnostics --adapter playwright --decodo --redacted > "$report" || smoke_status=$?
   printf 'Decodo smoke exit code %s\nReport: %s\n' "$smoke_status" "$report"
   ```

   The CLI writes one JSON object with a UTC `observed_at` timestamp, a `status`, and the Decodo `preflight` report. The exit code is `0` for `passed`, `1` for `failed`, and `2` for `partial`. Invalid arguments or configuration also exit `2`, with a usage error on stderr and an empty report file. A reachable fixture and completed browser probe can produce `partial` when independent geolocation evidence is unavailable. Review `observation_status`, the pre-flight checks, and the timeline before interpreting the result. An early pre-flight block can return before launching the browser. The report omits raw exit IPs, proxy credential values, cookies, and ICE candidates. `--redacted` keeps check results and bounded observations while dropping detailed browser and server values.

5. Inspect the recorded probe status and timings. If the report file is empty, check the command's stderr first.

   ```sh
   uv run --locked python - "$report" <<'PY'
   import json
   import sys

   with open(sys.argv[1]) as source:
       report = json.load(source)
   preflight = report.get("preflight") or {}
   checks = preflight.get("checks") or {}
   print(json.dumps({
       **{name: report.get(name) for name in (
           "status", "reason", "observation_status", "runtime_stopped",
           "first_causal_failure", "fixture_http_protocols", "lifecycle_timeline",
       )},
       "fixture_http_transport": (report.get("checks") or {}).get("fixture_http_transport"),
       "preflight_status": preflight.get("status"),
       "preflight_checks": {
           name: {key: checks.get(name, {}).get(key) for key in ("status", "reason", "dataset_status")}
           for name in ("target_tls", "sticky", "country", "asn", "network_type", "timezone")
       },
       "preflight_timings_ms": preflight.get("timings_ms"),
   }, indent=2))
   PY
   ```

To compare with a saved baseline report from the same fixture and adapter, run `uv run --locked python -m abrl.diagnostic_compare baseline.json "$report" > "$report_dir/comparison.json"`. Unconfirmed lookup values remain unknown in the comparison. The [November 23 verification record](delivery/decodo-live-verification-2026-11-23.json) records a hold with missing independent geographic and network evidence. Shut down the temporary endpoint and fixture listener after collecting the report.

For a WebRTC check, configure `ABRL_DIAGNOSTIC_STUN_URL` on the fixture with an owned STUN service on the same host. Supply the fixture with an accurate client peer address at its trusted ingress. Only the ICE verdict is retained.

When a tunnel terminates HTTPS, compare a direct request to the public echo with a request to the local echo. Set `ABRL_DIAGNOSTIC_EDGE_HEADERS` for the smoke process to the forwarding header names the tunnel added. Only `forwarded`, `x-forwarded-for`, `x-real-ip`, and `via` qualify on the configured owned origin. Proxy authentication and connection headers still fail the alignment check. The echo returns header presence by name and omits forwarding values.

The fixture records the browser's negotiated protocol for its document and same-origin echo. A reported `h3` on either request flags a mismatch with the TCP proxy transport. Missing protocol evidence or an unavailable launch-switch check makes an otherwise passing report partial. Chromium can withhold command-line inspection. The report retains only the launch verdict and allowlisted protocol names. Reverse proxies can negotiate another protocol upstream. [W3C Resource Timing](https://www.w3.org/TR/resource-timing/#dom-performanceresourcetiming-nexthopprotocol)

Session Details groups failed main-origin subresources by resource type and cause. Counts cover blocked or aborted requests, network failures, and HTTP 4xx and 5xx responses. The counts omit URLs, headers, and response bodies. The session keeps its existing asset policy, request budget, and target rate limit. Before a managed action or operator input proceeds, the runner rechecks the assigned proxy lease for ownership, expiry, and session epoch.

For a managed diagnostic, use a fresh automated browser, a browser-only target restricted to the configured fixture origin, and a workflow with a single navigation to `/lab/diagnostics`. Session Details shows the attempt's diagnostic verdicts. The managed diagnostic omits the post-run IP value, raw browser observations, and capture artifacts. An ordinary browser-only workflow keeps its existing post-run IP behavior.

#### Load timing and run comparison

The load timeline in Session Details and diagnostic JSON uses a monotonic attempt clock. `elapsed_ms` measures from the start of attempt instrumentation. `navigation_delta_ms` measures from the first dispatched navigation, with negative offsets for earlier milestones and `null` if navigation was never dispatched. Entries appear as their milestones are reached, including initial document commit, page load, the first interaction or post-load workflow step, and the time a browser failure was observed.

A navigation may commit before its initial `DOMContentLoaded` deadline and finish loading during cleanup. Session Details marks milestones recorded after the failure and shows the wait stage and deadline in Attempt failure. The navigate step's initial timeout applies through `DOMContentLoaded`. Its post-load hold starts only after that first wait succeeds.

Set a Navigate or Open tab step's initial timeout to override that step. A blank step inherits the workflow's **Default initial navigation timeout**, then the assigned proxy policy's **Initial navigation timeout**. With both defaults blank, a provider-backed session uses 15,000 ms and a direct or local session uses 5,000 ms. A direct fallback uses the direct default. The overall workflow budget can still expire sooner.

Workflow and proxy-policy defaults are versioned. A saved step with a 5,000 ms value continues to use that value. To inherit, clear the step timeout and save a new workflow revision. Diagnostic bundles include the first navigation's resolved timeout, and **Session Details → Attempt failure** shows the source for a navigation timeout. Reports use `step_override` for an explicit step or workflow-level override, `proxy_policy` for the policy setting, and `proxy_default` or `direct_default` for built-in defaults.

When a probe on the owned fixture terminates early, its failure evidence includes a fixed stage for fetch, invalid JSON, an unexpected echo response, or a runtime error. It omits the body and exception text. A retried session keeps the prior attempt's bounded elapsed time, timeline, and tunnel totals in its activity events.

**Navigation CONNECT telemetry** in Session Details groups local browser proxy observations by main-frame request, including a separate row for each redirect hop. A recorded status next to **Decodo** is the provider's CONNECT response. A status next to **Local browser proxy** comes from ABRL, and **Connection to upstream proxy** identifies an error while reaching or reading the provider. Matches use destination and timing. The broker keeps a brief in-memory window for CONNECTs that arrive before the browser's request event. Concurrent requests can make a match ambiguous, and a reused tunnel can leave no CONNECT evidence. The diagnostic bundle and standalone report include the same bounded events without destinations, headers, or provider response text.

The owned fixture reports fixed capability states for audio, canvas, WebGL, fonts, and selected runtime APIs. It also reports bounded document and echo timings when the browser exposes them. A capability can be `available`, `unsupported`, or `unavailable`. Connection timings have their own observation status and use `null` for unmeasured values.

Compare saved standalone reports or JSON from `GET /api/v1/sessions/{session_id}` with the same command:

```sh
uv run --locked python -m abrl.diagnostic_compare baseline.json candidate.json
```

The comparison returns one JSON object with confirmed ASN and country from the preflight lookup, sticky continuity status, document and echo protocols, lifecycle milestones, diagnostic verdicts, and bounded environment observations such as viewport dimensions and timezone offset. Timing differences are candidate minus baseline in milliseconds. Pre-flight probe timings are grouped by probe and attempt in capture order, including any measured retry delay.

HTTP response counts are split by main frame and subresource. Main-origin subresource failures are grouped by resource type and cause. Count differences are candidate minus baseline. When a run recorded counters, unlisted HTTP statuses and resource failures count as zero. Missing counter or timing evidence remains unknown. The comparison omits source paths, session identifiers, raw exit IPs, credentials, cookies, auth tokens, and ICE candidates.

#### Exporting a diagnostic bundle

In **Sessions**, open the flagged session and choose **Export diagnostic bundle**. Automatic selection looks for a completed session without a recorded failure in the same batch and launch configuration or a different batch with matching pinned target and workflow revisions and launch settings. It prefers a baseline with a recorded passing fixture verdict. `baseline_evidence` is `passed`, `partial`, `workflow_completed`, or `null` when no baseline was attached. A `partial` baseline can support timing and browser comparisons when MaxMind evidence is missing. `workflow_completed` denotes a completed workflow without a fixture verdict. Enter a full session ID or a unique prefix of at least eight characters to choose a specific baseline from the project. Clear **Include a clean baseline when available** and leave the ID blank for a single-session export.

The download contains a versioned JSON object with `session` and an optional `baseline`. Each record contains a proxy and network summary, browser and diagnostic observations, and the lifecycle and outcome. It separates recorded evidence from configured network policy and preserves unknown fields for measurements the session did not collect. The bundle excludes raw header values, exit IPs, credentials, cookies, auth tokens, URLs, and raw ICE candidates.

Compare a paired bundle offline with `uv run --locked python -m abrl.diagnostic_compare bundle.json`. `first_causal_failure.navigation_wait` identifies the active browser wait, deadline source, and configured navigation budgets. When the broker records a failed first tunnel, `first_causal_failure.tunnel_wait` identifies the gateway DNS, gateway TLS, or proxy CONNECT stage and its elapsed time. The comparison retains both waits when present.

Assigning a proxy policy to an owned diagnostic makes its provider binding mandatory. Batch preview, admission, and lease claim use the same selection rule. An assignment that reaches the worker without the expected binding is blocked before launch. For a direct control run, select direct connectivity explicitly.

The standalone Decodo smoke allows two concurrent target connections because the fixture fetches its echo while the document connection can remain open. The managed smoke verified on Chromium used `connection_load: 2` in its diagnostic policy. A failed echo records a network or HTTP failure. The attempt's transport summary includes the number of connections the broker refused at its configured limit.
