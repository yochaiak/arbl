"use client";

import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Boxes,
  CheckCircle2,
  Cpu,
  Plus,
  Server,
  Settings2,
  ShieldCheck,
  Users,
} from "lucide-react";
import { api, currentProject, dateTime, label } from "@/lib/api";
import {
  type Adapter,
  type Project,
  type PageResult,
  platformKey,
  humanValue,
} from "@/lib/platform";
import {
  Badge,
  Empty,
  ErrorNotice,
  Field,
  Loading,
  SectionTitle,
  Select,
} from "./ui";
import { ListControls, Modal, Pagination, Segments, useListState } from "./platform-ui";
import { Metric } from "./runs";
interface Fleet {
  active: number;
  queued: number;
  admitted: number;
  global_limit: number;
  workers: Array<{
    id: string;
    pool: string;
    capacity: number;
    seen_at: string;
    ready: boolean;
  }>;
  pools: Array<{ name: string; active: number; queued: number }>;
  cost: {
    hourly_estimate: number | null;
    known_hourly_subtotal?: number;
    configured_fixed_hourly?: number | null;
    currency?: string;
    inventory_coverage?: string;
    unknown_components?: string[];
    missing_prices_or_identity?: string[];
  };
  integrations: Record<string, unknown>;
}
interface Member {
  user_id: string;
  subject: string;
  email: string;
  name: string;
  role: string;
}
function MemberForm({ token, close }: { token: string; close: () => void }) {
  const [subject, setSubject] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("viewer");
  const query = useQueryClient();
  const save = useMutation({
    mutationFn: () =>
      api(`/projects/${currentProject()}/members`, token, {
        method: "PUT",
        body: { subject, email, role },
      }),
    onSuccess: () => {
      void query.invalidateQueries({ queryKey: platformKey(token, "members") });
      close();
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate();
  }
  return (
    <Modal
      title="Add project member"
      detail="Use the member's configured identity-provider subject."
      close={close}
    >
      <form onSubmit={submit}>
        <div className="modal-body stack">
          <Field label="Identity subject">
            <input
              required
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
            />
          </Field>
          <Field label="Email (optional)">
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <Field label="Project role">
            <Select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="viewer">Viewer · read-only access</option>
              <option value="analyst">Analyst · research and reports</option>
              <option value="operator">
                Operator · execute and manage sessions
              </option>
              <option value="owner">
                Owner · administration and membership
              </option>
            </Select>
          </Field>
          <ErrorNotice error={save.error} />
        </div>
        <div className="modal-footer">
          <button className="button secondary" type="button" onClick={close}>
            Cancel
          </button>
          <button className="button primary" disabled={save.isPending}>
            Save membership
          </button>
        </div>
      </form>
    </Modal>
  );
}
function ProjectForm({
  token,
  close,
  initial,
}: {
  token: string;
  close: () => void;
  initial?: Project;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [concurrency, setConcurrency] = useState(initial?.concurrency ?? 20);
  const query = useQueryClient();
  const create = useMutation({
    mutationFn: () =>
      api(initial ? `/projects/${initial.id}` : "/projects", token, {
        method: initial ? "PUT" : "POST",
        body: { name, concurrency },
      }),
    onSuccess: () => {
      void query.invalidateQueries({
        queryKey: platformKey(token, "projects"),
      });
      void query.invalidateQueries({ queryKey: platformKey(token, "fleet") });
      close();
    },
  });
  return (
    <Modal
      title={initial ? "Project settings" : "Create project"}
      detail={
        initial
          ? "Control the current project's admission limit. Global, target, and provider limits still apply."
          : "Isolate members, configurations, and research evidence."
      }
      close={close}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <div className="modal-body stack">
          <Field label="Project name">
            <input
              required
              maxLength={100}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field
            label="Project concurrency limit"
            hint="Lowering this limit stops new admission until existing sessions finish; active sessions keep running."
          >
            <input
              required
              type="number"
              min={1}
              max={500}
              value={concurrency}
              onChange={(e) => setConcurrency(Number(e.target.value))}
            />
          </Field>
          <ErrorNotice error={create.error} />
        </div>
        <div className="modal-footer">
          <button className="button secondary" type="button" onClick={close}>
            Cancel
          </button>
          <button className="button primary" disabled={create.isPending}>
            {initial ? "Save project settings" : "Create project"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
export function FleetPage({ token }: { token: string }) {
  const [tab, setTab] = useState<"fleet" | "adapters" | "team">("fleet");
  const workerList = useListState(50);
  const workers = useQuery({
    queryKey: platformKey(token, "workers", workerList.params),
    queryFn: () => api<PageResult<Fleet["workers"][number]>>(`/fleet/workers?${workerList.params}`, token),
    refetchInterval: 5000,
    enabled: tab === "fleet",
  });
  const [creating, setCreating] = useState<
    "member" | "project" | "settings" | null
  >(null);
  const fleet = useQuery({
    queryKey: platformKey(token, "fleet"),
    queryFn: () => api<Fleet>("/fleet", token),
    refetchInterval: 5000,
  });
  const adapters = useQuery({
    queryKey: platformKey(token, "adapters"),
    queryFn: () => api<Adapter[]>("/adapters", token),
    refetchInterval: 15000,
  });
  const members = useQuery({
    queryKey: platformKey(token, "members"),
    queryFn: () =>
      api<Member[]>(`/projects/${currentProject()}/members`, token),
    enabled: tab === "team",
  });
  const projects = useQuery({
    queryKey: platformKey(token, "projects"),
    queryFn: () => api<Project[]>("/projects", token),
  });
  const data = fleet.data;
  const selectedProject = projects.data?.find((p) => p.id === currentProject());
  const owner = selectedProject?.role === "owner";
  return (
    <div className="stack">
      <div className="page-toolbar">
        <Segments
          label="Fleet and settings"
          value={tab}
          onChange={setTab}
          options={[
            { value: "fleet", label: "Worker fleet" },
            { value: "adapters", label: "Browser integrations" },
            { value: "team", label: "Project & team" },
          ]}
        />
        {tab === "team" && owner && (
          <div className="actions">
            <button
              className="button secondary"
              onClick={() => setCreating("settings")}
            >
              <Settings2 size={16} />
              Project settings
            </button>
            <button
              className="button secondary"
              onClick={() => setCreating("project")}
            >
              <Plus size={16} />
              New project
            </button>
            <button
              className="button primary"
              onClick={() => setCreating("member")}
            >
              <Users size={16} />
              Add member
            </button>
          </div>
        )}
      </div>
      {tab === "fleet" && (
        <>
          <ErrorNotice error={fleet.error} />
          {fleet.isPending ? (
            <Loading />
          ) : (
            data && (
              <>
                <div className="metrics metrics-four">
                  <Metric
                    title="Active browsers"
                    value={String(data.active)}
                    detail={`${data.global_limit} global hard limit`}
                    icon={<Activity size={17} />}
                  />
                  <Metric
                    title="Admitted / queued"
                    value={`${data.admitted} / ${data.queued}`}
                    detail="Jobs scale from admitted work"
                    icon={<Boxes size={17} />}
                  />
                  <Metric
                    title="Workers ready"
                    value={`${data.workers.filter((w) => w.ready).length} / ${data.workers.length}`}
                    detail="Based on persisted heartbeats"
                    icon={<Server size={17} />}
                  />
                  <Metric
                    title="Observed compute estimate"
                    value={
                      data.cost.hourly_estimate === null
                        ? "Unknown"
                        : `${data.cost.hourly_estimate} ${data.cost.currency ?? ""}/h`
                    }
                    detail="Observed runner nodes only · not billed cost"
                    icon={<Cpu size={17} />}
                  />
                </div>
                <section className="panel data-panel">
                  <SectionTitle title="Cost coverage" detail="Configured prices and measured traffic produce estimates. Missing costs remain unknown; these are not provider invoices." />
                  <dl className="integration-list">
                    <div><dt>Known observed compute subtotal</dt><dd>{data.cost.known_hourly_subtotal == null ? "Unknown" : `${data.cost.known_hourly_subtotal.toFixed(4)} ${data.cost.currency ?? ""}/h`}</dd></div>
                    <div><dt>Configured fixed infrastructure</dt><dd>{data.cost.configured_fixed_hourly == null ? "Unknown" : `${data.cost.configured_fixed_hourly.toFixed(4)} ${data.cost.currency ?? ""}/h`}</dd></div>
                    <div><dt>Complete fleet total</dt><dd>Unknown until every component is priced and reconciled</dd></div>
                    {!!data.cost.unknown_components?.length && <div><dt>Unpriced components</dt><dd>{data.cost.unknown_components.map(label).join(" · ")}</dd></div>}
                    {!!data.cost.missing_prices_or_identity?.length && <div><dt>Missing node inputs</dt><dd>{data.cost.missing_prices_or_identity.join(" · ")}</dd></div>}
                  </dl>
                </section>
                <section className="panel capacity-panel">
                  <div>
                    <span className="eyebrow">CLOUD CAPACITY MODEL</span>
                    <h3>100 → 250 → 500 browsers</h3>
                    <p>
                      Staged live validation against owned fixtures. Hardware
                      estimates remain benchmark hypotheses until measured.
                    </p>
                  </div>
                  <div className="capacity-estimate">
                    <strong>
                      2,000 vCPU <span>/</span> 4,000 GiB
                    </strong>
                    <small>
                      500 vendor sessions at 4 vCPU / 8 GiB, before platform
                      overhead
                    </small>
                  </div>
                </section>
                <section className="panel data-panel">
                  <SectionTitle
                    title="Worker pools"
                    detail="Admission enforces the narrowest project, license, proxy, target, and resource limit."
                  />
                  {data.pools.length ? (
                    <div className="pool-grid">
                      {data.pools.map((pool) => (
                        <div key={pool.name}>
                          <Server size={20} />
                          <strong>{pool.name}</strong>
                          <span>
                            {pool.active} active · {pool.queued} queued
                          </span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <Empty
                      title="No registered pools"
                      detail="A native local runner or cloud worker registers its pool through heartbeats."
                    />
                  )}
                </section>
                <section className="panel data-panel">
                  <SectionTitle
                    title="Runtime health"
                    detail="Worker observations are persisted across API processes."
                  />
                  <ListControls label="workers" state={workerList}>
                    <Select aria-label="Worker pool" value={workerList.filters.pool ?? ""} onChange={(event) => workerList.setFilter("pool", event.target.value)}><option value="">All pools</option><option value="standard">Standard</option><option value="vendor">Vendor</option></Select>
                    <Select aria-label="Worker readiness" value={workerList.filters.ready ?? ""} onChange={(event) => workerList.setFilter("ready", event.target.value)}><option value="">All readiness</option><option value="true">Ready</option><option value="false">Offline</option></Select>
                  </ListControls>
                  <ErrorNotice error={workers.error} />
                  {workers.isPending ? <Loading /> : workers.data?.items.length ? (
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>Worker</th>
                            <th>Pool</th>
                            <th>Capacity</th>
                            <th>Status</th>
                            <th>Last heartbeat</th>
                          </tr>
                        </thead>
                        <tbody>
                          {workers.data.items.map((worker) => (
                            <tr key={worker.id}>
                              <td className="mono">{worker.id}</td>
                              <td>{worker.pool}</td>
                              <td>{worker.capacity}</td>
                              <td>
                                <Badge
                                  value={worker.ready ? "ready" : "offline"}
                                />
                              </td>
                              <td>{dateTime(worker.seen_at)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <Empty
                      title="No worker heartbeat"
                      detail="Start the local session worker or deploy a cloud worker pool."
                    />
                  )}
                  <Pagination total={workers.data?.total ?? 0} limit={50} offset={workerList.offset} onChange={workerList.setOffset} />
                </section>
                {Object.keys(data.integrations).length > 0 && (
                  <section className="panel data-panel">
                    <SectionTitle title="Service integration readiness" />
                    <dl className="integration-list">
                      {Object.entries(data.integrations).map(([key, value]) => (
                        <div key={key}>
                          <dt>{label(key)}</dt>
                          <dd>
                            {typeof value === "object" ? (
                              <pre>{JSON.stringify(value, null, 2)}</pre>
                            ) : (
                              humanValue(value)
                            )}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  </section>
                )}
              </>
            )
          )}
        </>
      )}
      {tab === "adapters" && (
        <>
          <ErrorNotice error={adapters.error} />
          {adapters.isPending ? (
            <Loading />
          ) : (
            <div className="adapter-grid">
              {adapters.data?.map((adapter) => (
                <section className="panel adapter-card" key={adapter.id}>
                  <div className="subsection-header">
                    <span className="feature-icon">
                      <Server size={22} />
                    </span>
                    <Badge value={adapter.status} />
                  </div>
                  <h3>{adapter.name}</h3>
                  <p>
                    {adapter.reason ||
                      (adapter.available
                        ? "The supported browser runtime is available."
                        : "Readiness has not been verified.")}
                  </p>
                  <div className="tag-list">
                    {(Array.isArray(adapter.capabilities)
                      ? adapter.capabilities
                      : Object.entries(adapter.capabilities)
                          .filter(([, enabled]) => enabled)
                          .map(([key]) => key)
                    ).map((capability) => (
                      <span key={capability} className="pill">
                        <CheckCircle2 size={12} />
                        {label(capability)}
                      </span>
                    ))}
                  </div>
                  {adapter.version && (
                    <small className="mono muted">
                      Version {adapter.version}
                    </small>
                  )}
                  {adapter.id === "cloakbrowser" && (
                    <p className="notice small-text">
                      Exact binary pinning and concurrency depend on your
                      license. Free-tier version pinning is not assumed.
                    </p>
                  )}
                  {adapter.id === "kameleo" && (
                    <p className="notice small-text">
                      Linux cloud workers require amd64 and compatible
                      SDK/engine versions. Proxy changes stop and restart the
                      profile.
                    </p>
                  )}
                </section>
              ))}
            </div>
          )}
        </>
      )}
      {tab === "team" && (
        <>
          <section className="panel data-panel">
            <SectionTitle
              title="Project members"
              detail="Roles apply to lifecycle controls, artifacts, reports, and browser viewers."
            />
            <ErrorNotice error={members.error} />
            {members.isPending ? (
              <Loading />
            ) : members.data?.length ? (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Member</th>
                      <th>Identity subject</th>
                      <th>Role</th>
                    </tr>
                  </thead>
                  <tbody>
                    {members.data.map((member) => (
                      <tr key={member.user_id}>
                        <td>
                          <strong>
                            {member.name || member.email || "Project member"}
                          </strong>
                          {member.email && (
                            <small className="table-subtitle">
                              {member.email}
                            </small>
                          )}
                        </td>
                        <td className="mono">{member.subject}</td>
                        <td>
                          <Badge value={member.role} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty
                title="No membership data"
                detail="The bootstrap owner or configured identity provider supplies project access."
              />
            )}
          </section>
          <div className="notice">
            <ShieldCheck size={17} />
            Cloud deployments require configured OIDC. Local development
            supports the bootstrap owner token. Membership changes are audited.
          </div>
        </>
      )}
      {creating === "member" && (
        <MemberForm token={token} close={() => setCreating(null)} />
      )}{" "}
      {creating === "project" && (
        <ProjectForm token={token} close={() => setCreating(null)} />
      )}
      {creating === "settings" && selectedProject && (
        <ProjectForm
          token={token}
          initial={selectedProject}
          close={() => setCreating(null)}
        />
      )}
    </div>
  );
}
