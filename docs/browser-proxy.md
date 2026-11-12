# Browser execution, profile storage and proxy contracts

The local execution path launches a real browser for each assignment. A reusable
profile is an explicit directory owned by a database lease and an additional
nonblocking filesystem lock. Independent sessions must use independent profiles.
`execute_workflow` retains its original keyword interface and adds `adapter`,
`profile_dir`, `preparation`, `proxy`, `emit`, `control`, `remote`,
`expected_compatibility`, and `artifact_policy`.

## Adapters and readiness

`adapter_capabilities()` performs local checks only. `configured` means the package
and local prerequisites exist, not that a licensed browser has passed a live trial.
The launch records CDP's actual browser product, revision and User-Agent, wrapper
version, host OS and architecture. No custom fingerprint flags are accepted.

- `playwright`: stock Chromium, fresh context or `launch_persistent_context`.
- `manual_chrome`: installed Google Chrome with a headed display, launched as a
  native subprocess with a dedicated profile and without Playwright automation
  launch flags. CDP attaches afterward for scope enforcement, observations and
  operator input. This attachment is explicitly recorded as a research confound;
  the lab also offers an unattached manual control. An operator performs the
  actions; automation evaluates assertions after handoff. An explicit success
  assertion and operator channel are required. Cloud execution uses a separately
  configured `ABRL_BROWSER_IMAGE_MANUAL_CHROME` image with installed Google Chrome
  and Xvfb. The browser pod checks the executable and `DISPLAY`; the API does not
  infer them from its own host. A loopback relay in the isolated browser pod
  injects the ephemeral proxy-access capability toward the authenticated broker;
  Chrome receives a credential-free proxy URL. No authentication secret enters
  Chrome command-line arguments or profile settings.
- `cloakbrowser`: optional current CloakBrowser wrapper and preinstalled binary.
  Set `CLOAKBROWSER_LICENSE_KEY`. License validity and available seats are checked
  before launch; vendor enforcement remains authoritative. The default baseline
  uses `humanize=False` and `geoip=False`. Free-tier concurrency is one; exact binary
  pinning requires a Pro entitlement. `ABRL_CLOAK_TIER=pro` allows a pin to reach the
  live entitlement check; it does not independently grant a license.
- `kameleo`: optional `kameleo.local-api-client`, with
  `ABRL_KAMELEO_ENDPOINT=http://127.0.0.1:5050`. The worker and engine must share the
  network namespace and exported profile directory. Linux requires amd64. Actual
  engine version comes from the SDK user-info response, and major/minor must match
  the SDK. A single Chroma context belongs to each profile. Stop precedes export,
  and successful export precedes deletion of the loaded engine profile. Import
  retains vendor profile identity. Uncertain saves retain that engine profile for
  reconciliation and quarantine.

`runtime_stopped` and `profile_saved` become true only after confirmed shutdown
(and, for Kameleo persistence, successful export). A failed shutdown is not a saved
profile. A crashed/partitioned runtime requires controller quarantine; lease
expiration alone cannot grant the next writer. A local lock does not provide
cross-host fencing.

## Preparation and operator control

Preparation is disabled by default. Automated preparation takes `seed`,
`min_seconds`/`max_seconds` (defaults 30/90; maximum 300), and optional relative
`paths`. It records a reproducible plan, actual actions, and its own elapsed time.
Actions are scrolling, waiting, and navigation on the registered target. The
browser blocks non-GET/HEAD traffic during this phase. It is always labelled
automated activity. It never clicks transactions or changes behavior after a
provider verdict.

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
filter geography, explicitly permitted fallback regions, product, continuity,
exclusive inventory, confirmed duration, expiry, adapter compatibility and
capacity. Eligible candidates rank by measured transport success, latency,
utilization and known cost. Unknown data stays null. Detector verdicts never enter
the transport score. An eligible existing binding is retained for continuity.

The proxy binding supports `residential`, `isp`, `dedicated_isp`, `datacenter` and
`dedicated_datacenter`. It requires the documented Oxylabs gateway `host`/`port`,
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

Direct mode validates all DNS answers and dials the validated literal public IPv4
address. With Oxylabs, the hostname is resolved again by the provider; configurations
must explicitly set `provider_resolved_ack=true`. A local validation cannot prove
which address an upstream resolver used. This boundary is included in metadata.
Navigation and resource origins remain distinct at the browser interception layer.
WebSockets and service workers are currently blocked, not silently unguarded.
Auxiliary arbitrary request clients are not exposed to workflow definitions.

The local proxy and Chromium settings are **not an OS-level browser sandbox**.
Arbitrary `ABRL_BROWSER_WS_ENDPOINT` or `ABRL_EGRESS_PROXY_URL` settings fail
readiness. Supported remote execution instead requires the controller-created
`remote` capability described below. A cloud browser pod has no DB/Redis
credentials and only broker egress; the runner has a separate network policy.
Actual Kubernetes network isolation remains a deployment validation gate; local
smoke tests alone do not verify it.

## Separate browser and broker runtimes

`python -m abrl.remote_browser browser` exposes an authenticated HTTP/CDP service
on 9222; `python -m abrl.remote_browser broker` exposes the session egress proxy and
controller endpoints on 3128. The browser and broker must run in distinct pods.
`CloudRuntime` creates per-session services, network policies and a secret, then
provides `execute_workflow(remote=runtime.connection)` with the private endpoints,
session ID, ownership epoch and ephemeral master capability. The master capability
stays in runner memory. Separate HMAC-derived browser-control, broker-control and
proxy-access capabilities are distributed only where needed; a proxy password
cannot renew a broker ownership lease. Cloud readiness comes from configured
adapter images; the browser process independently checks the installed package,
binary, entitlement and engine compatibility before launch.

Both processes require `ABRL_REMOTE_TOKEN`, `ABRL_REMOTE_PROXY_TOKEN`,
`ABRL_REMOTE_SESSION_ID`, and `ABRL_REMOTE_EPOCH`. The browser also requires
`ABRL_REMOTE_PROXY_URL`; the broker requires `ABRL_EGRESS_CONFIG_JSON` containing
only the authorized origins and selected proxy's secret references. Provider
credentials enter only the broker. The Chromium subprocess receives a sanitized
environment without ABRL, database, queue or provider secrets.

Control calls require the bearer capability plus `X-ABRL-Session` and
`X-ABRL-Epoch`. The broker starts with forwarding disabled. The runner attaches
navigation/resource interception, verifies browser/profile compatibility, then
activates forwarding. Separate watchdogs close the browser and its network route
after a 30-second lease expires; expiry cannot be reversed. The runner renews both
leases every three seconds. `ABRL_REMOTE_LIFETIME_SECONDS` additionally caps the
absolute lifetime at up to 86400 seconds, independent of successful renewals. A
lease expiry during vendor startup cancels launch and awaits its cleanup. The
initial 180-second startup grace permits pod
startup while target forwarding remains disabled. Browser shutdown is acknowledged
before profile export, and the Kubernetes controller additionally confirms the
owned pod UIDs are absent before releasing ownership.

Remote persistent Chromium profiles use authenticated encrypted upload before
launch and download only after confirmed clean shutdown. Transfer keys are derived
from the per-runtime browser capability; the master profile-storage encryption key
never enters the browser pod. The runner restores the returned profile into a new
directory, then publishes it through the regular encrypted `ProfileStore`.
Transfers are limited to 256 MiB compressed. Exact browser/OS/architecture checks
apply. Fresh and persistent remote standard Chromium have real local CDP/broker
tests. Remote CloakBrowser and Kameleo use the same documented adapter lifecycle,
including entitlement checks, vendor-default context preservation, stop/export, and
encrypted transfer. Their SDK contracts are tested with mocks; actual licensed
execution and Kubernetes deployment remain pending external prerequisites.

CloakBrowser uses its configured license origin (`https://cloakbrowser.dev`).
Kameleo requires an explicit allowlist of licensing/control origins, the pinned
amd64 engine image and its PAT secret. Those origins are available during startup
through the authenticated broker, are excluded from proxy health measurements, and
do not become workflow navigation/resource permissions. Licensing traffic goes
directly from the broker rather than through the experimental proxy. Target
traffic remains disabled until the runner attaches interception and validates
profile compatibility. Kameleo and native Chrome use the loopback relay for
proxy authentication, avoiding conflicting CDP interception layers. The relay
can connect only to the configured session broker, receives no control/provider
credentials, and closes with the browser runtime. Vendor credentials never enter
browser profiles. Licensed image builds must preinstall their browser
binaries: the runtime does not broaden the control-origin allowlist for downloads.

## Optional diagnostic artifacts

`artifact_policy` defaults to `none`; `on_failure` and `all` capture a bounded
viewport screenshot and up to 100 console entries. Secret-reference input
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
The present local packer buffers up to its 2 GiB profile limit in memory; cloud
capacity sizing must account for this or replace it with streaming archival.
Object-storage upload, versioned retention, backup and distributed ownership are
controller responsibilities. Cross-OS portability is not assumed.

## Verification evidence and vendor references

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

Proxy policy `connection_load` (default 1, maximum 32) reserves that number of
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
