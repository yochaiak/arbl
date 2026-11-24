"use client";

import { useCallback, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  Check,
  ChevronRight,
  Clock3,
  Download,
  CopyPlus,
  Monitor,
  MousePointer2,
  Pause,
  Play,
  Plus,
  Search,
  ShieldCheck,
  Square,
  X,
} from "lucide-react";
import {
  api,
  dateTime,
  ApiError,
  label,
  ms,
  request,
  saveFile,
  validateWorkflowUrlOverrides,
  workflowUrlParameterNames,
  type Target,
  type Workflow,
} from "@/lib/api";
import {
  type Adapter,
  type Batch,
  type BatchItem,
  type BatchInput,
  type BrowserSession,
  type PageResult,
  type Profile,
  type ProxyPolicy,
  platformKey,
  sessionActive,
  humanValue,
} from "@/lib/platform";
import type { components } from "@/lib/schema";
import {
  Badge,
  Empty,
  ErrorNotice,
  Field,
  Loading,
  SectionTitle,
  Select,
} from "./ui";
import { DetailGrid, ListControls, Modal, Pagination, Segments, useListState } from "./platform-ui";
import { BatchCatalog, BatchPresetFields, BatchReadinessNotices, useBatchPreflight, WorkflowValuesFields } from "./batch-setup";
import { DiagnosticBundleExport } from "./session-diagnostic-export";
import { DiagnosticCapabilities, LifecycleTimeline, NavigationConnects } from "./session-diagnostics";
import { RecordingAction, SessionRecordingPlayer } from "./session-recording";
import { SessionTemplates, type SessionTemplate } from "./session-templates";
import { WorkflowUrlParameters } from "./workflow-url-parameters";
import {
  allAdaptersSupport,
  deviceModeLabel,
  devicePresetLabel,
  DevicePresetField,
  UserAgentField,
  validUserAgent,
} from "./session-browser-settings";

type BatchIPSummary = components["schemas"]["BatchIPSummary"];
type BatchProxyCheckIPSummary = components["schemas"]["BatchProxyCheckIPSummary"];

const adscoreRequestFailureLabels = {
  scope_blocked: "Blocked by ABRL scope",
  network_error: "Browser network error",
  http_error: "HTTP error",
} as const;

type AdScoreRequestFailure = {
  url: string;
  kind: keyof typeof adscoreRequestFailureLabels;
  method: string;
  resource_type: string;
  phase: string;
  step: number;
  status?: number;
  error?: string;
};

type AdScoreMeasurementRequest = {
  url: string;
  method: string;
  status?: number;
  error?: string;
  referrer_origin?: string | null;
};

type AdScoreReferrerPolicy = {
  url: string;
  request_policy?: string;
  document_policy?: string | null;
};

type AdScoreRequestInitiator = {
  url: string;
  status?: number;
  initiator_type?: string;
  initiator_origin?: string;
  document_origin?: string;
  resource_type?: string;
  context?: string;
  initial_referrer_present?: boolean;
  paused_referrer_present?: boolean;
  network_referrer_present?: boolean;
  redirected?: boolean;
};

function headerPresence(present?: boolean) {
  return present === undefined ? "Not captured" : present ? "Present" : "Absent";
}

const proxyCheckIPHint = "Address returned by the post-run provider diagnostic. The website can see a different IP.";

const batchIPStatuses = {
  duplicate: { label: "Duplicate IPs", tone: "rose" },
  unique: { label: "Unique", tone: "green" },
  incomplete: { label: "Incomplete", tone: "amber" },
  pending: { label: "Pending", tone: "quiet" },
} as const;

const sessionEventTitles = new Map([
  ["controlled_research_entry", "Controlled research entry"],
  ["proxy_preflight", "Decodo pre-flight"],
  ["proxy_lease_expired", "Proxy lease expired"],
  ["connection_recovery_queued", "Connection recovery queued"],
  ["session_started", "Session attempt started"],
  ["operator_step_reserved", "Step reserved for operator"],
  ["session_attempt_finished", "Session attempt finished"],
  ["session_finished", "Session finished"],
]);

const proxyTransportTitles = new Map([
  ["proxy_gateway_timeout", "Proxy gateway timed out"],
  ["proxy_tunnel_timeout", "Proxy tunnel timed out"],
  ["proxy_tunnel_failed", "Proxy tunnel failed"],
  ["proxy_destination_denied", "Proxy denied CONNECT destination"],
]);

function sessionEventTitle(kind: string, detail?: Record<string, unknown>) {
  if (kind === "proxy_transport_issue") {
    const reason = detail?.reason;
    return (typeof reason === "string" && proxyTransportTitles.get(reason)) || "Proxy transport issue";
  }
  return sessionEventTitles.get(kind) ?? label(kind);
}

function sessionEventDetail(kind: string, detail: Record<string, unknown>) {
  if (kind === "operator_step_reserved") {
    const path = Array.isArray(detail.step_path) && detail.step_path.length > 0
      ? detail.step_path.join(".")
      : detail.step;
    return [path && `Step ${path}`, typeof detail.action === "string" && label(detail.action)].filter(Boolean).join(" · ");
  }
  if (kind === "proxy_preflight") {
    return [`Attempt ${detail.attempt}`, label(String(detail.status)), detail.reason && label(String(detail.reason))].filter(Boolean).join(" · ");
  }
  if (kind === "controlled_research_entry" ||
    (kind === "provider_observation" && "received_referrer_host" in detail)) {
    const referrerHost = typeof detail.received_referrer_host === "string"
      ? detail.received_referrer_host
      : "unavailable";
    const referrer = `Received referrer host ${referrerHost}`;
    if (kind === "controlled_research_entry") return referrer;

    return [
      referrer,
      typeof detail.callback_state === "string" && `Callback ${label(detail.callback_state)}`,
      typeof detail.callback_error === "number" && `Callback code ${detail.callback_error}`,
      typeof detail.signature_present === "boolean" &&
        (detail.signature_present ? "Signature present" : "Signature absent"),
      typeof detail.verification === "string" && `Verification ${label(detail.verification)}`,
      typeof detail.verification_reason === "string" && `Verification reason ${label(detail.verification_reason)}`,
      typeof detail.raw_code === "number" && `Raw code ${detail.raw_code}`,
      typeof detail.accepted === "boolean" && `Accepted ${humanValue(detail.accepted)}`,
    ].filter(Boolean).join(" · ");
  }

  return Object.entries(detail)
    .filter(([key]) =>
      (kind !== "proxy_transport_issue" || key !== "reason") &&
      !["jpeg_base64", "signature", "token", "password"].includes(key),
    )
    .slice(0, kind === "proxy_transport_issue" ? 4 : 3)
    .map(([key, value]) => key === "upstream_status"
      ? `Upstream HTTP ${humanValue(value)}`
      : `${label(key)}: ${humanValue(value)}`)
    .join(" · ");
}

function sessionWorkflowLabel({ workflow_name, workflow_version }: BrowserSession) {
  if (workflow_name && workflow_version != null) return `${workflow_name} · v${workflow_version}`;
  if (workflow_name) return workflow_name;
  return workflow_version == null ? "Unavailable" : `Version ${workflow_version}`;
}

const queueReasons: Record<string, string> = {
  batch_paused: "Batch is paused", retry_backoff: "Waiting to retry", global_capacity: "Workspace capacity reached",
  project_capacity: "Project capacity reached", batch_capacity: "Batch concurrency reached",
  experiment_capacity: "Historical run concurrency reached", site_capacity: "Site capacity reached",
  site_rate_limit: "Waiting for the site start-rate limit", profile_busy: "Waiting for the profile",
  worker_unavailable: "Waiting for a worker", worker_capacity: "All worker slots are busy",
  adapter_capacity: "Browser capacity reached", proxy_capacity: "Waiting for an eligible proxy",
};

function queueReason(reason?: string | null) {
  return reason ? queueReasons[reason] ?? label(reason) : null;
}

function viewportConformance(session: BrowserSession) {
  const evidence = session.metadata.viewport_conformance as { checks?: unknown; matched?: unknown } | undefined;
  if (!session.attempt_number || !evidence) return undefined;
  const { checks, matched } = evidence;
  if (typeof checks !== "number" || typeof matched !== "number"
    || !Number.isSafeInteger(checks) || !Number.isSafeInteger(matched)
    || checks <= 0 || matched < 0 || matched > checks) return undefined;
  return { checks, matched };
}

function DecodoPreflight({ report, attempt }: { report: unknown; attempt: number }) {
  if (!report || typeof report !== "object") return null;
  const data = report as Record<string, unknown>;
  if (data.attempt !== attempt || typeof data.status !== "string") return null;
  const items: [string, string][] = [];
  const checks = data.checks as Record<string, { status?: string; observed?: unknown; source?: string }> | undefined;
  for (const [key, title] of [
    ["exit_ip", "Public exit IP"], ["country", "Country"], ["asn", "ASN"], ["network_type", "Network type"],
    ["sticky", "Sticky IP"], ["target_tls", "Target TLS"], ["latency", "Tunnel latency"],
  ]) {
    const check = checks?.[key];
    if (!check?.status) continue;
    const value = check.observed == null ? [] : [humanValue(check.observed)];
    items.push([title, [...value, label(check.status), ...(check.source ? [label(check.source)] : [])].join(" · ")]);
  }
  return <section>
    <h3>Decodo pre-flight</h3>
    <p>Attempt {attempt} · {label(data.status)}{typeof data.reason === "string" && ` · ${label(data.reason)}`}</p>
    <DetailGrid items={items} />
    <details>
      <summary>Detailed evidence and timings</summary>
      <pre className="json-preview">{JSON.stringify({ checks: data.checks, timings_ms: data.timings_ms, probe_attempts: data.probe_attempts }, null, 2)}</pre>
    </details>
  </section>;
}

function BrowserDiagnostic({ data, attempt }: { data: Record<string, unknown>; attempt: number }) {
  if (typeof data.status !== "string") return null;
  const checks = data.checks && typeof data.checks === "object" ? Object.entries(data.checks) : [];
  const checkTitles: Record<string, string> = {
    ice: "WebRTC ICE", fixture_http_transport: "TCP fixture transport", quic_launch_flag: "QUIC disabled at launch",
  };
  const items: [string, string][] = checks.flatMap(([name, status]) => typeof status === "string"
    ? [[checkTitles[name] ?? label(name), label(status)]] : []);
  if (!checks.length && ["passed", "mismatch", "unavailable"].includes(String(data.quic_launch_flag))) {
    items.push([checkTitles.quic_launch_flag, label(String(data.quic_launch_flag))]);
  }
  const protocols = data.fixture_http_protocols as Record<string, unknown> | undefined;
  for (const [field, title] of [["document", "Document protocol"], ["echo", "Echo protocol"]]) {
    const protocol = protocols?.[field];
    if (typeof protocol === "string" && ["http/1.0", "http/1.1", "h2", "h3", "unavailable"].includes(protocol)) {
      items.push([title, protocol]);
    }
  }
  return <section>
    <h3>Owned browser diagnostics</h3>
    <p>Attempt {attempt} · {label(data.status)}{typeof data.reason === "string" && ` · ${label(data.reason)}`}</p>
    {typeof data.failed_check === "string" && <p>First mismatch {label(data.failed_check)}</p>}
    {items.length > 0 && <DetailGrid items={items} />}
    {typeof data.minimum_clock_skew_ms === "number" && (
      <p className="muted small-text">Minimum clock skew {ms(data.minimum_clock_skew_ms)}</p>
    )}
    {typeof data.connection_capacity_rejections === "number" && (
      <p className="muted small-text">Proxy connections rejected at capacity {data.connection_capacity_rejections}</p>
    )}
    <DiagnosticCapabilities data={data} />
  </section>;
}

function MainOriginFailures({ counts }: { counts: unknown }) {
  if (!counts || typeof counts !== "object") return null;
  const totals = counts as Record<string, unknown>;
  const items: [string, string][] = [];
  for (const type of [
    "stylesheet", "script", "image", "font", "media", "manifest", "texttrack", "fetch", "xhr",
    "eventsource", "ping", "preflight", "other",
  ]) {
    const countsByReason = totals[type];
    if (!countsByReason || typeof countsByReason !== "object") continue;
    const failures = [];
    for (const [reason, title] of [
      ["blocked", "blocked"], ["aborted", "aborted"], ["network_error", "network error"],
      ["http_4xx", "HTTP 4xx"], ["http_5xx", "HTTP 5xx"],
    ]) {
      const count = (countsByReason as Record<string, unknown>)[reason];
      if (typeof count === "number" && Number.isSafeInteger(count) && count > 0) failures.push(`${title} × ${count}`);
    }
    if (failures.length) items.push([label(type), failures.join(" · ")]);
  }
  return items.length ? <section><h3>Main-origin subresource failures</h3><DetailGrid items={items} /></section> : null;
}

function WorkflowFailure({ failure }: { failure: unknown }) {
  if (!failure || typeof failure !== "object") return null;
  const detail = failure as Record<string, unknown>;
  const wait = detail.navigation_wait as Record<string, unknown> | undefined;
  const tunnelWait = detail.tunnel_wait as Record<string, unknown> | undefined;
  const timeoutSourceNames = new Map([
    ["step_override", "Step or workflow override"],
    ["proxy_policy", "Proxy policy"],
    ["proxy_default", "Proxy default"],
    ["direct_default", "Direct connection default"],
  ]);
  const timeoutSource = typeof wait?.load_timeout_source === "string"
    ? timeoutSourceNames.get(wait.load_timeout_source) : undefined;
  const stageNames: Record<string, string> = {
    initial_dom_content_loaded: "Initial DOMContentLoaded",
    post_navigation_dom_content_loaded: "DOMContentLoaded after the hold",
    post_navigation_delay: "Post-load hold",
    post_navigation_challenge_check: "Check after navigation",
    post_step_challenge_check: "Check after the step",
    url_condition: "URL condition",
  };
  const status = detail.status ?? detail.http_status;
  return <section>
    <h3>Attempt failure</h3>
    <DetailGrid items={[
      ["Step", Array.isArray(detail.step_path) ? detail.step_path.join(".") : String(detail.step_path ?? detail.step ?? "Unrecorded")],
      ["Action", typeof detail.action === "string" ? label(detail.action) : "Unrecorded"],
      ["Phase", typeof detail.phase === "string" ? label(detail.phase) : "Unrecorded"],
      ["Reason", typeof detail.reason === "string" ? label(detail.reason) : "Unrecorded"],
      ...(typeof status === "number" ? [["HTTP status", status] as [string, number]] : []),
      ["Elapsed", typeof detail.elapsed_ms === "number" ? ms(detail.elapsed_ms) : "Unrecorded"],
      ...(typeof wait?.stage === "string"
        ? [["Waiting for", stageNames[wait.stage] ?? label(wait.stage)] as [string, string]] : []),
      ...(typeof wait?.deadline === "string" ? [["Deadline", label(wait.deadline)] as [string, string]] : []),
      ...(typeof wait?.load_timeout_ms === "number"
        ? [["Initial load timeout", ms(wait.load_timeout_ms)] as [string, string]] : []),
      ...(typeof wait?.load_timeout_ms === "number" && timeoutSource
        ? [["Timeout source", timeoutSource] as [string, string]] : []),
      ...(typeof wait?.post_navigation_wait_ms === "number" && wait.post_navigation_wait_ms > 0
        ? [["Post-load hold", ms(wait.post_navigation_wait_ms)] as [string, string]] : []),
      ...(typeof tunnelWait?.stage === "string"
        ? [["Tunnel stage", label(tunnelWait.stage)] as [string, string]] : []),
      ...(typeof detail.probe_failure === "string"
        ? [["Fixture probe", label(detail.probe_failure)] as [string, string]] : []),
    ]} />
  </section>;
}

export function defaultBatch(
  targets: Target[],
  workflows: Workflow[],
  preview?: Workflow | null,
): BatchInput {
  return {
    name: preview === undefined ? "Workflow batch" : "Workflow preview",
    target_id: targets.find((site) => site.live_ready !== false)?.id ?? "",
    workflow_id: preview?.id ?? workflows[0]?.id ?? "",
    count: 1,
    concurrency: 1,
    adapters: ["playwright"],
    persistence: "fresh",
    artifact_policy: "all",
    capture_javascript: true,
    artifact_retention_days: 30,
    timeout_seconds: 600,
    request_budget: 1000,
    traffic_budget_bytes: 104857600,
    idempotency_key: crypto.randomUUID(),
  };
}

function policyMatchesTarget(policy: ProxyPolicy, target?: Target): boolean {
  const origin = policy.target_origin;
  return !origin || (
    target?.origin === origin &&
    [...(target.navigation_origins ?? []), ...(target.resource_origins ?? [])].every(
      (allowedOrigin) => allowedOrigin === origin,
    )
  );
}

function supportsBatchPresets(value: BatchInput, adapters: Adapter[]): boolean {
  return value.mode !== "demo" && value.adapters.length > 0
    && value.user_agent == null && value.device_preset == null
    && value.adapters.every((id) => adapters.some((adapter) =>
      adapter.id === id && adapter.features?.batch_preset_application,
    ));
}

export function BatchFields({
  token,
  value,
  onChange,
  targets,
  workflows,
  adapters,
  policies,
  profiles = [],
  launchParameters,
  sessionTemplates = false,
  onTarget,
  onWorkflow,
}: {
  token: string;
  value: BatchInput;
  onChange: (value: BatchInput, template?: SessionTemplate) => void;
  targets: Target[];
  workflows: Workflow[];
  adapters: Adapter[];
  policies: ProxyPolicy[];
  profiles?: Profile[];
  launchParameters?: ReactNode;
  sessionTemplates?: boolean;
  onTarget?: (target: Target) => void;
  onWorkflow?: (workflow: Workflow) => void;
}) {
  const [profileLabels, setProfileLabels] = useState<Record<string, string>>({});
  const patch = (update: Partial<BatchInput>) =>
    onChange({ ...value, ...update });
  const supportsUserAgent = allAdaptersSupport(value.adapters, adapters, "user_agent_override");
  const selectedAdapters = adapters.filter((adapter) => value.adapters.includes(adapter.id));
  const contextAdapters = selectedAdapters.filter((adapter) => adapter.features?.batch_preset_application === "context");
  const operatorAdapters = selectedAdapters.filter((adapter) => adapter.features?.workflow_control === "operator");
  const entitlementAdapters = selectedAdapters.filter((adapter) => adapter.features?.persistent_profiles === "requires_entitlement");
  const selectedTarget = targets.find((target) => target.id === value.target_id);
  const selectedPolicy = policies.find((policy) => policy.id === value.proxy_policy_id);
  const eligiblePolicies = policies.filter((policy) =>
    (value.session_template_id && policy === selectedPolicy) || policyMatchesTarget(policy, selectedTarget),
  );
  return (
    <div className="stack compact-stack">
      {sessionTemplates && <SessionTemplates token={token} value={value} onChange={onChange} />}
      <fieldset className="batch-configuration" disabled={Boolean(value.session_template_id)}>
      <div className="batch-basics">
        <Field label="Batch name">
          <input
            required
            maxLength={100}
            value={value.name}
            onChange={(e) => patch({ name: e.target.value })}
          />
        </Field>
        <div className="form-grid">
          <BatchCatalog<Target> token={token} path="/targets" title="Site"
            value={value.target_id} initial={targets} description={(site) => site.origin}
            onSelect={(target) => { if (target) { onTarget?.(target); patch({ target_id: target.id }); } }} />
          <BatchCatalog<Workflow> token={token} path="/workflows" title="Workflow"
            value={value.workflow_id} initial={workflows} description={(workflow) => `Version ${workflow.version}`}
            onSelect={(workflow) => { if (workflow) { onWorkflow?.(workflow); patch({ workflow_id: workflow.id }); } }} />
        </div>
      </div>
      </fieldset>
      {!value.session_template_id && !targets.length && (
        <p className="notice">Add a site in the Sites registry to run a session.</p>
      )}
      {launchParameters}
      <fieldset className="batch-configuration" disabled={Boolean(value.session_template_id)}>
      <fieldset className="choice-fieldset">
        <legend>Browser distribution</legend>
        <p>Sessions are assigned across the selected browser adapters.</p>
        {adapters.length > 0 ? <div className="adapter-options">
          {adapters.map((adapter) => (
            <label
              key={adapter.id}
              className={`adapter-option ${value.adapters.includes(adapter.id) ? "selected" : ""}`}
            >
              <input
                type="checkbox"
                checked={value.adapters.includes(adapter.id)}
                onChange={(event) => {
                  const selected = event.target.checked
                    ? [...value.adapters, adapter.id]
                    : value.adapters.filter((id) => id !== adapter.id);
                  const hasContext = adapters.some((candidate) => selected.includes(candidate.id)
                    && candidate.features?.batch_preset_application === "context");
                  const presets = value.batch_presets;
                  patch({
                    adapters: selected,
                    count: Math.max(value.count, selected.length),
                    batch_presets: presets && !hasContext && presets.locales?.length === 0
                      ? { ...presets, locales: undefined } : presets,
                  });
                }}
              />
              <span className="adapter-icon">
                <Monitor size={19} />
              </span>
              <span>
                <strong>{adapter.name}</strong>
                <small>{adapter.reason || label(adapter.status)}</small>
                {adapter.available && <small>{adapter.worker_capacity == null
                  ? "Worker capacity unavailable"
                  : `${adapter.worker_capacity} worker ${adapter.worker_capacity === 1 ? "slot" : "slots"} reported`}</small>}
              </span>
              <Badge value={adapter.status} />
            </label>
          ))}
        </div> : <p className="muted small-text">Browser inventory has not loaded.</p>}
      </fieldset>
      {operatorAdapters.map((adapter) => (
        <p key={adapter.id} className="muted small-text">{adapter.name} waits for operator handoff. The workflow checks the result after handoff.</p>
      ))}
      <div className="form-grid">
        <Field
          label="Independent sessions"
          hint="Total across selected browsers. Maximum 2,000 sessions; up to 500 active."
        >
          <input
            type="number"
            required
            min={1}
            max={2000}
            value={value.count}
            onChange={(e) => patch({ count: Number(e.target.value) })}
          />
        </Field>
        <Field
          label="Maximum concurrency"
          hint="Admission also enforces project, site, proxy, and license limits."
        >
          <input
            type="number"
            required
            min={1}
            max={Math.min(value.count, 500)}
            value={value.concurrency}
            onChange={(e) => patch({ concurrency: Number(e.target.value) })}
          />
        </Field>
      </div>
      <details className="choice-fieldset">
        <summary>Advanced</summary>
        <BatchPresetFields value={value.batch_presets} seed={value.seed}
          available={supportsBatchPresets(value, adapters)} contextAdapters={contextAdapters.map((adapter) => adapter.name)}
          onChange={patch} />
        <div className="form-grid">
        {!value.batch_presets && <>
          <UserAgentField value={value.user_agent} supported={supportsUserAgent} onChange={(user_agent) => patch({ user_agent })} />
          <DevicePresetField value={value.device_preset} selected={value.adapters} adapters={adapters} onChange={(device_preset) => patch({ device_preset })} />
        </>}
        {!supportsUserAgent && value.user_agent != null && (
          <p className="notice" role="status">Choose Browser default or select only browsers that support User-Agent overrides.</p>
        )}
        <Field label="Initial referrer" hint="Sent on the first navigation.">
          <input
            type="url"
            maxLength={2048}
            value={value.referrer ?? ""}
            onChange={(e) => patch({ referrer: e.target.value || null })}
          />
        </Field>
        <Field label="New profile persistence" hint={entitlementAdapters.length > 0
          ? `Persistent profiles require a license entitlement for ${entitlementAdapters.map((adapter) => adapter.name).join(", ")}.`
          : undefined}>
          <Select
            value={value.persistence}
            onChange={(e) =>
              patch({ persistence: e.target.value as BatchInput["persistence"] })
            }
          >
            <option value="fresh">Fresh · discard browser state</option>
            <option value="persistent">
              Persistent · retain after clean shutdown
            </option>
          </Select>
        </Field>
        <BatchCatalog<ProxyPolicy> token={token} path="/proxy-policies" title="Proxy policy"
          initial={eligiblePolicies} optional emptyLabel="Direct connectivity" value={value.proxy_policy_id ?? ""}
          disabled={(policy) => !policyMatchesTarget(policy, selectedTarget)}
          label={(policy) => `${policy.name} · v${policy.version}`}
          onSelect={(policy) => patch({ proxy_policy_id: policy?.id ?? null })} />
      </div>
      <fieldset className="choice-fieldset">
        <legend>Reuse existing profiles</legend>
        <p>Choose an existing profile for each session, or leave the selection empty to create new profiles. Reused profiles keep their saved persistence.</p>
        <BatchCatalog<Profile> token={token} path="/profiles" title="Reusable profile"
          initial={profiles} value="" optional emptyLabel="Choose a profile to add"
          label={(profile) => `${profile.name} · ${label(profile.adapter)} · ${label(profile.persistence)} · ${profile.status}`}
          disabled={(profile) => profile.status !== "available" || !value.adapters.includes(profile.adapter)
            || (value.profile_ids ?? []).includes(profile.id) || (value.profile_ids?.length ?? 0) >= value.count}
          onSelect={(profile) => {
            if (!profile) return;
            setProfileLabels({ ...profileLabels, [profile.id]: `${profile.name} · ${label(profile.persistence)}` });
            patch({ profile_ids: [...(value.profile_ids ?? []), profile.id] });
          }} />
        {!!value.profile_ids?.length && <ul>{value.profile_ids.map((id) => <li key={id}>
          {profileLabels[id] ?? profiles.find((profile) => profile.id === id)?.name ?? id}{" "}
          <button type="button" className="text-button"
            onClick={() => patch({ profile_ids: value.profile_ids?.filter((profile) => profile !== id) })}>Remove profile</button>
        </li>)}</ul>}
      </fieldset>
      <fieldset className="choice-fieldset">
        <legend>Execution limits</legend>
        <div className="form-grid">
          <Field label="Maximum duration per session (seconds)">
            <input
              type="number"
              min={10}
              max={7200}
              value={value.timeout_seconds ?? 600}
              onChange={(e) =>
                patch({ timeout_seconds: Number(e.target.value) })
              }
            />
          </Field>
          <Field label="HTTP request budget per session" hint="Includes preparation and workflow requests. Each phase is recorded separately.">
            <input type="number" required min={1} max={100000} value={value.request_budget ?? 1000} onChange={(event) => patch({ request_budget: Number(event.target.value) })} />
          </Field>
          <Field label="Transport budget per session (MiB)" hint="Measured browser payload and vendor control traffic. This is an estimate, not the provider billing total.">
            <input type="number" required min={0.0009765625} max={2048} step="any" value={(value.traffic_budget_bytes ?? 104857600) / 1048576} onChange={(event) => patch({ traffic_budget_bytes: Math.round(Number(event.target.value) * 1048576) })} />
          </Field>
        </div>
      </fieldset>
      <div className="form-grid">
        <Field label="Screenshot and console capture">
          <Select
            value={value.artifact_policy ?? (value.session_template_id ? "none" : "all")}
            onChange={(e) =>
              patch({
                artifact_policy: e.target
                  .value as BatchInput["artifact_policy"],
              })
            }
          >
            <option value="none">Off</option>
            <option value="on_failure">Capture on failure</option>
            <option value="all">Capture all sessions</option>
          </Select>
        </Field>
        <Field label="Artifact retention (days)" hint="Applies to session recordings and retained diagnostic artifacts.">
          <input
            type="number"
            required
            min={1}
            max={365}
            value={value.artifact_retention_days ?? 30}
            onChange={(e) =>
              patch({ artifact_retention_days: Number(e.target.value) })
            }
          />
        </Field>
      </div>
      <label className="checkbox-line">
        <input
          type="checkbox"
          checked={value.capture_javascript ?? !value.session_template_id}
          onChange={(event) => patch({ capture_javascript: event.target.checked })}
        />
        Capture JavaScript assets and execution coverage
      </label>
      </details>
      </fieldset>
    </div>
  );
}
export function batchBrowserSettingsValid(value: BatchInput, adapters: Adapter[]): boolean {
  if (value.batch_presets) {
    return supportsBatchPresets(value, adapters) && value.batch_presets.presets.length > 0
      && (value.batch_presets.locales?.length ?? 1) > 0
      && (value.seed == null || (Number.isInteger(value.seed) && value.seed >= 0 && value.seed <= 2147483647));
  }
  return value.adapters.length > 0
    && validUserAgent(value.user_agent)
    && (value.user_agent == null || allAdaptersSupport(value.adapters, adapters, "user_agent_override"))
    && (value.device_preset == null || allAdaptersSupport(value.adapters, adapters, "responsive_viewport"));
}

export function BatchWizard({
  token, targets, workflows, preview, close, onCreated,
}: {
  token: string; targets: Target[]; workflows: Workflow[]; preview?: Workflow | null;
  close: () => void; onCreated: () => void;
}) {
  const query = useQueryClient();
  const [value, setValue] = useState(() => defaultBatch(targets, workflows, preview));
  const [extraTarget, setExtraTarget] = useState<Target>();
  const [extraWorkflow, setExtraWorkflow] = useState<Workflow>();
  const [submitted, setSubmitted] = useState<{ input: BatchInput; draft: string }>();
  const [stale, setStale] = useState(false);
  const selectedTarget = targets.find((item) => item.id === value.target_id) ?? (extraTarget?.id === value.target_id ? extraTarget : undefined);
  const selectedWorkflow = workflows.find((item) => item.id === value.workflow_id) ?? (extraWorkflow?.id === value.workflow_id ? extraWorkflow : undefined);
  const steps = selectedWorkflow?.steps ?? [];
  const parameterValues = value.fixture_params ?? {};
  const { overrides, error: parameterError } = validateWorkflowUrlOverrides(steps, parameterValues);
  const adapters = useQuery({
    queryKey: platformKey(token, "adapters"),
    queryFn: () => api<Adapter[]>("/adapters", token),
  });
  const policies = useQuery({
    queryKey: platformKey(token, "proxy-policies"),
    queryFn: () => api<PageResult<ProxyPolicy>>("/proxy-policies?limit=30", token),
  });
  const profiles = useQuery({
    queryKey: platformKey(token, "profile-options"),
    queryFn: () => api<PageResult<Profile>>("/profiles?limit=30", token),
  });
  const input = {
    ...value, fixture_params: Object.keys(overrides).length ? overrides : undefined,
    expected_workflow_version: selectedWorkflow?.version,
  };
  const draft = JSON.stringify(input);
  const valid = !parameterError && Boolean(selectedTarget && selectedWorkflow)
    && batchBrowserSettingsValid(value, adapters.data ?? []) && value.count >= 1
    && value.concurrency >= 1 && value.concurrency <= value.count
    && (!value.profile_ids?.length || value.profile_ids.length === value.count);
  const readiness = useBatchPreflight(token, input, valid);
  const create = useMutation({
    mutationFn: (requestBody: BatchInput) => api<Batch>("/batches", token, { method: "POST", body: requestBody }),
    onSuccess: () => {
      void query.invalidateQueries({ queryKey: ["platform", token] });
      onCreated();
      close();
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status < 500) {
        setSubmitted(undefined);
        if (error.status === 409) {
          setStale(true);
          void query.invalidateQueries({ queryKey: ["workflows", token] });
          void query.invalidateQueries({ queryKey: ["targets", token] });
          void readiness.refetch();
        }
      }
    },
  });
  function change(next: BatchInput) {
    if (next.workflow_id !== value.workflow_id) {
      next = {
        ...next, fixture_params: undefined, workflow_inputs: undefined,
      };
    }
    setValue(next);
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid || !readiness.current || !readiness.data || readiness.error || stale || submitted) return;
    const payload = { ...input, expected_dependencies: readiness.data.expected_dependencies };
    setSubmitted({ input: payload, draft });
    create.mutate(payload);
  }
  return <Modal title={preview === undefined ? "Create session batch" : "Preview workflow"}
    detail={preview === undefined ? "Choose a site and workflow, then set how many sessions to run." : "Run this workflow once on the selected site."} close={close} wide>
    <form onSubmit={submit}>
      <div className="modal-body stack">
        <BatchFields token={token} targets={extraTarget ? [...targets, ...(!targets.some((item) => item.id === extraTarget.id) ? [extraTarget] : [])] : targets}
          workflows={extraWorkflow ? [...workflows, ...(!workflows.some((item) => item.id === extraWorkflow.id) ? [extraWorkflow] : [])] : workflows}
          value={value} onChange={change} onTarget={setExtraTarget} onWorkflow={setExtraWorkflow}
          adapters={adapters.data ?? []} policies={policies.data?.items ?? []} profiles={profiles.data?.items ?? []}
          launchParameters={<>
            <WorkflowValuesFields workflow={selectedWorkflow}
              value={value.workflow_inputs ?? {}} onChange={(workflow_inputs) => change({ ...value, workflow_inputs })} />
            {workflowUrlParameterNames(steps).length > 0 && <WorkflowUrlParameters steps={steps}
              value={parameterValues} origin={selectedTarget?.origin}
              onChange={(fixture_params) => change({ ...value, fixture_params })} />}
            <ErrorNotice error={parameterError} />
          </>} />
        <fieldset className="choice-fieldset">
          <legend>Launch readiness</legend>
          <p>{value.count} {value.count === 1 ? "session" : "sessions"} · {value.concurrency} maximum concurrent</p>
          {readiness.current && readiness.isFetching && <p role="status">Checking configuration…</p>}
          {readiness.current && <ErrorNotice error={readiness.error} />}
          {readiness.current && readiness.data && !readiness.error && <>
            <p>Site v{readiness.data.dependencies.target_version} · Workflow v{readiness.data.dependencies.workflow_version}</p>
            <BatchReadinessNotices notices={readiness.data.notices} />
          </>}
          {stale && <button className="button secondary" type="button" onClick={() => { setStale(false); void readiness.refetch(); }}>Refresh launch readiness</button>}
        </fieldset>
        {submitted && create.error && <>
          <p className="notice">The application did not receive a launch confirmation. Retry the same request to recover its receipt.</p>
          <div className="button-row">
            <button type="button" className="button secondary" disabled={create.isPending} onClick={() => create.mutate(submitted.input)}>Retry original launch</button>
            {draft !== submitted.draft && <button type="button" className="button secondary" onClick={() => {
              setSubmitted(undefined);
              setValue({ ...value, idempotency_key: crypto.randomUUID() });
              create.reset();
            }}>Use revised settings</button>}
          </div>
        </>}
        <ErrorNotice error={create.error} />
      </div>
      <div className="modal-footer">
        <button type="button" className="button secondary" onClick={close}>Cancel</button>
        <button type="submit" className="button primary" disabled={!valid || !readiness.current || !readiness.data || Boolean(readiness.error) || stale || !!submitted}>
          {create.isPending ? "Creating…" : "Create batch"}
        </button>
      </div>
    </form>
  </Modal>;
}

function SessionViewer({
  token,
  session,
}: {
  token: string;
  session: BrowserSession;
}) {
  const [text, setText] = useState("");
  const [owned, setOwned] = useState(false);
  const query = useQueryClient();
  const command = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api(`/sessions/${session.id}/commands`, token, {
        method: "POST",
        body: { ...body, idempotency_key: crypto.randomUUID() },
      }),
    onSuccess: (_data, body) => {
      if (body.command === "claim_control") setOwned(true);
      if (["release_control", "continue"].includes(String(body.command)))
        setOwned(false);
      void query.invalidateQueries({
        queryKey: platformKey(token, "sessions"),
      });
    },
  });
  const frame = useQuery({
    queryKey: platformKey(token, "frame", session.id, session.epoch),
    queryFn: async () => {
      const response = await request(`/sessions/${session.id}/frame`, token);
      const blob = await response.blob();
      return await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      });
    },
    refetchInterval: owned ? 1000 : 5000,
    retry: false,
    gcTime: 0,
  });
  return (
    <section className="viewer">
      <div className="viewer-toolbar">
        <span>
          <MousePointer2 size={16} />
          {owned ? "You control this browser" : "Manual browser viewer"}
        </span>
        <button
          className="button secondary small"
          disabled={command.isPending}
          onClick={() =>
            command.mutate({
              command: owned ? "release_control" : "claim_control",
            })
          }
        >
          {owned ? "Release control" : "Take control"}
        </button>
      </div>
      <div className={`viewer-frame ${owned ? "interactive" : ""}`}>
        {/* The frame is authenticated binary data, never a public browser endpoint. */}
        {frame.data ? (
          // Authenticated transient frames must bypass public image optimization.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={frame.data}
            alt="Live browser session"
            onClick={(e) => {
              if (!owned) return;
              const rect = e.currentTarget.getBoundingClientRect();
              command.mutate({
                command: "click",
                x: Math.round(
                  ((e.clientX - rect.left) * e.currentTarget.naturalWidth) /
                    rect.width,
                ),
                y: Math.round(
                  ((e.clientY - rect.top) * e.currentTarget.naturalHeight) /
                    rect.height,
                ),
              });
            }}
          />
        ) : (
          <Empty
            title="Waiting for a browser frame"
            detail="The viewer becomes available when the session is ready for operator handoff."
          />
        )}
      </div>
      {owned && (
        <>
          <div className="viewer-input">
            <input
              aria-label="Text to enter in browser"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Type into the focused browser field"
            />
            <button
              className="button secondary small"
              disabled={!text || command.isPending}
              onClick={() => command.mutate({ command: "type", text })}
            >
              Type
            </button>
            <button
              className="button secondary small"
              onClick={() => command.mutate({ command: "key", key: "Enter" })}
            >
              Enter
            </button>
          </div>
          <div className="actions">
            <button
              className="button secondary small"
              onClick={() =>
                command.mutate({ command: "scroll", delta_y: 480 })
              }
            >
              Scroll down
            </button>
            <button
              className="button secondary small"
              onClick={() =>
                command.mutate({ command: "scroll", delta_y: -480 })
              }
            >
              Scroll up
            </button>
            <button
              className="button primary small"
              disabled={command.isPending}
              onClick={() => command.mutate({ command: "continue" })}
            >
              <Play size={14} />
              Hand back to workflow
            </button>
          </div>
        </>
      )}
      <ErrorNotice error={command.error} />
      {frame.error && (
        <p className="muted small-text">
          Frame unavailable:{" "}
          {frame.error instanceof Error
            ? frame.error.message
            : "No frame returned."}
        </p>
      )}
    </section>
  );
}
function SessionDetail({
  token,
  session,
  onPlayRecording,
  close,
}: {
  token: string;
  session: BrowserSession;
  onPlayRecording: (id: string) => void;
  close: () => void;
}) {
  const query = useQueryClient();
  const preset = session.batch_preset;
  const viewportChecks = viewportConformance(session);
  const currentViewport = !preset || viewportChecks !== undefined
    || (session.attempt_number > 0 && session.metadata.environment_epoch === session.epoch);
  const device = currentViewport ? devicePresetLabel(session.metadata.device_preset) : undefined;
  const deviceMode = currentViewport ? deviceModeLabel(session.metadata.device_mode) : undefined;
  const deviceViewport = currentViewport ? session.metadata.viewport as { width?: number; height?: number } | undefined : undefined;
  const viewportSize = typeof deviceViewport?.width === "number" && typeof deviceViewport.height === "number"
    ? `${deviceViewport.width} × ${deviceViewport.height}`
    : undefined;
  const browserEngine = [session.metadata.browser_engine, session.metadata.browser_version]
    .filter((entry): entry is string => typeof entry === "string")
    .join(" · ");
  const requestUsage = session.metadata.request_usage as { total: number; budget: number; by_phase: Record<string, number>; exhausted: boolean } | undefined;
  const transportUsage = session.metadata.transport_usage as { total_bytes: number; budget_consumed_bytes: number; limit_bytes: number; budget_exhausted: boolean } | undefined;
  const javascriptCapture = session.metadata.javascript_capture;
  const browserDiagnostic = session.metadata.browser_diagnostic as Record<string, unknown> | undefined;
  const isDiagnostic = browserDiagnostic?.attempt === session.attempt_number
    && browserDiagnostic.epoch === session.epoch;
  const javascriptCaptureStatus =
    typeof javascriptCapture === "object" &&
    javascriptCapture !== null &&
    "attempt_number" in javascriptCapture &&
    javascriptCapture.attempt_number === session.attempt_number &&
    "status" in javascriptCapture &&
    typeof javascriptCapture.status === "string"
      ? javascriptCapture.status
      : undefined;
  const [viewer, setViewer] = useState(false);
  const canOpenViewer = session.status === "awaiting_operator";
  const active = sessionActive(session.status);
  const adscoreMeasurements = Array.isArray(session.metadata.adscore_requests)
    ? (session.metadata.adscore_requests as AdScoreMeasurementRequest[]).filter((row) => row.method === "POST")
    : [];
  const adscoreReferrerPolicies = Array.isArray(session.metadata.adscore_referrer_policies)
    ? session.metadata.adscore_referrer_policies as AdScoreReferrerPolicy[]
    : [];
  const adscoreInitiators = Array.isArray(session.metadata.adscore_request_initiators)
    ? session.metadata.adscore_request_initiators as AdScoreRequestInitiator[]
    : [];
  const visibleWarning = session.metadata.visible_assertion_warning as { step?: number; codes?: unknown[] } | undefined;
  const visibleWarningCodes = Array.isArray(visibleWarning?.codes)
    ? visibleWarning.codes.filter((code) => code === "ADSCORE_SCRIPT_RULE" || code === "OFFER_TARGETING_MISMATCH")
    : [];
  const adscoreRequestFailures = Array.isArray(session.metadata.adscore_request_failures)
    ? session.metadata.adscore_request_failures as AdScoreRequestFailure[]
    : [];
  const omittedAdScoreFailures = session.metadata.adscore_request_failures_omitted;
  const stop = useMutation({
    mutationFn: () =>
      api(`/sessions/${session.id}/commands`, token, {
        method: "POST",
        body: { command: "stop", idempotency_key: crypto.randomUUID() },
      }),
    onSuccess: () =>
      void query.invalidateQueries({
        queryKey: platformKey(token, "sessions"),
      }),
  });
  const events = useQuery({
    queryKey: platformKey(token, "session-events", session.id, session.attempt_number, session.finished_at),
    queryFn: () =>
      api<
        PageResult<{
          sequence: number;
          kind: string;
          created_at: string;
          detail?: Record<string, unknown>;
        }>
      >(`/sessions/${session.id}/events`, token),
    refetchInterval: sessionActive(session.status) ? 5000 : false,
  });
  const artifacts = useQuery({
    queryKey: platformKey(token, "artifacts", session.id, session.attempt_number, session.finished_at),
    queryFn: () =>
      api<
        Array<{
          id: string;
          name: string;
          media_type: string;
          size: number;
          checksum: string;
          expires_at: string;
        }>
      >(`/sessions/${session.id}/artifacts`, token),
    refetchInterval: sessionActive(session.status) ? 5000 : false,
  });
  const download = useMutation({
    mutationFn: async ({ id, name }: { id: string; name: string }) => {
      const response = await request(`/artifacts/${id}`, token);
      saveFile(await response.blob(), name);
    },
  });
  const observations = useQuery({
    queryKey: platformKey(token, "observations", session.id),
    queryFn: () =>
      api<
        Array<{
          id: string;
          verification_status: string;
          verdict: string | null;
          raw_code: number | null;
          accepted: boolean;
          source: string;
          provider_version: string | null;
          created_at: string;
        }>
      >(`/sessions/${session.id}/observations`, token),
    refetchInterval: sessionActive(session.status) ? 5000 : false,
  });
  return (
    <aside className="detail-drawer" aria-label="Session details">
      <div className="drawer-heading">
        <div>
          <span className="eyebrow">SESSION</span>
          <h2 className="mono">{session.id.slice(0, 12)}</h2>
        </div>
        <button
          className="icon-button"
          onClick={close}
          aria-label="Close session details"
        >
          <X size={20} />
        </button>
      </div>
      <div className="drawer-body stack">
        <div className="actions">
          <Badge value={session.status} />
          <Badge value={session.mode} />
          <DiagnosticBundleExport key={session.id} token={token} sessionId={session.id} />
        </div>
        <DetailGrid
          items={[
            ["Workflow", sessionWorkflowLabel(session)],
            ["Browser", session.adapter],
            ["Browser engine", browserEngine || "Unrecorded"],
            ["Device", [device, deviceMode, viewportSize].filter(Boolean).join(" · ") || "Unrecorded"],
            ["Profile", typeof session.metadata.profile_launch_state === "string" && session.metadata.profile_launch_state !== session.persistence
              ? `${label(session.persistence)} · ${label(session.metadata.profile_launch_state)}` : label(session.persistence)],
            [
              "Observed IP",
              session.observed_ip ? <span className="mono session-ip-value">{session.observed_ip}</span>
                : isDiagnostic ? "Not retained" : "Unavailable",
            ],
            [
              "Proxy Check IP",
              session.proxy_check_ip ? <span className="mono session-ip-value">{session.proxy_check_ip}</span>
                : isDiagnostic ? "Not retained" : "Unavailable",
            ],
            [
              "Operational outcome",
              session.outcome ? label(session.outcome) : "Pending",
            ],
            [
              "Workflow outcome",
              session.workflow_outcome
                ? label(session.workflow_outcome)
                : "Unrecorded",
            ],
            ["Duration", ms(session.elapsed_ms)],
            ...(session.queue_reason ? [["Queue", queueReason(session.queue_reason)] as [string, string | null]] : []),
            ...(session.next_attempt_at ? [["Next retry", dateTime(session.next_attempt_at)] as [string, string]] : []),
            ...(typeof session.metadata.completion_status === "string"
              ? [["Completion check", label(session.metadata.completion_status)] as [string, string]] : []),
            ["Ground truth", label(session.ground_truth ?? "unrecorded")],
            ["Created", dateTime(session.created_at)],
            [
              "Environment epoch",
              session.epoch ?? session.environment_epoch ?? 1,
            ],
          ]}
        />
        {preset && <section>
          <h3>Batch preset assignment</h3>
          <DetailGrid items={[
            ["Preset", `${preset.id} · catalog v${preset.catalog_version}`],
            ["Assigned settings", preset.application === "viewport_only" ? "Viewport only" : "Viewport, DPR, locale, and timezone"],
            ["Browser device class", label(preset.device_class)],
            ["Viewport", `${label(preset.viewport_class)} · ${preset.viewport.width} × ${preset.viewport.height}`],
            ["Device pixel ratio", preset.device_pixel_ratio ?? "Adapter default"],
            ["Expected locale and timezone", preset.locale && preset.timezone ? `${preset.locale} · ${preset.timezone}` : "Adapter defaults"],
            ["Batch distribution", label(preset.mode)],
            ["Batch seed", preset.seed],
          ]} />
        </section>}
        {preset && <section>
          <h3>Viewport conformance</h3>
          <p>{viewportChecks
            ? `${viewportChecks.matched} of ${viewportChecks.checks} viewport checks matched · Attempt ${session.attempt_number}`
            : active ? "Awaiting viewport checks" : "No viewport checks recorded for this attempt"}</p>
        </section>}
        {!isDiagnostic && <p className="muted small-text">{proxyCheckIPHint}</p>}
        {session.error && <ErrorNotice error={session.error} />}
        <DecodoPreflight report={session.metadata.proxy_preflight} attempt={session.attempt_number} />
        {session.metadata.environment_epoch === session.epoch && (
          <LifecycleTimeline value={session.metadata.lifecycle_timeline} />
        )}
        {isDiagnostic && <BrowserDiagnostic data={browserDiagnostic} attempt={session.attempt_number} />}
        {session.metadata.environment_epoch === session.epoch && (
          <MainOriginFailures counts={session.metadata.main_origin_subresource_failures} />
        )}
        {!!session.metadata.step_failure && <WorkflowFailure failure={session.metadata.step_failure} />}
        {session.metadata.environment_epoch === session.epoch && (
          <NavigationConnects value={session.metadata.navigation_connects} />
        )}
        {typeof visibleWarning?.step === "number" && visibleWarningCodes.length > 0 && (
          <DetailGrid items={[[
            `Visible warning at step ${visibleWarning.step}`,
            <span key="warning-codes" className="mono">{visibleWarningCodes.join(", ")}</span>,
          ]]} />
        )}
        {(requestUsage || transportUsage) && <section>
          <h3>Execution budgets</h3>
          <DetailGrid items={[
            ["HTTP requests", requestUsage ? `${requestUsage.total} / ${requestUsage.budget}${requestUsage.exhausted ? " · exhausted" : ""}` : "Unmeasured"],
            ["Requests by phase", requestUsage ? Object.entries(requestUsage.by_phase).map(([phase, count]) => `${label(phase)} ${count}`).join(" · ") : "Unmeasured"],
            ["Transport budget", transportUsage ? `${(transportUsage.budget_consumed_bytes / 1048576).toFixed(2)} / ${(transportUsage.limit_bytes / 1048576).toFixed(2)} MiB${transportUsage.budget_exhausted ? " · exhausted" : ""}` : "Unmeasured"],
            ["Browser payload", transportUsage ? `${(transportUsage.total_bytes / 1048576).toFixed(2)} MiB` : "Unmeasured"],
          ]} />
          <p className="muted small-text">HTTP requests include preparation and workflow, separately from network tunnels. Transport includes encrypted browser payload; the budget also includes vendor control traffic. Provider billing may include additional overhead.</p>
        </section>}
        {(canOpenViewer || active) && (
          <div className="actions">
            {canOpenViewer && (
              <button
                className="button secondary"
                onClick={() => setViewer(!viewer)}
              >
                <Monitor size={16} />
                {viewer ? "Hide viewer" : "Open viewer"}
              </button>
            )}
            {active && (
              <button
                className="button danger"
                disabled={stop.isPending}
                onClick={() => stop.mutate()}
              >
                <Square size={14} />
                Stop session
              </button>
            )}
          </div>
        )}
        {!canOpenViewer && active && session.adapter === "manual_chrome" && (
          <p className="muted small-text">
            The live viewer opens when the session is ready for operator handoff.
          </p>
        )}
        <ErrorNotice error={stop.error} />
        {canOpenViewer && viewer && (
          <SessionViewer key={session.epoch} token={token} session={session} />
        )}
        <section>
          <h3>Provider observations</h3>
          <p className="muted small-text">
            Verification, provider verdict, and action acceptance are recorded
            independently from workflow completion and ground truth.
          </p>
          <ErrorNotice error={observations.error} />
          {observations.data?.length ? (
            <div className="observation-list">
              {observations.data.map((item) => (
                <div key={item.id}>
                  <div className="actions">
                    <Badge value={item.verification_status} />
                    <Badge
                      value={
                        item.verification_status !== "verified"
                          ? "verdict unavailable"
                          : item.raw_code === 0
                            ? "Clean"
                            : (item.verdict ?? "verdict unavailable")
                      }
                    />
                  </div>
                  <DetailGrid
                    items={[
                      ["Raw vendor code", item.raw_code ?? "Unavailable"],
                      [
                        "Acceptance decision",
                        item.accepted ? "Accepted" : "Not accepted",
                      ],
                      ["Observation source", label(item.source)],
                      [
                        "Provider version",
                        item.provider_version ?? "Unrecorded",
                      ],
                    ]}
                  />
                </div>
              ))}
            </div>
          ) : (
            <p className="muted small-text">
              No provider observations are recorded for this session.
            </p>
          )}
        </section>
        {!active && adscoreMeasurements.length > 0 && (
          <section>
            <h3>AdScore measurement requests</h3>
            <div className="artifact-list">
              {adscoreMeasurements.map((request, index) => (
                <div key={`${request.url}-${index}`}>
                  <div>
                    <strong>{request.status === undefined ? request.error ?? "No HTTP response" : `HTTP ${request.status}`}</strong>
                    <small className="mono">{request.method} {request.url}</small>
                    <small>
                      Browser referrer origin{" "}
                      <span className="mono">
                        {request.referrer_origin === null ? "None sent" : request.referrer_origin ?? "Not captured"}
                      </span>
                    </small>
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}
        {!active && adscoreReferrerPolicies.length > 0 && (
          <section>
            <h3>AdScore referrer policies</h3>
            <div className="artifact-list">
              {adscoreReferrerPolicies.map((policy, index) => (
                <div key={`${policy.url}-${index}`}>
                  <div>
                    <small className="mono">POST {policy.url}</small>
                    <small>Browser request policy {policy.request_policy ?? "Not captured"}</small>
                    <small>
                      Page response policy {policy.document_policy === null ? "No recognized header" : policy.document_policy ?? "Not captured"}
                    </small>
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}
        {!active && adscoreInitiators.length > 0 && (
          <section>
            <h3>AdScore request initiators</h3>
            <div className="artifact-list">
              {adscoreInitiators.map((entry, index) => (
                <div key={`${entry.url}-${index}`}>
                  <div>
                    <small className="mono">POST {entry.url}{entry.status === undefined ? "" : ` · HTTP ${entry.status}`}</small>
                    <small>
                      Initiator {entry.initiator_type ? label(entry.initiator_type) : "Not captured"}
                      {entry.resource_type && ` · ${label(entry.resource_type)}`}
                      {entry.context && ` · ${label(entry.context)}`}
                    </small>
                    {entry.initiator_origin && <small>Initiator origin {entry.initiator_origin}</small>}
                    {entry.document_origin && <small>Document origin {entry.document_origin}</small>}
                    <small>Referer in initial browser headers {headerPresence(entry.initial_referrer_present)}</small>
                    {entry.redirected ? (
                      <small>Browser dispatch and network headers could not be matched across redirects.</small>
                    ) : (
                      <>
                        <small>Referer before forwarding {headerPresence(entry.paused_referrer_present)}</small>
                        <small>Referer in network headers {headerPresence(entry.network_referrer_present)}</small>
                      </>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}
        {!active && adscoreRequestFailures.length > 0 && (
          <section>
            <h3>AdScore request failures</h3>
            <div className="artifact-list">
              {adscoreRequestFailures.map((failure, index) => (
                <div key={`${failure.url}-${failure.kind}-${index}`}>
                  <div>
                    <strong>{adscoreRequestFailureLabels[failure.kind]}</strong>
                    <small className="mono">{failure.method} {failure.url}</small>
                    <small>
                      {[
                        failure.status ? `HTTP ${failure.status}` : failure.error,
                        failure.resource_type && label(failure.resource_type),
                        failure.phase === "workflow" && failure.step > 0
                          ? `Step ${failure.step}`
                          : label(failure.phase),
                      ].filter(Boolean).join(" · ")}
                    </small>
                  </div>
                </div>
              ))}
            </div>
            {typeof omittedAdScoreFailures === "number" && omittedAdScoreFailures > 0 && (
              <p className="muted small-text">Additional failures exceeded the capture limit.</p>
            )}
          </section>
        )}
        <section>
          <h3>Session recording</h3>
          <RecordingAction session={session} onPlay={onPlayRecording} />
        </section>
        <section>
          <h3>Retained artifacts</h3>
          {javascriptCaptureStatus && (
            <div className="actions">
              <span>JavaScript capture</span>
              <Badge value={javascriptCaptureStatus} />
            </div>
          )}
          <ErrorNotice error={artifacts.error || download.error} />
          {artifacts.data?.length ? (
            <div className="artifact-list">
              {artifacts.data.map((item) => (
                <div key={item.id}>
                  <div>
                    <strong>{item.name}</strong>
                    <small>
                      {(item.size / 1024).toFixed(1)} KB · expires{" "}
                      {dateTime(item.expires_at)}
                    </small>
                    <small className="mono">
                      SHA-256 {item.checksum.slice(0, 16)}…
                    </small>
                  </div>
                  <button
                    className="icon-button"
                    aria-label={`Download ${item.name}`}
                    disabled={download.isPending}
                    onClick={() => download.mutate(item)}
                  >
                    <Download size={16} />
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted small-text">
              No retained artifacts.
            </p>
          )}
        </section>
        <section>
          <h3>Activity timeline</h3>
          {events.isPending ? (
            <Loading />
          ) : events.error ? (
            <ErrorNotice error={events.error} />
          ) : events.data?.items.length ? (
            <ol className="activity-timeline">
              {events.data.items.map((event) => (
                <li key={event.sequence}>
                  <span className="timeline-dot" />
                  <div>
                    <strong>{sessionEventTitle(event.kind, event.detail)}</strong>
                    <small>{dateTime(event.created_at)}</small>
                    {event.detail && <p>{sessionEventDetail(event.kind, event.detail)}</p>}
                    {event.kind === "proxy_preflight" && event.detail && event.detail.attempt !== session.attempt_number && (
                      <details>
                        <summary>Check results and timings</summary>
                        <pre className="json-preview">{JSON.stringify({
                          checks: event.detail.checks, timings_ms: event.detail.timings_ms,
                        }, null, 2)}</pre>
                      </details>
                    )}
                    {event.kind === "session_attempt_finished" && !!event.detail?.failure && (
                      <WorkflowFailure failure={event.detail.failure} />
                    )}
                  </div>
                </li>
              ))}
            </ol>
          ) : (
            <p className="muted">No recorded events yet.</p>
          )}
        </section>
      </div>
    </aside>
  );
}
function SessionList({
  sessions,
  selected,
  onOpen,
  onPlayRecording,
  onSelect,
  selection,
}: {
  sessions: BrowserSession[];
  selected: string | null;
  onOpen: (id: string) => void;
  onPlayRecording: (id: string) => void;
  onSelect: (id: string) => void;
  selection: Set<string>;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const getSessionKey = useCallback((index: number) => sessions[index].id, [sessions]);
  // The virtualizer remains outside React Compiler memoization; its scroll state is owned by TanStack.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: sessions.length,
    getScrollElement: () => ref.current,
    getItemKey: getSessionKey,
    estimateSize: () => 72,
    overscan: 6,
  });
  return (
    <>
      <div className="session-table-head">
        <span />
        <span>Session / browser</span>
        <span className="session-workflow-column">Workflow</span>
        <span>Status</span>
        <span className="session-profile">Profile</span>
        <span className="session-evidence">Evidence</span>
        <span className="session-ip-column" title="Address seen by the observation receiver">Observed IP</span>
        <span className="session-ip-column" title={proxyCheckIPHint}>Proxy Check IP</span>
        <span className="session-duration">Duration</span>
        <span>Recording</span>
      </div>
      <div
        ref={ref}
        className="session-virtual"
        style={{ height: Math.min(540, Math.max(150, virtualizer.getTotalSize())) }}
        role="list"
        aria-label="Browser sessions"
      >
        <div
          style={{ height: virtualizer.getTotalSize(), position: "relative" }}
        >
          {virtualizer.getVirtualItems().map((item) => {
            const session = sessions[item.index];
            const workflow = sessionWorkflowLabel(session);
            const observedIP = session.observed_ip ?? "Unavailable";
            const proxyCheckIP = session.proxy_check_ip ?? "Unavailable";
            return (
              <div
                key={session.id}
                data-index={item.index}
                ref={virtualizer.measureElement}
                role="listitem"
                className={`session-row ${selected === session.id ? "selected" : ""}`}
                style={{
                  transform: `translateY(${item.start}px)`,
                }}
              >
                <input
                  type="checkbox"
                  aria-label={`Select session ${session.id}`}
                  checked={selection.has(session.id)}
                  onChange={() => onSelect(session.id)}
                />
                <button
                  className="session-identity"
                  onClick={() => onOpen(session.id)}
                >
                  <span className="session-avatar">
                    <Monitor size={17} />
                  </span>
                  <span className="session-identity-text">
                    <strong>{session.adapter}</strong>
                    <small className="mono">{session.id.slice(0, 12)}</small>
                    <small className="session-workflow-inline session-workflow-value">
                      Workflow {workflow}
                    </small>
                    <small className="session-ip-inline">
                      Observed IP <span className="mono session-ip-value">{observedIP}</span>
                    </small>
                    <small className="session-ip-inline" title={proxyCheckIPHint}>
                      Proxy Check IP <span className="mono session-ip-value">{proxyCheckIP}</span>
                    </small>
                  </span>
                </button>
                <span className="session-workflow-column session-workflow-value">
                  <span className="sr-only">Workflow </span>
                  {workflow}
                </span>
                <span title={queueReason(session.queue_reason) ?? undefined}><Badge value={session.status} /></span>
                <span className="session-profile muted">{label(session.persistence)}</span>
                <span className="session-evidence"><Badge value={session.mode} /></span>
                <span className="session-ip-column session-ip-value mono muted" title={observedIP}>
                  <span className="sr-only">Observed IP </span>
                  <span>{observedIP}</span>
                </span>
                <span className="session-ip-column session-ip-value mono muted" title={proxyCheckIP}>
                  <span className="sr-only">Proxy Check IP </span>
                  <span>{proxyCheckIP}</span>
                </span>
                <span className="session-duration mono muted">{ms(session.elapsed_ms)}</span>
                <div className="session-row-actions">
                  <RecordingAction session={session} onPlay={onPlayRecording} compact />
                  <button
                    className="icon-button"
                    aria-label={`Open session ${session.id}`}
                    onClick={() => onOpen(session.id)}
                  >
                    <ChevronRight size={17} />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}
function ProfileHistory({
  token,
  profile,
  close,
}: {
  token: string;
  profile: Profile;
  close: () => void;
}) {
  const query = useQueryClient();
  const [evidence, setEvidence] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const snapshots = useQuery({
    queryKey: platformKey(token, "snapshots", profile.id),
    queryFn: () =>
      api<
        PageResult<{
          id: string;
          generation: number;
          checksum: string;
          environment: unknown;
          created_at: string;
        }>
      >(`/profiles/${profile.id}/snapshots`, token),
  });
  const action = useMutation({
    mutationFn: ({
      command,
      generation,
    }: {
      command: string;
      generation?: number;
    }) =>
      api(`/profiles/${profile.id}/${command}`, token, {
        method: "POST",
        body: {
          generation,
          idempotency_key: crypto.randomUUID(),
          ...(command === "recover"
            ? {
                confirmed_stopped: confirmed,
                ownership_epoch: profile.epoch,
                evidence,
              }
            : {}),
        },
      }),
    onSuccess: () => {
      void query.invalidateQueries({
        queryKey: platformKey(token, "profiles"),
      });
      close();
    },
  });
  const available = profile.status === "available" && !profile.owner_session_id;
  return (
    <Modal
      title={profile.name}
      detail="Profile generations and exclusive ownership"
      close={close}
    >
      <div className="modal-body stack">
        <DetailGrid
          items={[
            ["Adapter", profile.adapter],
            ["Status", <Badge key="status" value={profile.status} />],
            ["Generation", profile.version],
            ["Ownership epoch", profile.epoch ?? profile.ownership_epoch ?? 0],
            ["Profile ID", <code key="id">{profile.id}</code>],
            ["Owner", profile.owner_session_id ?? "Unassigned"],
          ]}
        />
        <h3>Encrypted snapshots</h3>
        <ErrorNotice error={snapshots.error || action.error} />
        {snapshots.isPending ? (
          <Loading />
        ) : snapshots.data?.items.length ? (
          <ol className="activity-timeline">
            {snapshots.data.items.map((snapshot) => (
              <li key={snapshot.id}>
                <span className="timeline-dot" />
                <div>
                  <strong>Generation {snapshot.generation}</strong>
                  <small>{dateTime(snapshot.created_at)}</small>
                  <p className="mono">
                    SHA-256 {snapshot.checksum.slice(0, 20)}…
                  </p>
                  <details className="environment-details">
                    <summary>Compatibility environment</summary>
                    <pre>{JSON.stringify(snapshot.environment, null, 2)}</pre>
                  </details>
                  <button
                    className="button secondary small"
                    disabled={!available || action.isPending}
                    onClick={() =>
                      action.mutate({
                        command: "restore",
                        generation: snapshot.generation,
                      })
                    }
                  >
                    Restore this generation
                  </button>
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted">No clean-shutdown snapshot is recorded.</p>
        )}
        {profile.status === "quarantined" && (
          <fieldset className="choice-fieldset">
            <legend>Recover confirmed stopped runtime</legend>
            <Field
              label="Runtime termination evidence"
              hint="Describe how you confirmed the previous browser process stopped and its network route closed. Enter at least 20 characters."
            >
              <textarea
                minLength={20}
                value={evidence}
                onChange={(e) => setEvidence(e.target.value)}
                rows={3}
              />
            </Field>
            <label className="checkbox-line">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              <span>
                I confirmed the previous browser runtime and its network route
                are stopped.
              </span>
            </label>
            <button
              className="button secondary"
              disabled={
                !confirmed || evidence.trim().length < 20 || action.isPending
              }
              onClick={() => action.mutate({ command: "recover" })}
            >
              Recover profile ownership
            </button>
          </fieldset>
        )}
        <p className="notice">
          Restore and reset require a stopped, available profile. Quarantined
          profiles require runtime reconciliation.
        </p>
      </div>
      <div className="modal-footer">
        <button className="button secondary" onClick={close}>
          Close
        </button>
        <button
          className="button danger"
          disabled={!available || action.isPending}
          onClick={() => action.mutate({ command: "reset" })}
        >
          Reset browser state
        </button>
      </div>
    </Modal>
  );
}
function ProfileForm({ token, close }: { token: string; close: () => void }) {
  const query = useQueryClient();
  const [name, setName] = useState("");
  const [adapter, setAdapter] = useState("playwright");
  const [persistence, setPersistence] = useState("persistent");
  const adapters = useQuery({
    queryKey: platformKey(token, "adapters"),
    queryFn: () => api<Adapter[]>("/adapters", token),
  });
  const create = useMutation({
    mutationFn: () =>
      api("/profiles", token, {
        method: "POST",
        body: { name, adapter, persistence },
      }),
    onSuccess: () => {
      void query.invalidateQueries({
        queryKey: platformKey(token, "profiles"),
      });
      close();
    },
  });
  return (
    <Modal
      title="Create browser profile"
      detail="A durable state container with one active writer."
      close={close}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <div className="modal-body stack">
          <Field label="Profile name">
            <input
              required
              maxLength={100}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field label="Browser adapter">
            <Select
              value={adapter}
              onChange={(e) => setAdapter(e.target.value)}
            >
              {adapters.data?.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Persistence">
            <Select
              value={persistence}
              onChange={(e) => setPersistence(e.target.value)}
            >
              <option value="persistent">Persistent</option>
              <option value="fresh">Fresh</option>
            </Select>
          </Field>
          <ErrorNotice error={create.error || adapters.error} />
        </div>
        <div className="modal-footer">
          <button type="button" className="button secondary" onClick={close}>
            Cancel
          </button>
          <button className="button primary" disabled={create.isPending}>
            Create profile
          </button>
        </div>
      </form>
    </Modal>
  );
}
function ProfileList({ token }: { token: string }) {
  const listing = useListState();
  const { offset, setOffset } = listing;
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<Profile | null>(null);
  const profiles = useQuery({
    queryKey: platformKey(token, "profiles", listing.params),
    queryFn: () =>
      api<PageResult<Profile>>(`/profiles?${listing.params}`, token),
  });
  return (
    <section className="panel data-panel">
      <SectionTitle
        title="Durable profiles"
        detail="ABRL browser state, independent of accounts on external platforms."
        action={
          <button
            className="button secondary small"
            onClick={() => setCreating(true)}
          >
            <Plus size={15} />
            New profile
          </button>
        }
      />
      <ListControls label="profiles" state={listing}>
        <Select aria-label="Profile status" value={listing.filters.status ?? ""} onChange={(event) => listing.setFilter("status", event.target.value)}>
          <option value="">All states</option>{["available", "owned", "quarantined"].map((state) => <option key={state} value={state}>{label(state)}</option>)}
        </Select>
        <Select aria-label="Profile persistence" value={listing.filters.persistence ?? ""} onChange={(event) => listing.setFilter("persistence", event.target.value)}><option value="">All profiles</option><option value="fresh">Fresh</option><option value="persistent">Persistent</option></Select>
      </ListControls>
      <ErrorNotice error={profiles.error} />
      {profiles.isPending ? (
        <Loading />
      ) : profiles.data?.items.length ? (
        <>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Browser</th>
                  <th>Persistence</th>
                  <th>Status</th>
                  <th>Generation</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {profiles.data.items.map((profile) => (
                  <tr key={profile.id}>
                    <td>
                      <button
                        className="text-button"
                        onClick={() => setSelected(profile)}
                      >
                        {profile.name}
                      </button>
                    </td>
                    <td>{profile.adapter}</td>
                    <td>{label(profile.persistence)}</td>
                    <td>
                      <Badge value={profile.status} />
                    </td>
                    <td className="mono">{profile.version}</td>
                    <td>
                      <button
                        className="icon-button"
                        aria-label={`View ${profile.name}`}
                        onClick={() => setSelected(profile)}
                      >
                        <ChevronRight size={17} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination
            total={profiles.data.total}
            offset={offset}
            limit={50}
            onChange={setOffset}
          />
        </>
      ) : (
        <Empty
          title="No browser profiles"
          detail="Create a session batch to create independent fresh or persistent profiles."
        />
      )}
      {selected && (
        <ProfileHistory
          token={token}
          profile={selected}
          close={() => setSelected(null)}
        />
      )}{" "}
      {creating && (
        <ProfileForm token={token} close={() => setCreating(false)} />
      )}
    </section>
  );
}
function BatchCreationDetail({ token, batch, close }: { token: string; batch: Batch; close: () => void }) {
  const [offset, setOffset] = useState(0);
  const key = useRef(crypto.randomUUID());
  const query = useQueryClient();
  const items = useQuery({
    queryKey: platformKey(token, "batch-items", batch.id, offset),
    queryFn: () => api<PageResult<BatchItem>>(`/batches/${batch.id}/items?limit=50&offset=${offset}`, token),
    refetchInterval: 5000,
  });
  const retry = useMutation({
    mutationFn: () => api(`/batches/${batch.id}/retry-creation`, token, {
      method: "POST", headers: { "Idempotency-Key": key.current }, body: {},
    }),
    onSuccess: () => {
      key.current = crypto.randomUUID();
      void query.invalidateQueries({ queryKey: ["platform", token] });
    },
  });
  const failures = batch.creation?.failed ?? 0;
  return (
    <Modal title={`${batch.name} · creation`} detail="One durable allocation record per requested session. Retrying creation never reruns an existing session." close={close} wide>
      <div className="modal-body stack">
        <DetailGrid items={[
          ["Requested", batch.count], ["Created", batch.creation?.created ?? 0],
          ["Creation failures", failures], ["Cancelled allocations", batch.creation?.cancelled ?? 0],
        ]} />
        {failures > 0 && <div className="notice">Creation failures remain visible separately from workflow outcomes. A paused batch stays paused after retry.</div>}
        <ErrorNotice error={items.error || retry.error} />
        {items.isPending ? <Loading /> : items.data?.items.length ? <>
          <div className="table-scroll"><table>
            <thead><tr><th>Item</th><th>Browser</th><th>Allocation</th><th>Attempts</th><th>Session / error</th></tr></thead>
            <tbody>{items.data.items.map((item) => <tr key={item.ordinal}>
              <td>{item.ordinal}</td><td>{item.adapter}</td><td><Badge value={item.status} /></td><td>{item.attempts}</td>
              <td>{item.error || (item.session_id ? <span className="mono">{item.session_id}</span> : "No session allocated")}</td>
            </tr>)}</tbody>
          </table></div>
          <Pagination total={items.data.total} offset={offset} limit={50} onChange={setOffset} />
        </> : <Empty title="No allocation records" detail="Historical batches may predate per-item creation records." />}
      </div>
      <div className="modal-footer">
        <button className="button secondary" onClick={close}>Close</button>
        <button className="button primary" disabled={!failures || retry.isPending || ["completed", "cancelled", "cancelling"].includes(batch.status)} onClick={() => retry.mutate()}>
          {retry.isPending ? "Retrying creation…" : "Retry failed creation"}
        </button>
      </div>
    </Modal>
  );
}

function BatchIPResult({ token, batchId }: { token: string; batchId: string }) {
  const summary = useQuery({
    queryKey: platformKey(token, "batch-ip-summary", batchId),
    queryFn: ({ signal }) => api<BatchIPSummary>(`/batches/${batchId}/ip-summary`, token, { signal }),
    refetchInterval: 5000,
  });
  const result = summary.data;
  const status = result && batchIPStatuses[result.status];
  return (
    <div className="batch-ip-summary" role="status" aria-label="Batch IP uniqueness">
      <strong>IP uniqueness</strong>
      {result && status ? (
        <>
          <Badge value={status.label} tone={status.tone} />
          <span className="muted">
            {result.status === "duplicate" || result.status === "unique"
              ? `${result.distinct_ips} distinct across ${result.observed_sessions} observed`
              : `${result.observed_sessions} of ${result.expected_sessions} observed`}
          </span>
        </>
      ) : (
        <span className="muted">{summary.isPending ? "Checking…" : "Unavailable"}</span>
      )}
    </div>
  );
}

function BatchProxyCheckIPResult({ token, batchId, onOpenSession }: {
  token: string;
  batchId: string;
  onOpenSession: (id: string) => void;
}) {
  const summary = useQuery({
    queryKey: platformKey(token, "batch-proxy-check-ip-summary", batchId),
    queryFn: ({ signal }) => api<BatchProxyCheckIPSummary>(
      `/batches/${batchId}/proxy-check-ip-summary`, token, { signal },
    ),
    refetchInterval: 5000,
  });
  const result = summary.data;
  return (
    <section className="batch-ip-summary batch-proxy-check-summary" aria-label="Proxy Check IP summary">
      <div className="batch-proxy-check-overview">
        <strong>Proxy Check IP summary</strong>
        <div className="batch-proxy-check-counts muted" role="status">
          {result ? (
            <>
              <span>{result.checked_sessions} of {result.expected_sessions} checked</span>
              <span>{result.unique_sessions} unique {result.unique_sessions === 1 ? "session" : "sessions"}</span>
              <span>{result.duplicate_sessions} duplicate sessions</span>
              <span>{result.distinct_ips} distinct {result.distinct_ips === 1 ? "IP" : "IPs"}</span>
            </>
          ) : (
            <span>{summary.isPending ? "Checking…" : "Unavailable"}</span>
          )}
        </div>
      </div>
      {result && result.duplicate_groups.length > 0 && (
        <details className="batch-proxy-check-duplicates">
          <summary>Show duplicate sessions</summary>
          <ul className="batch-proxy-check-groups">
            {result.duplicate_groups.map((group, index) => (
              <li key={group.sessions[0]?.id}>
                <span className="mono session-ip-value">{group.proxy_check_ip ?? `Shared IP ${index + 1}`}</span>
                <ul className="batch-proxy-check-members">
                  {group.sessions.map((session) => (
                    <li key={session.id}>
                      <button
                        type="button"
                        className="text-button"
                        aria-label={`View session ${session.ordinal} (${session.id})`}
                        title={session.id}
                        onClick={() => onOpenSession(session.id)}
                      >
                        Session {session.ordinal}
                      </button>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

export function Sessions({
  token,
  targets,
  workflows,
}: {
  token: string;
  targets: Target[];
  workflows: Workflow[];
}) {
  const [tab, setTab] = useState<"sessions" | "batches" | "profiles">(
    "sessions",
  );
  const [batchFilter, setBatchFilter] = useState("");
  const [creating, setCreating] = useState(false);
  const [creationDetail, setCreationDetail] = useState<string | null>(null);
  const listing = useListState();
  const batchListing = useListState();
  const { offset, setOffset, search, setSearch } = listing;
  const status = listing.filters.status ?? "";
  const setStatus = (value: string) => listing.setFilter("status", value);
  const [selected, setSelected] = useState<string | null>(null);
  const [recordingSession, setRecordingSession] = useState<string | null>(null);
  const [selection, setSelection] = useState(new Set<string>());
  const query = useQueryClient();
  const sessions = useQuery({
    queryKey: platformKey(
      token,
      "sessions",
      offset,
      status,
      search,
      batchFilter,
      listing.params,
    ),
    queryFn: () =>
      api<PageResult<BrowserSession>>(
        `/sessions?${listing.params}${batchFilter ? `&batch_id=${batchFilter}` : ""}`,
        token,
      ),
    refetchInterval: 3000,
    enabled: tab === "sessions",
  });
  const batches = useQuery({
    queryKey: platformKey(token, "batches", batchListing.params),
    queryFn: () => api<PageResult<Batch>>(`/batches?${batchListing.params}`, token),
    refetchInterval: 5000,
    enabled: tab === "batches",
  });
  const batchAction = useMutation({
    mutationFn: ({ id, action }: { id: string; action: string }) =>
      api(`/batches/${id}/${action}`, token, {
        method: "POST",
        body: { idempotency_key: crypto.randomUUID() },
      }),
    onSuccess: () =>
      void query.invalidateQueries({ queryKey: ["platform", token] }),
  });
  const bulk = useMutation({
    mutationFn: async () => {
      const results = await Promise.allSettled(
        [...selection].map((id) =>
          api(`/sessions/${id}/commands`, token, {
            method: "POST",
            body: { command: "stop", idempotency_key: crypto.randomUUID() },
          }),
        ),
      );
      const failures = results.filter((r) => r.status === "rejected");
      if (failures.length)
        throw new Error(
          `${failures.length} of ${results.length} stop requests failed. Refresh and retry those sessions.`,
        );
    },
    onSettled: () => {
      setSelection(new Set());
      void query.invalidateQueries({
        queryKey: platformKey(token, "sessions"),
      });
    },
  });
  const active = (sessions.data?.items ?? []).find((s) => s.id === selected);

  function openDuplicateSession(id: string) {
    setSearch(id);
    listing.setFilter("status", "");
    listing.setFilter("mode", "");
    listing.setFilter("adapter", "");
    setSelected(id);
  }

  return (
    <div className="stack">
      <div className="page-toolbar">
        <Segments
          label="Session views"
          value={tab}
          onChange={setTab}
          options={[
            { value: "sessions", label: "Sessions" },
            { value: "batches", label: "Batches" },
            { value: "profiles", label: "Profiles" },
          ]}
        />
        <button className="button primary" onClick={() => setCreating(true)}>
          <CopyPlus size={16} />
          Create batch
        </button>
      </div>
      {tab === "sessions" && (
        <section className="panel data-panel">
          <div className="filter-bar">
            <div className="search-field">
              <Search size={17} />
              <input
                aria-label="Search sessions"
                placeholder="Search session ID"
                maxLength={100}
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setOffset(0);
                }}
              />
            </div>
            <Select
              aria-label="Session status"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">All statuses</option>
              {[
                "queued",
                "starting",
                "ready",
                "running",
                "preparing",
                "awaiting_operator",
                "stopping",
                "completed",
                "failed",
                "blocked",
                "cancelled",
                "needs_reconciliation",
              ].map((value) => (
                <option key={value} value={value}>
                  {label(value)}
                </option>
              ))}
            </Select>
            <Select aria-label="Session evidence" value={listing.filters.mode ?? ""} onChange={(event) => listing.setFilter("mode", event.target.value)}><option value="">All evidence</option><option value="live">Live</option><option value="demo">Demo</option></Select>
            <Select aria-label="Session browser" value={listing.filters.adapter ?? ""} onChange={(event) => listing.setFilter("adapter", event.target.value)}><option value="">All browsers</option>{["playwright", "manual_chrome", "cloakbrowser", "kameleo"].map((adapter) => <option key={adapter} value={adapter}>{label(adapter)}</option>)}</Select>
            <Select aria-label="Sort sessions" value={listing.order} onChange={(event) => listing.setOrder(event.target.value)}><option value="newest">Newest first</option><option value="oldest">Oldest first</option><option value="status_asc">State</option><option value="duration_desc">Longest duration</option></Select>
            <span className="muted mono small-text">
              {sessions.data?.total ?? 0} sessions
            </span>
          </div>
          {batchFilter && (
            <>
              <div className="selection-bar">
                <span>Batch {batchFilter.slice(0, 12)}</span>
                <button
                  className="text-button"
                  onClick={() => setBatchFilter("")}
                >
                  Show all sessions
                </button>
              </div>
              <BatchIPResult token={token} batchId={batchFilter} />
              <BatchProxyCheckIPResult
                key={batchFilter}
                token={token}
                batchId={batchFilter}
                onOpenSession={openDuplicateSession}
              />
            </>
          )}
          {selection.size > 0 && (
            <div className="selection-bar">
              <Check size={16} />
              <strong>{selection.size} selected</strong>
              <button
                className="button danger small"
                disabled={bulk.isPending}
                onClick={() => bulk.mutate()}
              >
                Stop selected
              </button>
              <button
                className="text-button"
                onClick={() => setSelection(new Set())}
              >
                Clear
              </button>
            </div>
          )}
          <ErrorNotice error={sessions.error || bulk.error} />
          {sessions.isPending ? (
            <Loading />
          ) : sessions.data?.items.length ? (
            <>
              <SessionList
                sessions={sessions.data.items}
                selected={selected}
                onOpen={setSelected}
                onPlayRecording={setRecordingSession}
                selection={selection}
                onSelect={(id) =>
                  setSelection((current) => {
                    const next = new Set(current);
                    if (next.has(id)) next.delete(id);
                    else next.add(id);
                    return next;
                  })
                }
              />
              <Pagination
                total={sessions.data.total}
                offset={offset}
                limit={50}
                onChange={setOffset}
              />
            </>
          ) : (
            <Empty
              title="Your browser fleet starts here"
              detail="Create a batch to run a saved workflow in independent browsers."
              action={
                <button
                  className="button secondary"
                  onClick={() => setCreating(true)}
                >
                  <Plus size={16} />
                  Create first batch
                </button>
              }
            />
          )}
        </section>
      )}
      {tab === "batches" && (
        <section className="panel data-panel">
          <SectionTitle
            title="Session batches"
            detail="Resumable creation, progress, and per-session outcomes."
          />
          <ErrorNotice error={batches.error || batchAction.error} />
          <ListControls label="batches" state={batchListing}>
            <Select aria-label="Batch status" value={batchListing.filters.status ?? ""} onChange={(event) => batchListing.setFilter("status", event.target.value)}><option value="">All states</option>{["queued", "running", "paused", "creation_failed", "completed", "cancelled"].map((state) => <option key={state} value={state}>{label(state)}</option>)}</Select>
            <Select aria-label="Batch evidence" value={batchListing.filters.mode ?? ""} onChange={(event) => batchListing.setFilter("mode", event.target.value)}><option value="">All evidence</option><option value="live">Live</option><option value="demo">Demo</option></Select>
          </ListControls>
          {batches.isPending ? (
            <Loading />
          ) : batches.data?.items.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Batch</th>
                    <th>Status</th>
                    <th>Sessions</th>
                    <th>Allocation</th>
                    <th>Completed</th>
                    <th>Failed</th>
                    <th>Created</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {batches.data.items.map((batch) => (
                    <tr key={batch.id}>
                      <td>
                        <button
                          className="text-button"
                          onClick={() => {
                            setBatchFilter(batch.id);
                            setOffset(0);
                            setTab("sessions");
                          }}
                        >
                          <strong>{batch.name}</strong>
                        </button>
                        <small className="table-subtitle mono">
                          {batch.id.slice(0, 12)}
                        </small>
                      </td>
                      <td>
                        <Badge value={batch.status} />
                      </td>
                      <td>{batch.count ?? batch.definition?.count ?? "—"}</td>
                      <td>
                        {batch.creation?.created ?? 0} created
                        <small className="table-subtitle">{batch.creation?.failed ?? 0} creation failures</small>
                      </td>
                      <td>
                        {batch.completed ??
                          batch.counts?.completed ??
                          batch.progress?.completed ??
                          0}
                      </td>
                      <td>
                        {batch.failed ??
                          batch.counts?.failed ??
                          batch.progress?.failed ??
                          0}
                      </td>
                      <td>{dateTime(batch.created_at)}</td>
                      <td>
                        <button className="button secondary small" aria-label={`Creation details for ${batch.name}`} onClick={() => setCreationDetail(batch.id)}>Creation details</button>
                        {!["completed", "cancelled"].includes(batch.status) && (
                          <div className="actions">
                            <button
                              className="icon-button"
                              aria-label={`${batch.status === "paused" ? "Resume" : "Pause"} ${batch.name}`}
                              disabled={batchAction.isPending}
                              onClick={() =>
                                batchAction.mutate({
                                  id: batch.id,
                                  action:
                                    batch.status === "paused"
                                      ? "resume"
                                      : "pause",
                                })
                              }
                            >
                              {batch.status === "paused" ? (
                                <Play size={15} />
                              ) : (
                                <Pause size={15} />
                              )}
                            </button>
                            <button
                              className="icon-button"
                              aria-label={`Cancel ${batch.name}`}
                              disabled={batchAction.isPending}
                              onClick={() =>
                                batchAction.mutate({
                                  id: batch.id,
                                  action: "cancel",
                                })
                              }
                            >
                              <Square size={14} />
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title="No batches yet"
              detail="Batch creation keeps a durable record of every requested session."
            />
          )}
          {batches.data && <Pagination total={batches.data.total} offset={batchListing.offset} limit={50} onChange={batchListing.setOffset} />}
        </section>
      )}
      {tab === "profiles" && <ProfileList token={token} />}
      <div className="context-note">
        <ShieldCheck size={16} />
        <span>
          One writable owner per persistent profile. Browser state is saved only
          after a supported clean shutdown.
        </span>
        <Clock3 size={15} />
        <span>One shared refresh for this list.</span>
      </div>
      {active && (
        <SessionDetail
          key={active.id}
          token={token}
          session={active}
          onPlayRecording={setRecordingSession}
          close={() => setSelected(null)}
        />
      )}{" "}
      {recordingSession && (
        <SessionRecordingPlayer
          key={recordingSession}
          token={token}
          sessionId={recordingSession}
          close={() => setRecordingSession(null)}
        />
      )}
      {creationDetail && batches.data?.items.find((batch) => batch.id === creationDetail) && (
        <BatchCreationDetail token={token} batch={batches.data.items.find((batch) => batch.id === creationDetail)!} close={() => setCreationDetail(null)} />
      )}
      {creating && (
        <BatchWizard
          token={token}
          targets={targets}
          workflows={workflows}
          close={() => setCreating(false)}
          onCreated={() => setTab("sessions")}
        />
      )}
    </div>
  );
}
