"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowDownToLine,
  FlaskConical,
  Info,
  Plus,
  ShieldCheck,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, label, ms, pct, request, saveFile } from "@/lib/api";
import {
  type Batch,
  type PageResult,
  type SharedProps,
  platformKey,
} from "@/lib/platform";
import { Badge, Empty, ErrorNotice, Field, Loading, SectionTitle, Select } from "./ui";
import { BatchWizard } from "./sessions";
import { Experiments } from "./experiments";
import { Segments } from "./platform-ui";
import { ResearchVisuals, type ResearchTrial } from "./research-visuals";
import { Metric } from "./runs";
interface ComparisonGroup {
  id: string;
  adapter: string;
  environment: unknown;
  n: number;
  successes: number;
  rate: number;
  low: number | null;
  high: number | null;
  p50_ms: number | null;
  latency_stddev_ms?: number | null;
  cost_currency?: string | null;
  diagnostics?: Record<
    string,
    { numerator: number; denominator: number; rate: number | null }
  >;
  p95_ms: number | null;
  verified: number;
  verdicts: Record<string, number>;
  cost_per_success: number | null;
  cost_metering?: {
    known_proxy_subtotals: Record<string, number>;
    measured_proxy_bytes: number;
    measured_attempts: number;
    sessions_with_total_cost: number;
  };
  first_attempt?: { rate: number; successes: number; total: number };
  recovered?: number;
  operational_reliability?: {
    rate: number;
    low: number | null;
    high: number | null;
    successes: number;
    total: number;
  };
  completion_rate?: number;
  verified_clean_rate?: number | null;
  first_attempt_rate?: number;
  recovery_rate?: number;
  cluster_interval?: { low: number; high: number };
}
interface Comparison {
  trials?: ResearchTrial[];
  reliability_target?: number;
  batch_id: string;
  mode: string;
  status: string;
  groups: ComparisonGroup[];
  comparisons: Array<{
    a: string;
    b: string;
    difference: number;
    p_value: number | null;
    adjusted_p_value: number | null;
    significant: boolean;
  }>;
  recommendation: { status: string; message: string; candidates: string[] };
  limitations: string[];
  evidence_mode?: string;
}
interface RunSelection { experimentId: string; runId: string }
interface RunComparison {
  run_id: string;
  experiment_id: string;
  version: number;
  mode: string;
  status: string;
  batch_ids: string[];
  snapshot_sha256: string;
  randomization: Record<string, unknown>;
  strata: Array<Comparison & { id: string; conditions: Record<string, unknown>; batch_ids: string[] }>;
}
function Comparisons(props: SharedProps & { initialBatchId?: string; runSelection?: RunSelection; onExitRun?: () => void }) {
  const { token } = props;
  const [selected, setSelected] = useState(props.initialBatchId ?? "");
  const [creating, setCreating] = useState(false);
  const [exportError, setExportError] = useState<unknown>(null);
  const [exporting, setExporting] = useState(false);
  const [selectedStratum, setSelectedStratum] = useState("");
  const runPath = props.runSelection ? `/experiments/${props.runSelection.experimentId}/runs/${props.runSelection.runId}` : null;
  const batches = useQuery({
    queryKey: platformKey(token, "research-batches"),
    queryFn: () => api<PageResult<Batch>>("/batches?limit=100", token),
    refetchInterval: 10000,
    enabled: !runPath,
  });
  const batchId = selected || batches.data?.items[0]?.id || "";
  const result = useQuery({
    queryKey: platformKey(token, "comparisons", runPath ?? batchId),
    queryFn: () =>
      api<Comparison | RunComparison>(
        runPath ? `${runPath}/comparison` : `/research/comparisons?batch_id=${encodeURIComponent(batchId)}`,
        token,
      ),
    enabled: !!runPath || !!batchId,
    refetchInterval: 10000,
  });
  const runData = result.data && "strata" in result.data ? result.data : undefined;
  const data = runData
    ? runData.strata.find((stratum) => stratum.id === selectedStratum) ?? runData.strata[0]
    : result.data as Comparison | undefined;
  const total = data?.groups.reduce((n, g) => n + g.n, 0) ?? 0;
  const verified = data?.groups.reduce((n, g) => n + g.verified, 0) ?? 0;
  async function download(format: string) {
    setExporting(true);
    setExportError(null);
    try {
      const response = await request(
        runPath ? `${runPath}/export?format=${format}` : `/research/export?batch_id=${encodeURIComponent(batchId)}&format=${format}`,
        token,
      );
      saveFile(await response.blob(), `abrl-research-${props.runSelection?.runId ?? batchId}.${format}`);
      if (runPath) void result.refetch();
    } catch (error) {
      setExportError(error);
    } finally {
      setExporting(false);
    }
  }
  return (
    <div className="stack">
      <div className="page-toolbar">
        {runPath ? <button className="button secondary" onClick={props.onExitRun}>Back to batch reports</button> : <Select
          aria-label="Research batch"
          value={batchId}
          onChange={(e) => setSelected(e.target.value)}
        >
          <option value="" disabled>
            Select a batch
          </option>
          {batches.data?.items.map((batch) => (
            <option value={batch.id} key={batch.id}>
              {batch.name} · {label(batch.status)}
            </option>
          ))}
        </Select>}
        <button className="button primary" onClick={() => setCreating(true)}>
          <Plus size={16} />
          New batch comparison
        </button>
      </div>
      {runData && <section className="panel data-panel stack run-report-scope">
        <SectionTitle title="Experiment execution report" detail={`Definition v${runData.version} · ${runData.batch_ids.length} batches · ${runData.strata.length} matched condition groups. Exports include every condition group.`} action={<Badge value={runData.mode} />} />
        <p className="muted small-text">Recommendations apply only within the selected target, workflow, evidence mode, and environment. Different conditions are never combined into a global winner.</p>
        <Field label="Matched conditions">
          <Select value={(data as RunComparison["strata"][number] | undefined)?.id ?? ""} onChange={(event) => setSelectedStratum(event.target.value)}>
            {runData.strata.map((stratum, index) => <option value={stratum.id} key={stratum.id}>
              {index + 1}. {props.targets.find((target) => target.id === stratum.conditions.target_id)?.name ?? String(stratum.conditions.target_id)} / {props.workflows.find((workflow) => workflow.id === stratum.conditions.workflow_id)?.name ?? String(stratum.conditions.workflow_id)} · {String(stratum.conditions.phase)}
            </option>)}
          </Select>
        </Field>
        <details className="environment-details"><summary>Conditions and execution order</summary><pre>{JSON.stringify({ conditions: (data as RunComparison["strata"][number] | undefined)?.conditions, randomization: runData.randomization, snapshot_sha256: runData.snapshot_sha256 }, null, 2)}</pre></details>
      </section>}
      <ErrorNotice error={batches.error || result.error || exportError} />
      {!batchId && !runPath ? (
        <section className="panel">
          <Empty
            title="Evidence before recommendations"
            detail="Compare supported browser configurations against a saved target and workflow. Start with a pilot, then declare a separate confirmation run."
            action={
              <button
                className="button secondary"
                onClick={() => setCreating(true)}
              >
                <FlaskConical size={16} />
                Configure comparison
              </button>
            }
          />
        </section>
      ) : result.isPending ? (
        <Loading />
      ) : (
        data && (
          <>
            <section
              className={`panel recommendation-card ${data.recommendation.status === "qualified" ? "qualified" : ""}`}
            >
              <span className="feature-icon">
                <ShieldCheck size={25} />
              </span>
              <div>
                <div className="actions">
                  <span className="eyebrow">DECISION REPORT</span>
                  <Badge value={data.mode} />
                  <Badge value={data.recommendation.status} />
                </div>
                <h2>{data.recommendation.message}</h2>
                {data.recommendation.candidates.length > 0 && (
                  <p>
                    Eligible candidates:{" "}
                    {data.recommendation.candidates
                      .map(
                        (id) =>
                          data.groups.find((group) => group.id === id)
                            ?.adapter ?? id,
                      )
                      .join(", ")}
                  </p>
                )}
              </div>
            </section>
            <div className="metrics metrics-four">
              <Metric
                title="Research samples"
                value={total.toLocaleString()}
                detail="Logical trials; retries add no samples"
                icon={<FlaskConical size={17} />}
              />
              <Metric
                title="Browser groups"
                value={String(data.groups.length)}
                detail="Matched target and workflow"
                icon={<ShieldCheck size={17} />}
              />
              <Metric
                title="Verification coverage"
                value={total ? pct(verified / total) : "—"}
                detail={`${verified} verified observations`}
                icon={<ShieldCheck size={17} />}
              />
              <Metric
                title="Evidence source"
                value={label(data.mode)}
                detail={
                  data.evidence_mode
                    ? label(data.evidence_mode)
                    : "See group environment and limitations"
                }
                icon={<Info size={17} />}
              />
            </div>
            <div className="research-grid">
              <section className="panel data-panel">
                <SectionTitle
                  title="Reliability by browser"
                  detail="Operational failures remain in the denominator. Error intervals are shown in the table."
                />
                <div className="research-chart">
                  <ResponsiveContainer width="100%" height={260}>
                    <BarChart
                      data={data.groups.map((group) => ({
                        ...group,
                        percent: group.rate * 100,
                      }))}
                      margin={{ top: 10, right: 24, left: 0, bottom: 10 }}
                    >
                      <CartesianGrid stroke="var(--border)" vertical={false} />
                      <XAxis
                        dataKey="adapter"
                        stroke="var(--muted)"
                        tickLine={false}
                        axisLine={false}
                      />
                      <YAxis
                        domain={[0, 100]}
                        tickFormatter={(v) => `${v}%`}
                        stroke="var(--muted)"
                        tickLine={false}
                        axisLine={false}
                      />
                      <Tooltip
                        contentStyle={{
                          background: "var(--surface)",
                          border: "1px solid var(--border)",
                          borderRadius: 10,
                        }}
                        formatter={(value) => [
                          `${Number(value).toFixed(1)}%`,
                          "Reliability",
                        ]}
                      />
                      <Bar
                        dataKey="percent"
                        fill="var(--cyan)"
                        radius={[5, 5, 0, 0]}
                        maxBarSize={60}
                        isAnimationActive={false}
                      />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </section>
              <section className="panel methodology-card">
                <span className="eyebrow">HOW TO READ THIS REPORT</span>
                <h3>
                  Comparable conditions.
                  <br />
                  Visible uncertainty.
                </h3>
                <ul className="method-list">
                  <li>
                    Instrumented primary outcome: completed workflow plus
                    verified Clean.
                  </li>
                  <li>
                    Browser-only primary outcome: observable workflow
                    completion.
                  </li>
                  <li>
                    Independent rates use 95% Wilson intervals; repeated
                    profiles require cluster-aware analysis.
                  </li>
                  <li>
                    Qualification uses the interval lower bound against the
                    configured reliability threshold.
                  </li>
                  <li>
                    Simulation and incomplete runs cannot produce final live
                    recommendations.
                  </li>
                </ul>
              </section>
            </div>
            <section className="panel data-panel">
              <SectionTitle
                title="Group evidence"
                detail="Qualification requires both primary and operational 95% lower bounds to meet the target. Operational reliability includes launch, execution, and cleanup failures."
                action={
                  <div className="actions">
                    {["json", "csv", "html", "pdf"].map((format) => (
                      <button
                        key={format}
                        className="button secondary small"
                        disabled={
                          exporting ||
                          !["completed", "cancelled"].includes(runData?.status ?? data.status)
                        }
                        onClick={() => void download(format)}
                      >
                        <ArrowDownToLine size={14} />
                        {format.toUpperCase()}
                      </button>
                    ))}
                  </div>
                }
              />
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Browser</th>
                      <th>Successes / trials</th>
                      <th>Primary reliability</th>
                      <th>95% interval</th>
                      <th>Operational reliability</th>
                      <th>Verified</th>
                      <th>First attempt / recovered</th>
                      <th>Session latency p50 / p95</th>
                      <th>Cost / success</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.groups.map((group, index) => (
                      <tr key={`${group.adapter}-${index}`}>
                        <td>
                          <strong>{group.adapter}</strong>
                          <details className="environment-details">
                            <summary>Environment</summary>
                            <pre>
                              {JSON.stringify(group.environment, null, 2)}
                            </pre>
                          </details>
                        </td>
                        <td className="mono">
                          {group.successes} / {group.n}
                        </td>
                        <td>{pct(group.rate)}</td>
                        <td className="mono">
                          {pct(group.low)} – {pct(group.high)}
                        </td>
                        <td>
                          {pct(group.operational_reliability?.rate ?? null)}
                          <small className="table-subtitle mono">
                            95%: {pct(group.operational_reliability?.low ?? null)} – {pct(group.operational_reliability?.high ?? null)}
                          </small>
                        </td>
                        <td>
                          {group.verified} / {group.n}
                          <small className="table-subtitle">
                            {Object.entries(group.verdicts ?? {})
                              .map(
                                ([verdict, count]) =>
                                  `${verdict === "0" ? "Clean (0)" : label(verdict)} ${count}`,
                              )
                              .join(" · ") || "No verified verdicts"}
                          </small>
                        </td>
                        <td>
                          {pct(group.first_attempt?.rate ?? null)}
                          <small className="table-subtitle">
                            {group.recovered ?? 0} recovered workflows
                          </small>
                        </td>
                        <td>
                          {ms(group.p50_ms)}
                          <small className="table-subtitle">
                            {ms(group.p95_ms)} · σ{" "}
                            {ms(group.latency_stddev_ms ?? null)}
                          </small>
                        </td>
                        <td>
                          {group.cost_per_success === null
                            ? "Unknown"
                            : `${group.cost_per_success.toFixed(4)} ${group.cost_currency ?? "(currency unknown)"}`}
                          {group.cost_metering && <small className="table-subtitle">
                            Proxy subtotal estimate: {Object.entries(group.cost_metering.known_proxy_subtotals).map(([currency, amount]) => `${amount.toFixed(4)} ${currency}`).join(" · ") || "Unknown"}. {(group.cost_metering.measured_proxy_bytes / 1048576).toFixed(2)} MiB measured; {group.cost_metering.sessions_with_total_cost}/{group.n} sessions with complete cost inputs.
                          </small>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
            {data.groups.some((group) => group.diagnostics) && (
              <section className="panel data-panel">
                <SectionTitle
                  title="Diagnostic rates and denominators"
                  detail="Detector rates use their stated verified population. Operational and verification failures use planned logical sessions."
                />
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Browser</th>
                        <th>Measurement</th>
                        <th>Rate</th>
                        <th>Numerator / denominator</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.groups.flatMap((group) =>
                        Object.entries(group.diagnostics ?? {}).map(
                          ([key, measurement]) => (
                            <tr key={`${group.id}-${key}`}>
                              <td>{group.adapter}</td>
                              <td>{label(key)}</td>
                              <td>{pct(measurement.rate)}</td>
                              <td className="mono">
                                {measurement.numerator} /{" "}
                                {measurement.denominator}
                              </td>
                            </tr>
                          ),
                        ),
                      )}
                    </tbody>
                  </table>
                </div>
              </section>
            )}
            {data.comparisons.length > 0 && (
              <section className="panel data-panel">
                <SectionTitle
                  title="Pairwise comparisons"
                  detail="Corrected multiple-comparison probabilities. Statistical differences do not replace qualification."
                />
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Comparison</th>
                        <th>Rate difference</th>
                        <th>Raw p</th>
                        <th>Adjusted p</th>
                        <th>Evidence</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.comparisons.map((item, index) => (
                        <tr key={index}>
                          <td>
                            {data.groups.find((group) => group.id === item.a)
                              ?.adapter ?? item.a}{" "}
                            /{" "}
                            {data.groups.find((group) => group.id === item.b)
                              ?.adapter ?? item.b}
                          </td>
                          <td>{pct(item.difference)}</td>
                          <td className="mono">
                            {item.p_value?.toPrecision(3) ?? "Unavailable"}
                          </td>
                          <td className="mono">
                            {item.adjusted_p_value?.toPrecision(3) ??
                              "Unavailable"}
                          </td>
                          <td>
                            <Badge
                              value={
                                item.significant
                                  ? "significant"
                                  : "inconclusive"
                              }
                              tone={item.significant ? "green" : "quiet"}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}
            <ResearchVisuals data={data} />
            {data.limitations.length > 0 && (
              <section className="panel limitations-card">
                <h3>Limitations & pending evidence</h3>
                <ul>
                  {data.limitations.map((limitation, index) => (
                    <li key={index}>{limitation}</li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )
      )}
      {creating && (
        <BatchWizard
          {...props}
          close={() => setCreating(false)}
          onCreated={() => {
            void batches.refetch();
          }}
        />
      )}
    </div>
  );
}

export function Research(props: SharedProps) {
  const [view, setView] = useState<"comparisons" | "experiments">(
    "comparisons",
  );
  const [batch, setBatch] = useState("");
  const [runSelection, setRunSelection] = useState<RunSelection>();
  return (
    <div className="stack">
      <Segments
        label="Research views"
        value={view}
        onChange={setView}
        options={[
          { value: "comparisons", label: "Decision reports" },
          { value: "experiments", label: "Experiments" },
        ]}
      />
      {view === "experiments" ? (
        <Experiments
          {...props}
          onCompare={(id) => {
            setRunSelection(undefined);
            setBatch(id);
            setView("comparisons");
          }}
          onCompareRun={(experimentId, runId) => {
            setRunSelection({ experimentId, runId });
            setView("comparisons");
          }}
        />
      ) : (
        <Comparisons key={runSelection?.runId ?? batch} {...props} initialBatchId={batch} runSelection={runSelection} onExitRun={() => setRunSelection(undefined)} />
      )}
    </div>
  );
}
