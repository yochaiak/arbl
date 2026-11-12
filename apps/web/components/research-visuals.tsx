"use client";

import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ErrorBar,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { dateTime, label, ms, pct } from "@/lib/api";
import { Badge, Empty, SectionTitle } from "./ui";
import { DetailGrid, Modal, Pagination } from "./platform-ui";

export interface ResearchTrial {
  id: string;
  ordinal: number;
  adapter: string;
  status: string;
  outcome: string | null;
  workflow_outcome?: string | null;
  workflow_completed: boolean;
  success: boolean;
  ground_truth: string;
  verification_status: string;
  elapsed_ms: number | null;
  raw_codes: number[];
  environment: unknown;
  attempts: Array<{
    id: string;
    number: number;
    outcome: string | null;
    started_at: string | null;
    finished_at: string | null;
  }>;
}
interface VisualGroup {
  id: string;
  adapter: string;
  environment: unknown;
  n: number;
  successes: number;
  rate: number;
  low: number | null;
  high: number | null;
  verdicts: Record<string, number>;
}
interface Evidence {
  groups: VisualGroup[];
  trials?: ResearchTrial[];
  reliability_target?: number;
  mode: string;
}
const colors = [
  "#6bdee0",
  "#8da8ff",
  "#b2a1ef",
  "#e7b96a",
  "#f38f98",
  "#7990a9",
];
const tooltip = {
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  fontSize: 11,
};
const terminal = new Set([
  "completed",
  "failed",
  "blocked",
  "cancelled",
  "needs_reconciliation",
]);
function environment(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}
function finishedTime(trial: ResearchTrial) {
  const times = trial.attempts
    .map((attempt) =>
      attempt.finished_at ? Date.parse(attempt.finished_at) : NaN,
    )
    .filter(Number.isFinite);
  return times.length ? Math.max(...times) : null;
}
function runtimeKey(group: VisualGroup) {
  return `${group.adapter} ${environment(group.environment).browser_version ?? "unrecorded version"}`;
}
function conditionKey(group: VisualGroup) {
  const env = environment(group.environment);
  return [
    "os",
    "architecture",
    "proxy_product",
    "proxy_region",
    "persistence",
    "preparation",
  ]
    .map((key) => `${key}: ${env[key] ?? "unrecorded"}`)
    .join(" · ");
}
function quantile(values: number[], q: number) {
  return values.length
    ? values[Math.max(0, Math.ceil(q * values.length) - 1)]
    : null;
}

export function ResearchVisuals({ data }: { data: Evidence }) {
  const trials = data.trials ?? [];
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<ResearchTrial | null>(null);
  const [timelineOffset, setTimelineOffset] = useState(0);
  const verified = data.groups.filter((group) =>
    Object.values(group.verdicts).some((count) => count > 0),
  );
  const verdictKeys = [
    ...new Set(verified.flatMap((group) => Object.keys(group.verdicts))),
  ].sort((a, b) => Number(a) - Number(b));
  const verdictData = verified.map((group) => ({
    name: group.adapter,
    ...group.verdicts,
  }));
  const intervals = data.groups
    .filter((group) => group.low !== null && group.high !== null)
    .map((group) => ({
      name: group.adapter,
      rate: group.rate * 100,
      interval: [
        (group.rate - group.low!) * 100,
        (group.high! - group.rate) * 100,
      ],
    }));
  const statusCounts = trials.reduce<Record<string, number>>(
    (counts, trial) => ({
      ...counts,
      [trial.status]: (counts[trial.status] ?? 0) + 1,
    }),
    {},
  );
  const statuses = Object.entries(statusCounts).map(([name, value]) => ({
    name: label(name),
    value,
  }));
  const finished = trials.filter((trial) => terminal.has(trial.status));
  const latencies = finished
    .map((trial) => trial.elapsed_ms)
    .filter(
      (value): value is number =>
        typeof value === "number" && Number.isFinite(value),
    );
  const maxLatency = Math.max(0, ...latencies);
  const binCount = Math.max(
    1,
    Math.min(8, Math.ceil(Math.sqrt(latencies.length))),
  );
  const binSize = Math.max(1, Math.ceil(maxLatency / binCount));
  const histogram = latencies.length
    ? Array.from({ length: binCount }, (_, index) => ({
        range: `${index * binSize}–${(index + 1) * binSize}`,
        count: latencies.filter(
          (value) =>
            Math.min(binCount - 1, Math.floor(value / binSize)) === index,
        ).length,
      }))
    : [];
  const chronological = finished
    .map((trial) => ({ trial, time: finishedTime(trial) }))
    .filter(
      (entry): entry is { trial: ResearchTrial; time: number } =>
        entry.time !== null,
    )
    .sort((a, b) => a.time - b.time);
  const trend = chronological.reduce<
    Array<{ time: number; completed: number; finished: number; rate: number }>
  >((rows, entry, index) => {
    const completed =
      (rows.at(-1)?.completed ?? 0) + (entry.trial.workflow_completed ? 1 : 0);
    return [
      ...rows,
      {
        time: entry.time,
        completed,
        finished: index + 1,
        rate: (completed / (index + 1)) * 100,
      },
    ];
  }, []);
  const candidates = [...new Set(data.groups.map(runtimeKey))];
  const conditions = [...new Set(data.groups.map(conditionKey))];
  const durations = data.groups
    .map((group) => {
      const values = trials
        .filter((trial) => trial.adapter === group.adapter)
        .flatMap((trial) =>
          trial.attempts.flatMap((attempt) => {
            const start = attempt.started_at
              ? Date.parse(attempt.started_at)
              : NaN;
            const end = attempt.finished_at
              ? Date.parse(attempt.finished_at)
              : NaN;
            return Number.isFinite(start) &&
              Number.isFinite(end) &&
              end >= start
              ? [end - start]
              : [];
          }),
        )
        .sort((a, b) => a - b);
      return {
        name: group.adapter,
        n: values.length,
        p50: quantile(values, 0.5),
        p95: quantile(values, 0.95),
      };
    })
    .filter(
      (item, index, all) =>
        item.n > 0 &&
        all.findIndex((other) => other.name === item.name) === index,
    );
  const timeline = trials
    .flatMap((trial) => {
      const starts = trial.attempts
        .map((item) => (item.started_at ? Date.parse(item.started_at) : NaN))
        .filter(Number.isFinite);
      const ends = trial.attempts
        .map((item) => (item.finished_at ? Date.parse(item.finished_at) : NaN))
        .filter(Number.isFinite);
      return starts.length && ends.length
        ? [{ trial, start: Math.min(...starts), end: Math.max(...ends) }]
        : [];
    })
    .sort((a, b) => a.start - b.start);
  const firstTime = timeline.length
    ? Math.min(...timeline.map((item) => item.start))
    : 0;
  const lastTime = timeline.length
    ? Math.max(...timeline.map((item) => item.end))
    : 0;
  const span = Math.max(1, lastTime - firstTime);
  return (
    <div className="stack research-analytics">
      <div className="analytics-grid">
        <section className="panel data-panel">
          <SectionTitle
            action={<Badge value={data.mode} />}
            title="Verified provider verdicts"
            detail="Raw vendor codes over verified observations only. Clean (0) is a provider category."
          />
          {verified.length ? (
            <div className="analytics-chart">
              <ResponsiveContainer width="100%" height={260}>
                <BarChart
                  data={verdictData}
                  margin={{ left: 0, right: 20, top: 10, bottom: 5 }}
                >
                  <CartesianGrid stroke="var(--border)" vertical={false} />
                  <XAxis
                    dataKey="name"
                    tick={{ fontSize: 10, fill: "var(--muted)" }}
                  />
                  <YAxis
                    allowDecimals={false}
                    tick={{ fontSize: 10, fill: "var(--muted)" }}
                  />
                  <Tooltip contentStyle={tooltip} />
                  <Legend wrapperStyle={{ fontSize: 10 }} />
                  {verdictKeys.map((code, index) => (
                    <Bar
                      key={code}
                      dataKey={code}
                      name={code === "0" ? "Clean (0)" : `Code ${code}`}
                      stackId="verdicts"
                      fill={colors[index % colors.length]}
                      isAnimationActive={false}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <Empty
              title="No verified verdict distribution"
              detail="Unverified callbacks, simulations, and absent provider evidence do not become verified classifications."
            />
          )}
        </section>
        <section className="panel data-panel">
          <SectionTitle
            action={<Badge value={data.mode} />}
            title="Reliability and 95% intervals"
            detail="Intervals use the method recorded for each group. The line marks the declared qualification threshold."
          />
          {intervals.length ? (
            <div className="analytics-chart">
              <ResponsiveContainer width="100%" height={260}>
                <BarChart
                  data={intervals}
                  margin={{ left: 0, right: 20, top: 10, bottom: 5 }}
                >
                  <CartesianGrid stroke="var(--border)" vertical={false} />
                  <XAxis
                    dataKey="name"
                    tick={{ fontSize: 10, fill: "var(--muted)" }}
                  />
                  <YAxis
                    domain={[0, 100]}
                    tickFormatter={(value) => `${value}%`}
                    tick={{ fontSize: 10, fill: "var(--muted)" }}
                  />
                  <Tooltip
                    contentStyle={tooltip}
                    formatter={(value) => [
                      `${Number(value).toFixed(1)}%`,
                      "Primary reliability",
                    ]}
                  />
                  {typeof data.reliability_target === "number" && (
                    <ReferenceLine
                      y={data.reliability_target * 100}
                      stroke="var(--amber)"
                      strokeDasharray="4 4"
                    />
                  )}
                  <Bar
                    dataKey="rate"
                    fill="#547b90"
                    maxBarSize={48}
                    isAnimationActive={false}
                  >
                    <ErrorBar
                      dataKey="interval"
                      width={8}
                      stroke="var(--cyan)"
                      direction="y"
                    />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <Empty
              title="Intervals unavailable"
              detail="This snapshot does not contain an eligible uncertainty interval."
            />
          )}
        </section>
      </div>
      <div className="analytics-grid">
        <section className="panel data-panel">
          <SectionTitle
            action={<Badge value={data.mode} />}
            title="Completion trend over time"
            detail="Cumulative workflow completion among timestamped, finished logical sessions. Pending sessions remain in the main report's denominator."
          />
          {trend.length ? (
            <div className="analytics-chart">
              <ResponsiveContainer width="100%" height={250}>
                <LineChart
                  data={trend}
                  margin={{ left: 0, right: 20, top: 10, bottom: 5 }}
                >
                  <CartesianGrid stroke="var(--border)" vertical={false} />
                  <XAxis
                    type="number"
                    dataKey="time"
                    domain={["dataMin", "dataMax"]}
                    tickFormatter={(time) =>
                      new Date(time).toLocaleTimeString("en", {
                        hour: "2-digit",
                        minute: "2-digit",
                      })
                    }
                    tick={{ fontSize: 10, fill: "var(--muted)" }}
                  />
                  <YAxis
                    domain={[0, 100]}
                    tickFormatter={(value) => `${value}%`}
                    tick={{ fontSize: 10, fill: "var(--muted)" }}
                  />
                  <Tooltip
                    contentStyle={tooltip}
                    labelFormatter={(time) =>
                      dateTime(new Date(Number(time)).toISOString())
                    }
                    formatter={(value) => [
                      `${Number(value).toFixed(1)}%`,
                      "Cumulative completion",
                    ]}
                  />
                  <Line
                    type="stepAfter"
                    dataKey="rate"
                    stroke="var(--cyan)"
                    strokeWidth={2}
                    dot={trend.length < 30}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <Empty
              title="No finished-session timestamps"
              detail="The trend appears when execution attempts record finish times."
            />
          )}
        </section>
        <section className="panel data-panel">
          <SectionTitle
            action={<Badge value={data.mode} />}
            title="Session latency distribution"
            detail={`${latencies.length} finished sessions with recorded elapsed time, including preparation, execution, cleanup, and recorded failures. Bins are in milliseconds.`}
          />
          {histogram.length ? (
            <div className="analytics-chart">
              <ResponsiveContainer width="100%" height={250}>
                <BarChart
                  data={histogram}
                  margin={{ left: 0, right: 20, top: 10, bottom: 5 }}
                >
                  <CartesianGrid stroke="var(--border)" vertical={false} />
                  <XAxis
                    dataKey="range"
                    tick={{ fontSize: 9, fill: "var(--muted)" }}
                  />
                  <YAxis
                    allowDecimals={false}
                    tick={{ fontSize: 10, fill: "var(--muted)" }}
                  />
                  <Tooltip
                    contentStyle={tooltip}
                    formatter={(value) => [value, "Sessions"]}
                  />
                  <Bar
                    dataKey="count"
                    fill="#8da8ff"
                    isAnimationActive={false}
                    radius={[3, 3, 0, 0]}
                  />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <Empty
              title="No recorded latency"
              detail="Missing elapsed-time measurements are excluded, never replaced with zero."
            />
          )}
        </section>
      </div>
      <div className="analytics-grid">
        <section className="panel data-panel">
          <SectionTitle
            action={<Badge value={data.mode} />}
            title="Execution progress"
            detail={`${finished.length} of ${trials.length} logical sessions reached a final state in this snapshot.`}
          />
          {statuses.length ? (
            <div className="analytics-chart">
              <ResponsiveContainer width="100%" height={240}>
                <PieChart>
                  <Pie
                    data={statuses}
                    dataKey="value"
                    nameKey="name"
                    innerRadius={57}
                    outerRadius={85}
                    paddingAngle={2}
                    isAnimationActive={false}
                  >
                    {statuses.map((status, index) => (
                      <Cell
                        key={status.name}
                        fill={colors[index % colors.length]}
                      />
                    ))}
                  </Pie>
                  <Tooltip contentStyle={tooltip} />
                  <Legend wrapperStyle={{ fontSize: 10 }} />
                </PieChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <Empty
              title="No session outcomes"
              detail="Logical sessions appear after a batch is created."
            />
          )}
        </section>
        <section className="panel data-panel">
          <SectionTitle
            action={<Badge value={data.mode} />}
            title="Execution attempt performance"
            detail="Durations are derived from recorded attempt start and finish timestamps, grouped by browser. Retries remain separate attempts."
          />
          {durations.length ? (
            <div className="analytics-chart">
              <ResponsiveContainer width="100%" height={240}>
                <BarChart
                  data={durations}
                  margin={{ left: 0, right: 20, top: 10, bottom: 5 }}
                >
                  <CartesianGrid stroke="var(--border)" vertical={false} />
                  <XAxis
                    dataKey="name"
                    tick={{ fontSize: 10, fill: "var(--muted)" }}
                  />
                  <YAxis tick={{ fontSize: 10, fill: "var(--muted)" }} />
                  <Tooltip
                    contentStyle={tooltip}
                    formatter={(value, name) => [
                      ms(typeof value === "number" ? value : null),
                      name,
                    ]}
                  />
                  <Legend wrapperStyle={{ fontSize: 10 }} />
                  <Bar
                    dataKey="p50"
                    name="Attempt p50"
                    fill={colors[0]}
                    isAnimationActive={false}
                  />
                  <Bar
                    dataKey="p95"
                    name="Attempt p95"
                    fill={colors[1]}
                    isAnimationActive={false}
                  />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <Empty
              title="No timed execution attempts"
              detail="Attempt duration charts require recorded timestamps."
            />
          )}
          <p className="chart-footnote">
            Historical per-worker CPU and memory utilization are unavailable in
            this snapshot. Current worker readiness is shown in Fleet &
            settings.
          </p>
        </section>
      </div>
      <section className="panel data-panel">
        <SectionTitle
          action={<Badge value={data.mode} />}
          title="Browser × environment reliability"
          detail="Cell color encodes the observed primary success rate. Open environment labels for the exact matched conditions."
        />
        {data.groups.length ? (
          <div className="table-scroll">
            <table className="environment-heatmap">
              <thead>
                <tr>
                  <th>Browser candidate</th>
                  {conditions.map((condition, index) => (
                    <th key={condition}>
                      <details>
                        <summary>Environment {index + 1}</summary>
                        <p>{condition}</p>
                      </details>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {candidates.map((candidate) => (
                  <tr key={candidate}>
                    <th scope="row">{candidate}</th>
                    {conditions.map((condition) => {
                      const groups = data.groups.filter(
                        (group) =>
                          runtimeKey(group) === candidate &&
                          conditionKey(group) === condition,
                      );
                      const n = groups.reduce((sum, group) => sum + group.n, 0);
                      const successes = groups.reduce(
                        (sum, group) => sum + group.successes,
                        0,
                      );
                      return (
                        <td key={condition}>
                          {n ? (
                            <div
                              className="heatmap-cell"
                              style={{
                                background: `rgba(107,222,224,${0.05 + (0.24 * successes) / n})`,
                              }}
                            >
                              <strong>{pct(successes / n)}</strong>
                              <small>
                                {successes} / {n} trials
                              </small>
                            </div>
                          ) : (
                            <span className="muted">
                              No matched observations
                            </span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title="No environment groups"
            detail="Each executed browser records the conditions used for its comparison group."
          />
        )}
      </section>
      <section className="panel data-panel">
        <SectionTitle
          action={<Badge value={data.mode} />}
          title="Individual trial outcome matrix"
          detail="Each cell is one logical session. Open a cell to inspect its status, verification, ground truth, and recorded attempts."
        />
        <div className="trial-matrix">
          {trials.slice(offset, offset + 100).map((trial) => (
            <button
              type="button"
              key={trial.id}
              className={`trial-cell ${trial.success ? "success" : terminal.has(trial.status) ? "failure" : "pending"}`}
              aria-label={`Inspect trial ${trial.ordinal}: ${label(trial.status)}`}
              title={`${trial.adapter} · ${trial.id} · ${trial.verification_status}`}
              onClick={() => setSelected(trial)}
            >
              {trial.ordinal}
            </button>
          ))}
        </div>
        {!trials.length && (
          <Empty
            title="No logical sessions"
            detail="The matrix remains empty until sessions are created."
          />
        )}
        <Pagination
          total={trials.length}
          offset={offset}
          limit={100}
          onChange={setOffset}
        />
      </section>
      <section className="panel data-panel">
        <SectionTitle
          action={<Badge value={data.mode} />}
          title="Experiment execution timeline"
          detail="Bars span the first recorded attempt start to the final recorded finish. Missing timestamps are not inferred."
        />
        {timeline.length ? (
          <>
            <div className="timeline-scale">
              <span>{dateTime(new Date(firstTime).toISOString())}</span>
              <span>{dateTime(new Date(lastTime).toISOString())}</span>
            </div>
            <div className="execution-timeline">
              {timeline
                .slice(timelineOffset, timelineOffset + 30)
                .map(({ trial, start, end }) => (
                  <button
                    key={trial.id}
                    className="execution-line"
                    onClick={() => setSelected(trial)}
                  >
                    <span className="mono">
                      {trial.adapter}
                      <small>
                        #{trial.ordinal} · {trial.id.slice(0, 8)}
                      </small>
                    </span>
                    <span className="execution-track">
                      <span
                        className={
                          trial.success
                            ? "success"
                            : terminal.has(trial.status)
                              ? "failure"
                              : "pending"
                        }
                        style={{
                          left: `${((start - firstTime) / span) * 100}%`,
                          width: `${Math.max(0.6, ((end - start) / span) * 100)}%`,
                        }}
                      />
                    </span>
                    <span className="mono">{ms(end - start)}</span>
                  </button>
                ))}
            </div>
            <Pagination
              total={timeline.length}
              offset={timelineOffset}
              limit={30}
              onChange={setTimelineOffset}
            />
          </>
        ) : (
          <Empty
            title="No recorded execution span"
            detail="The timeline requires both start and finish timestamps from attempts."
          />
        )}
      </section>
      {selected && (
        <Modal
          title={`Logical trial ${selected.ordinal}`}
          detail={selected.id}
          close={() => setSelected(null)}
        >
          <div className="modal-body stack">
            <div className="actions">
              <Badge value={selected.status} />
              <Badge value={data.mode} />
            </div>
            <DetailGrid
              items={[
                ["Browser", selected.adapter],
                ["Workflow outcome", selected.workflow_outcome ?? "Unrecorded"],
                [
                  "Primary outcome",
                  selected.success ? "Successful" : "Not successful",
                ],
                ["Verification", selected.verification_status],
                ["Ground truth", selected.ground_truth],
                [
                  "Verified raw codes",
                  selected.raw_codes.length
                    ? selected.raw_codes
                        .map((code) =>
                          code === 0 ? "0 · Clean" : String(code),
                        )
                        .join(", ")
                    : "Unavailable",
                ],
                ["Workflow elapsed", ms(selected.elapsed_ms)],
                ["Attempts", selected.attempts.length],
              ]}
            />
            <h3>Recorded environment</h3>
            <pre className="json-preview">
              {JSON.stringify(selected.environment, null, 2)}
            </pre>
            <h3>Attempt history</h3>
            <ol className="activity-timeline">
              {selected.attempts.map((attempt) => (
                <li key={attempt.id}>
                  <span className="timeline-dot" />
                  <div>
                    <strong>
                      Attempt {attempt.number} · {attempt.outcome ?? "Pending"}
                    </strong>
                    <small>
                      {attempt.started_at
                        ? dateTime(attempt.started_at)
                        : "Start unrecorded"}{" "}
                      →{" "}
                      {attempt.finished_at
                        ? dateTime(attempt.finished_at)
                        : "Finish unrecorded"}
                    </small>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </Modal>
      )}
    </div>
  );
}
