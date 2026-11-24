import { ms } from "@/lib/api";
import { DetailGrid } from "./platform-ui";

const lifecycleLabels = new Map([
  ["preflight_completed", "Pre-flight completed"],
  ["browser_launched", "Browser launched"],
  ["navigation_started", "First navigation started"],
  ["document_committed", "Initial document committed"],
  ["dom_content_loaded", "DOM content loaded"],
  ["first_subresource_completed", "First subresource completed"],
  ["last_subresource_completed_before_load", "Last subresource completed before load"],
  ["page_loaded", "Page loaded"],
  ["first_interaction", "First interaction dispatched"],
  ["first_post_load_step", "First post-load workflow step"],
  ["workflow_completed", "Workflow completed"],
  ["failure_observed", "Browser failure observed"],
]);

const connectSources = new Map([
  ["local_proxy", "Local browser proxy"],
  ["decodo", "Decodo"],
  ["upstream_proxy", "Upstream proxy"],
  ["upstream_connection", "Connection to upstream proxy"],
]);

const connectReasons = new Map([
  ["connect_established", "Tunnel established"],
  ["local_scope_denied", "Destination outside proxy scope"],
  ["local_dns_denied", "Destination DNS rejected"],
  ["local_capacity", "Connection capacity reached"],
  ["local_forwarding_disabled", "Forwarding disabled"],
  ["local_budget_exhausted", "Traffic budget exhausted"],
  ["local_invalid_request", "Invalid CONNECT request"],
  ["local_direct_connect_failed", "Direct connection failed"],
  ["target_dns_unavailable", "Destination DNS unavailable"],
  ["proxy_gateway_dns_unavailable", "Proxy gateway DNS unavailable"],
  ["proxy_gateway_dns_denied", "Proxy gateway DNS rejected"],
  ["proxy_gateway_timeout", "Proxy gateway timed out"],
  ["proxy_gateway_tls_failed", "Proxy gateway TLS failed"],
  ["proxy_tunnel_timeout", "Proxy tunnel timed out"],
  ["proxy_tunnel_failed", "Proxy tunnel failed"],
  ["proxy_response_invalid", "Invalid proxy response"],
  ["proxy_destination_denied", "Destination rejected by provider"],
  ["authentication", "Proxy authentication failed"],
  ["quota", "Proxy quota reached"],
]);

const navigationOutcomes = new Map([
  ["committed", "Committed"],
  ["failed", "Failed"],
  ["redirected", "Redirected"],
  ["incomplete", "Incomplete"],
]);

const capabilityLabels = {
  audio_context: "Audio context",
  canvas_2d: "Canvas 2D",
  webgl: "WebGL",
  font_loading: "Font loading API",
  performance_observer: "Performance Observer",
  network_information: "Network Information API",
  service_worker: "Service Worker API",
};

const availabilityLabels = {
  available: "Available",
  unsupported: "Unsupported",
  unavailable: "Unavailable",
};

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value);
}

export function NavigationConnects({ value }: { value: unknown }) {
  const report = record(value);
  if (report?.version !== 1 || !Array.isArray(report.navigations)) return null;

  const rows = report.navigations.slice(0, 64).flatMap((value: unknown) => {
    const row = record(value);
    if (!row || !isInteger(row.navigation) || row.navigation < 1 || typeof row.outcome !== "string") return [];
    const outcome = navigationOutcomes.get(row.outcome);
    if (!outcome || !Array.isArray(row.connects)) return [];
    const replies = row.connects.slice(0, 8).flatMap((value: unknown) => {
      const reply = record(value);
      if (typeof reply?.source !== "string" || typeof reply.reason !== "string") return [];
      const source = connectSources.get(reply.source);
      const reason = connectReasons.get(reply.reason);
      if (!source || !reason) return [];
      const status = reply.http_status;
      return [`${source} · ${reason}${isInteger(status) && status >= 100 && status <= 599 ? ` · HTTP ${status}` : ""}`];
    });
    return [{
      navigation: row.navigation,
      step: isInteger(row.step) && row.step >= 1 ? row.step : null,
      redirected: row.redirected === true,
      outcome,
      ambiguous: row.attribution === "ambiguous",
      replies,
    }];
  });
  if (!rows.length) return null;

  return <section>
    <h3>Navigation CONNECT telemetry</h3>
    <p className="muted small-text">CONNECT events are matched by destination and timing. Shared or reused connections can leave attribution inconclusive.</p>
    <div className="table-scroll">
      <table aria-label="Navigation CONNECT telemetry">
        <thead><tr>
          <th scope="col">Navigation</th>
          <th scope="col">Outcome</th>
          <th scope="col">CONNECT evidence</th>
        </tr></thead>
        <tbody>{rows.map(({ navigation, step, redirected, outcome, ambiguous, replies }) => <tr key={navigation}>
          <th scope="row">
            {navigation}{redirected && <small className="table-subtitle">Redirect hop</small>}
            {step !== null && <small className="table-subtitle">Step {step}</small>}
          </th>
          <td>{outcome}</td>
          <td>
            {ambiguous && <div>Ambiguous match</div>}
            {replies.length ? replies.map((reply, index) => <div key={index}>{reply}</div>)
              : !ambiguous && <span className="muted">No matching CONNECT observed</span>}
          </td>
        </tr>)}</tbody>
      </table>
    </div>
    {report.truncated === true && <p className="muted small-text">The capture limit was reached.</p>}
  </section>;
}

export function LifecycleTimeline({ value }: { value: unknown }) {
  const timeline = record(value);
  if (timeline?.version !== 1 || !Array.isArray(timeline.events)) return null;

  const events = timeline.events.slice(0, lifecycleLabels.size).flatMap((event: unknown) => {
    const row = record(event);
    if (!row || typeof row.event !== "string") return [];
    const title = lifecycleLabels.get(row.event);
    const elapsed = row.elapsed_ms;
    const delta = row.navigation_delta_ms;
    if (!title || !isInteger(elapsed) || elapsed < 0 || (delta !== null && !isInteger(delta))) return [];
    return [{ event: row.event, title, elapsed, delta }];
  });
  if (!events.length) return null;
  const failureIndex = events.findIndex(({ event }) => event === "failure_observed");
  const hadLateEvents = failureIndex >= 0 && failureIndex < events.length - 1;

  return <section>
    <h3>Load timeline</h3>
    <p className="muted small-text">Negative offsets precede the first navigation.</p>
    {hadLateEvents && <p className="muted small-text">The browser can still report milestones during cleanup.</p>}
    <div className="table-scroll">
      <table aria-label="Load timeline">
        <thead><tr>
          <th scope="col">Milestone</th>
          <th scope="col">From attempt start</th>
          <th scope="col">From navigation</th>
        </tr></thead>
        <tbody>{events.map(({ title, elapsed, delta }, index) => <tr key={title}>
          <td>{title}{failureIndex >= 0 && index > failureIndex && <small className="muted"> · after failure</small>}</td>
          <td className="mono">{ms(elapsed)}</td>
          <td className="mono">{delta === null ? "—" : `${delta > 0 ? "+" : ""}${ms(delta)}`}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </section>;
}

export function DiagnosticCapabilities({ data }: { data: Record<string, unknown> }) {
  const capabilities = record(data.capabilities);
  const timings = record(data.connection_timings_ms);
  const items: [string, string][] = [];
  for (const [key, title] of Object.entries(capabilityLabels)) {
    const value = capabilities?.[key];
    if (value === "available" || value === "unsupported" || value === "unavailable") {
      items.push([title, availabilityLabels[value]]);
    }
  }
  for (const [key, title] of [["document", "Document timing"], ["echo", "Echo timing"]]) {
    const row = record(timings?.[key]);
    if (row?.status === "unsupported" || row?.status === "unavailable") {
      items.push([title, availabilityLabels[row.status]]);
    } else if (row?.status === "observed") {
      const values = [["duration", "Total"], ["request_to_first_byte", "First byte"], ["connect", "Connect"]]
        .flatMap(([field, name]) => {
          const value = row[field];
          return isInteger(value) && value >= 0 && value <= 60_000 ? [`${name} ${ms(value)}`] : [];
        });
      items.push([title, values.join(" · ") || "Observed"]);
    }
  }

  return items.length > 0 ? <details>
    <summary>Runtime capabilities and connection timings</summary>
    <DetailGrid items={items} />
  </details> : null;
}
