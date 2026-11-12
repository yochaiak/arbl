"use client";

import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  Globe2,
  KeyRound,
  Network,
  Plus,
  ShieldCheck,
} from "lucide-react";
import { api, label, ms, pct } from "@/lib/api";
import {
  type PageResult,
  type ProxyInventory,
  type ProxyPolicy,
  type ProxyInventoryInput,
  platformKey,
  configuredDraft,
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
import { DetailGrid, ListControls, Modal, Pagination, Segments, useListState } from "./platform-ui";

type PolicyDraft = Omit<ProxyPolicy, "id" | "version" | "created_at">;
type InventoryDraft = ProxyInventoryInput;
const products = ["datacenter", "dedicated_datacenter", "isp", "dedicated_isp", "residential"] as const;
const gateways = {
  residential: "pr.oxylabs.io",
  isp: "isp.oxylabs.io",
  dedicated_isp: "disp.oxylabs.io",
  datacenter: "dc.oxylabs.io",
  dedicated_datacenter: "ddc.oxylabs.io",
} as const;
function PolicyEditor({
  token,
  close,
  initial,
}: {
  token: string;
  close: () => void;
  initial?: ProxyPolicy;
}) {
  const query = useQueryClient();
  const [value, setValue] = useState<PolicyDraft>(() =>
    configuredDraft<PolicyDraft>(
      {
        name: "Continuity-first policy",
        country: "IL",
        fallback_countries: [],
        products: ["isp", "dedicated_isp", "datacenter"],
        continuity: "fixed",
        continuity_minutes: 60,
        exclusive: false,
        allow_direct: false,
        failure_policy: "connection_recovery",
        max_replacements: 1,
        provider_resolved_ack: false,
        max_latency_ms: null,
        min_transport_success_rate: null,
        connection_load: 1,
      },
      initial,
    ),
  );
  const patch = (next: Partial<PolicyDraft>) => setValue({ ...value, ...next });
  const save = useMutation({
    mutationFn: () =>
      api(
        initial ? `/proxy-policies/${initial.id}` : "/proxy-policies",
        token,
        { method: initial ? "PUT" : "POST", body: value },
      ),
    onSuccess: () => {
      void query.invalidateQueries({
        queryKey: platformKey(token, "proxy-policies"),
      });
      close();
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate();
  }
  return (
    <Modal
      title={initial ? "Edit proxy policy" : "Create proxy policy"}
      detail="Versioned selection constraints and safe recovery behavior."
      close={close}
    >
      <form onSubmit={submit}>
        <div className="modal-body stack">
          <div className="form-grid">
            <Field label="Policy name">
              <input
                required
                value={value.name}
                onChange={(e) => patch({ name: e.target.value })}
              />
            </Field>
            <Field
              label="Required country"
              hint="Two-letter country code. Empty means any country."
            >
              <input
                maxLength={2}
                pattern="[A-Z]{2}"
                value={value.country ?? ""}
                onChange={(e) =>
                  patch({ country: e.target.value.toUpperCase() || null })
                }
              />
            </Field>
            <Field
              label="Permitted fallback countries"
              hint="Comma-separated country codes. No implicit geographical fallback."
            >
              <input
                value={value.fallback_countries.join(", ")}
                onChange={(e) =>
                  patch({
                    fallback_countries: e.target.value
                      .split(",")
                      .map((v) => v.trim().toUpperCase())
                      .filter(Boolean),
                  })
                }
              />
            </Field>
            <Field label="Continuity guarantee" hint="Strict affinity fails rather than silently changing exits; it does not guarantee uptime or exclusivity.">
              <Select
                value={value.continuity}
                onChange={(e) =>
                  patch({
                    continuity: e.target.value as PolicyDraft["continuity"],
                  })
                }
              >
                <option value="fixed">Fixed exit</option>
                <option value="exclusive">Exclusive fixed exit</option>
                <option value="strict">Strict affinity</option>
                <option value="sticky">Sticky session · exit may change</option>
                <option value="none">No continuity required</option>
              </Select>
            </Field>
            <Field label="Maximum transport latency (ms)" hint="Optional measured transport-health threshold.">
              <input
                type="number" min={1} placeholder="No threshold"
                value={value.max_latency_ms ?? ""}
                onChange={(e) => patch({ max_latency_ms: e.target.value === "" ? null : Number(e.target.value) })}
              />
            </Field>
            <Field label="Minimum transport success (%)" hint="Optional. Detector verdicts never affect this health score.">
              <input
                type="number" min={0} max={100} step={0.1} placeholder="No threshold"
                value={value.min_transport_success_rate == null ? "" : value.min_transport_success_rate * 100}
                onChange={(e) => patch({ min_transport_success_rate: e.target.value === "" ? null : Number(e.target.value) / 100 })}
              />
            </Field>
            <Field label="Required continuity (minutes)">
              <input
                type="number"
                required
                min={1}
                max={1440}
                value={value.continuity_minutes}
                onChange={(e) =>
                  patch({ continuity_minutes: Number(e.target.value) })
                }
              />
            </Field>
            <Field label="Maximum proxy connections per session" hint="Reserves this many connection units and enforces the same live transport limit.">
              <input type="number" required min={1} max={32} value={value.connection_load ?? 1} onChange={(event) => patch({ connection_load: Number(event.target.value) })} />
            </Field>
          </div>
          <fieldset className="choice-fieldset">
            <legend>Allowed product families</legend>
            <div className="checkbox-grid">
              {products.map((product) => (
                <label className="checkbox-line" key={product}>
                  <input
                    type="checkbox"
                    checked={value.products.includes(product)}
                    onChange={(e) =>
                      patch({
                        products: e.target.checked
                          ? [...value.products, product]
                          : value.products.filter((p) => p !== product),
                      })
                    }
                  />
                  <span>{label(product)}</span>
                </label>
              ))}
            </div>
            <p>
              Residential exits may change during a sticky session. Essential
              continuity requires eligible fixed inventory.
            </p>
          </fieldset>
          <div className="form-grid">
            <Field label="Failure recovery">
              <Select
                value={value.failure_policy}
                onChange={(e) =>
                  patch({
                    failure_policy: e.target
                      .value as PolicyDraft["failure_policy"],
                  })
                }
              >
                <option value="connection_recovery">
                  Replace on transport failure at safe boundaries
                </option>
                <option value="fixed">Fixed binding · pause on failure</option>
              </Select>
            </Field>
            <Field label="Maximum replacements">
              <input
                type="number"
                min={0}
                max={3}
                required
                value={value.max_replacements}
                onChange={(e) =>
                  patch({ max_replacements: Number(e.target.value) })
                }
              />
            </Field>
          </div>
          <label className="checkbox-line">
            <input
              type="checkbox"
              checked={value.exclusive}
              onChange={(e) => patch({ exclusive: e.target.checked })}
            />
            <span>Require an exclusive connection</span>
          </label>
          <label className="checkbox-line">
            <input
              type="checkbox"
              checked={value.allow_direct}
              onChange={(e) => patch({ allow_direct: e.target.checked })}
            />
            <span>Allow direct connectivity when explicitly eligible</span>
          </label>
          <label className="checkbox-line">
            <input
              type="checkbox"
              checked={value.provider_resolved_ack}
              onChange={(e) =>
                patch({ provider_resolved_ack: e.target.checked })
              }
            />
            <span>Acknowledge provider-side DNS resolution trust boundary</span>
          </label>
          <p className="notice">
            <ShieldCheck size={17} />
            Site challenges and detector verdicts do not improve proxy health
            scores or trigger replacement. Ambiguous submissions require
            reconciliation.
          </p>
          <ErrorNotice error={save.error} />
        </div>
        <div className="modal-footer">
          <button type="button" className="button secondary" onClick={close}>
            Cancel
          </button>
          <button
            className="button primary"
            disabled={save.isPending || value.products.length === 0}
          >
            {save.isPending
              ? "Saving…"
              : initial
                ? "Save new version"
                : "Create policy"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
function InventoryEditor({
  token,
  close,
  initial,
}: {
  token: string;
  close: () => void;
  initial?: ProxyInventory;
}) {
  const query = useQueryClient();
  const [value, setValue] = useState<InventoryDraft>(() =>
    configuredDraft<InventoryDraft>(
      {
        name: "Oxylabs inventory",
        product: "residential",
        country: "IL",
        host: "pr.oxylabs.io",
        port: 7777,
        username_ref: "OXYLABS_USERNAME",
        password_ref: "OXYLABS_PASSWORD",
        capacity: 1,
        max_duration_minutes: 30,
        exclusive: false,
        cost_per_gb: null,
        cost_currency: null,
        enabled: true,
      },
      initial,
    ),
  );
  const patch = (next: Partial<InventoryDraft>) =>
    setValue({ ...value, ...next });
  const save = useMutation({
    mutationFn: () =>
      api(
        initial ? `/proxy-inventory/${initial.id}` : "/proxy-inventory",
        token,
        { method: initial ? "PUT" : "POST", body: value },
      ),
    onSuccess: () => {
      void query.invalidateQueries({
        queryKey: platformKey(token, "proxy-inventory"),
      });
      close();
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate();
  }
  return (
    <Modal
      title={initial ? "Edit proxy inventory" : "Register proxy inventory"}
      detail="Use your entitled Oxylabs endpoint and named secret references."
      close={close}
    >
      <form onSubmit={submit}>
        <div className="modal-body stack">
          <div className="form-grid">
            <Field label="Inventory name">
              <input
                required
                value={value.name}
                onChange={(e) => patch({ name: e.target.value })}
              />
            </Field>
            <Field label="Product family">
              <Select
                value={value.product}
                onChange={(e) => {
                  const product = e.target.value as ProxyInventoryInput["product"];
                  patch({ product, host: gateways[product], port: product === "residential" ? 7777 : 8001,
                    exclusive: product === "dedicated_isp" || product === "dedicated_datacenter" ? value.exclusive : false,
                  });
                }}
              >
                {products.map((product) => (
                  <option key={product} value={product}>
                    {label(product)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Endpoint hostname">
              <input
                required
                pattern="[A-Za-z0-9.-]+"
                value={value.host}
                onChange={(e) => patch({ host: e.target.value })}
              />
            </Field>
            <Field label="Port" hint={value.product === "residential" ? "Residential gateway uses port 7777." : "Use your assigned fixed port (8001 or above). Rotating port 8000 is unsupported."}>
              <input
                required
                type="number"
                min={1}
                max={65535}
                value={value.port}
                onChange={(e) => patch({ port: Number(e.target.value) })}
              />
            </Field>
            <Field label="Country">
              <input
                required
                maxLength={2}
                pattern="[A-Z]{2}"
                value={value.country}
                onChange={(e) =>
                  patch({ country: e.target.value.toUpperCase() })
                }
              />
            </Field>
            <Field label="Connection capacity">
              <input
                required
                type="number"
                min={1}
                max={500}
                value={value.capacity}
                onChange={(e) => patch({ capacity: Number(e.target.value) })}
              />
            </Field>
            <Field label="Maximum continuity (minutes)">
              <input
                required
                type="number"
                min={1}
                max={1440}
                value={value.max_duration_minutes}
                onChange={(e) =>
                  patch({ max_duration_minutes: Number(e.target.value) })
                }
              />
            </Field>
            <Field label="Known cost per GB (optional)">
              <input
                type="number"
                min={0}
                step="0.01"
                placeholder="Unknown"
                value={value.cost_per_gb ?? ""}
                onChange={(e) =>
                  patch({
                    cost_per_gb:
                      e.target.value === "" ? null : Number(e.target.value),
                  })
                }
              />
            </Field>
            <Field label="Proxy cost currency" hint="ISO currency code for the configured per-GB price. Unknown prices remain unknown.">
              <input maxLength={3} pattern="[A-Z]{3}" placeholder="USD" value={value.cost_currency ?? ""} onChange={(event) => patch({ cost_currency: event.target.value.toUpperCase() || null })} />
            </Field>
            <Field label="Username secret reference">
              <input
                required
                pattern="[A-Za-z][A-Za-z0-9_]{0,47}"
                autoComplete="off"
                value={value.username_ref}
                onChange={(e) => patch({ username_ref: e.target.value })}
              />
            </Field>
            <Field label="Password secret reference">
              <input
                required
                pattern="[A-Za-z][A-Za-z0-9_]{0,47}"
                autoComplete="off"
                value={value.password_ref}
                onChange={(e) => patch({ password_ref: e.target.value })}
              />
            </Field>
          </div>
          <label className="checkbox-line">
            <input
              type="checkbox"
              checked={value.exclusive}
              disabled={value.product !== "dedicated_isp" && value.product !== "dedicated_datacenter"}
              onChange={(e) => patch({ exclusive: e.target.checked })}
            />
            <span>Require dedicated inventory exclusivity</span>
          </label>
          <label className="checkbox-line">
            <input
              type="checkbox"
              checked={value.enabled}
              onChange={(e) => patch({ enabled: e.target.checked })}
            />
            <span>Enable for eligible assignments</span>
          </label>
          <div className="notice">
            <KeyRound size={17} />
            Only reference names are stored here. Resolve credentials through
            the worker environment or secret store.
          </div>
          <ErrorNotice error={save.error} />
        </div>
        <div className="modal-footer">
          <button type="button" className="button secondary" onClick={close}>
            Cancel
          </button>
          <button className="button primary" disabled={save.isPending}>
            {save.isPending
              ? "Saving…"
              : initial
                ? "Save inventory"
                : "Register inventory"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
interface Lease {
  id: string;
  session_id: string;
  inventory_id: string;
  status: string;
  created_at: string;
  rationale?: unknown;
  selection_rationale?: unknown;
  environment_epoch?: number;
  epoch?: number;
  country?: string;
  product?: string;
}
export function NetworkPage({ token }: { token: string }) {
  const [tab, setTab] = useState<"policies" | "inventory" | "leases">(
    "policies",
  );
  const [editing, setEditing] = useState<"policy" | "inventory" | null>(null);
  const [selected, setSelected] = useState<Lease | null>(null);
  const [policyEdit, setPolicyEdit] = useState<ProxyPolicy>();
  const [inventoryEdit, setInventoryEdit] = useState<ProxyInventory>();
  const policyList = useListState();
  const inventoryList = useListState();
  const leaseList = useListState();
  const listing = tab === "policies" ? policyList : tab === "inventory" ? inventoryList : leaseList;
  const policies = useQuery({
    queryKey: platformKey(token, "proxy-policies", policyList.params),
    queryFn: () =>
      api<PageResult<ProxyPolicy>>(`/proxy-policies?${policyList.params}`, token),
  });
  const inventory = useQuery({
    queryKey: platformKey(token, "proxy-inventory", inventoryList.params),
    queryFn: () =>
      api<PageResult<ProxyInventory>>(`/proxy-inventory?${inventoryList.params}`, token),
    refetchInterval: 10000,
  });
  const leases = useQuery({
    queryKey: platformKey(token, "proxy-leases", leaseList.params),
    queryFn: () => api<PageResult<Lease>>(`/proxy-leases?${leaseList.params}`, token),
    enabled: tab === "leases",
    refetchInterval: 10000,
  });
  return (
    <div className="stack">
      <div className="page-toolbar">
        <Segments
          label="Network views"
          value={tab}
          onChange={setTab}
          options={[
            { value: "policies", label: "Selection policies" },
            { value: "inventory", label: "Proxy inventory" },
            { value: "leases", label: "Active & past leases" },
          ]}
        />
        <button
          className="button primary"
          onClick={() =>
            setEditing(tab === "inventory" ? "inventory" : "policy")
          }
        >
          <Plus size={16} />
          {tab === "inventory" ? "Register inventory" : "New policy"}
        </button>
      </div>
      <ListControls label={tab === "policies" ? "policies" : tab === "inventory" ? "inventory" : "lease sessions"} state={listing} orders={tab === "leases" ? [{value:"newest",label:"Newest first"},{value:"oldest",label:"Oldest first"}] : undefined}>
        {tab === "policies" && <Select aria-label="Policy continuity" value={listing.filters.continuity ?? ""} onChange={(event) => listing.setFilter("continuity", event.target.value)}><option value="">All continuity policies</option>{["none","sticky","strict","fixed","exclusive"].map((value) => <option key={value} value={value}>{label(value)}</option>)}</Select>}
        {tab === "inventory" && <>
          <Select aria-label="Inventory product filter" value={listing.filters.product ?? ""} onChange={(event) => listing.setFilter("product", event.target.value)}><option value="">All products</option>{products.map((value) => <option key={value} value={value}>{label(value)}</option>)}</Select>
          <Select aria-label="Inventory enabled filter" value={listing.filters.enabled ?? ""} onChange={(event) => listing.setFilter("enabled", event.target.value)}><option value="">All states</option><option value="true">Enabled</option><option value="false">Disabled</option></Select>
          <input aria-label="Inventory country filter" maxLength={2} placeholder="Country" value={listing.filters.country ?? ""} onChange={(event) => listing.setFilter("country", event.target.value.toUpperCase())} />
        </>}
        {tab === "leases" && <Select aria-label="Proxy lease status" value={listing.filters.status ?? ""} onChange={(event) => listing.setFilter("status", event.target.value)}><option value="">All states</option><option value="active">Active</option><option value="released">Released</option><option value="expired">Expired</option></Select>}
      </ListControls>
      <div className="policy-explainer panel">
        <span className="feature-icon">
          <Network size={24} />
        </span>
        <div>
          <h3>Explainable connection selection</h3>
          <p>
            Preserve continuity, enforce eligibility, then compare transport
            health, latency, utilization, and known cost.
          </p>
        </div>
        <span className="pill">Transport evidence only</span>
      </div>
      {tab === "policies" && (
        <>
          <ErrorNotice error={policies.error} />
          {policies.isPending ? (
            <Loading />
          ) : policies.data?.items.length ? (
            <div className="policy-grid">
              {policies.data.items.map((policy) => (
                <section className="panel policy-card" key={policy.id}>
                  <div className="subsection-header">
                    <span className="feature-icon">
                      <ShieldCheck size={20} />
                    </span>
                    <div className="actions">
                      <span className="version-tag">v{policy.version}</span>
                      <button
                        className="text-button"
                        onClick={() => {
                          setPolicyEdit(policy);
                          setEditing("policy");
                        }}
                      >
                        Edit
                      </button>
                    </div>
                  </div>
                  <h3>{policy.name}</h3>
                  <p>
                    {policy.country ?? "Any country"}
                    {policy.fallback_countries.length
                      ? ` · fallback: ${policy.fallback_countries.join(", ")}`
                      : " · no geographic fallback"}
                  </p>
                  <div className="tag-list">
                    {policy.products.map((product) => (
                      <span className="pill" key={product}>
                        {label(product)}
                      </span>
                    ))}
                  </div>
                  <DetailGrid
                    items={[
                      ["Continuity", `${policy.continuity_minutes} min`],
                      [
                        "Exclusivity",
                        policy.exclusive ? "Required" : "Shared eligible",
                      ],
                      ["Recovery", label(policy.failure_policy)],
                      ["Replacement limit", policy.max_replacements],
                    ]}
                  />
                  <div className="card-foot">
                    <Badge
                      value={
                        policy.allow_direct
                          ? "direct permitted"
                          : "proxy required"
                      }
                    />
                    <span className="muted small-text">
                      Immutable policy version
                    </span>
                  </div>
                </section>
              ))}
            </div>
          ) : (
            <section className="panel">
              <Empty
                title="Choose connections deliberately"
                detail="Define required geography, continuity, product families, and connection recovery before creating a batch."
                action={
                  <button
                    className="button secondary"
                    onClick={() => setEditing("policy")}
                  >
                    <Plus size={16} />
                    Create policy
                  </button>
                }
              />
            </section>
          )}
        </>
      )}
      {tab === "inventory" && (
        <section className="panel data-panel">
          <SectionTitle
            title="Oxylabs inventory"
            detail="Provider capacity and transport measurements. Missing measurements remain unknown."
          />
          <ErrorNotice error={inventory.error} />
          {inventory.isPending ? (
            <Loading />
          ) : inventory.data?.items.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Inventory</th>
                    <th>Product / country</th>
                    <th>Status</th>
                    <th>Reserved connections</th>
                    <th>Transport success</th>
                    <th>Latency</th>
                    <th>Cost / GB</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {inventory.data.items.map((item) => (
                    <tr key={item.id}>
                      <td>
                        <strong>{item.name}</strong>
                        <small className="table-subtitle mono">
                          {item.host}:{item.port}
                        </small>
                      </td>
                      <td>
                        {label(item.product)}
                        <small className="table-subtitle">{item.country}</small>
                      </td>
                      <td>
                        <Badge
                          value={
                            item.status ??
                            (item.enabled ? "enabled" : "disabled")
                          }
                        />
                        <small className="table-subtitle">{humanValue(item.readiness?.status)}</small>
                        {item.readiness?.reason != null && <small className="table-subtitle">{humanValue(item.readiness.reason)}</small>}
                      </td>
                      <td>
                        {item.active_leases ?? item.active_connections ?? 0} /{" "}
                        {item.capacity}
                      </td>
                      <td>
                        {pct(
                          item.transport_success_rate ??
                            (typeof item.health?.success_rate === "number"
                              ? item.health.success_rate
                              : null),
                        )}
                      </td>
                      <td>
                        {ms(
                          item.latency_ms ??
                            (typeof item.health?.latency_ms === "number"
                              ? item.health.latency_ms
                              : null),
                        )}
                      </td>
                      <td>
                        {item.cost_per_gb == null
                          ? "Unknown"
                          : `${item.cost_per_gb} ${item.cost_currency ?? "(currency unknown)"}`}
                      </td>
                      <td>
                        <button
                          className="text-button"
                          disabled={(item.active_leases ?? 0) > 0}
                          onClick={() => {
                            setInventoryEdit(item);
                            setEditing("inventory");
                          }}
                        >
                          Edit
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title="No proxy inventory configured"
              detail="Register the exact products you have access to. Credentials stay in named environment secrets."
              action={
                <button
                  className="button secondary"
                  onClick={() => setEditing("inventory")}
                >
                  <Plus size={16} />
                  Register inventory
                </button>
              }
            />
          )}
        </section>
      )}
      {tab === "leases" && (
        <section className="panel data-panel">
          <SectionTitle
            title="Proxy leases"
            detail="Every replacement starts a new recorded environment epoch."
          />
          <ErrorNotice error={leases.error} />
          {leases.isPending ? (
            <Loading />
          ) : leases.data?.items.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Session</th>
                    <th>Inventory</th>
                    <th>Status</th>
                    <th>Environment</th>
                    <th>Selection</th>
                  </tr>
                </thead>
                <tbody>
                  {leases.data.items.map((lease) => (
                    <tr key={lease.id}>
                      <td className="mono">{lease.session_id.slice(0, 12)}</td>
                      <td>
                        {inventory.data?.items.find(
                          (i) => i.id === lease.inventory_id,
                        )?.name ?? lease.inventory_id}
                      </td>
                      <td>
                        <Badge value={lease.status} />
                      </td>
                      <td>{lease.epoch ?? lease.environment_epoch ?? "—"}</td>
                      <td>
                        <button
                          className="text-button"
                          onClick={() => setSelected(lease)}
                        >
                          View rationale <ArrowRight size={14} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title="No proxy leases"
              detail="Eligible batch sessions allocate a connection lease and record why that inventory was selected."
            />
          )}
        </section>
      )}
      <Pagination total={(tab === "policies" ? policies.data?.total : tab === "inventory" ? inventory.data?.total : leases.data?.total) ?? 0} offset={listing.offset} limit={50} onChange={listing.setOffset} />
      <div className="context-note">
        <Globe2 size={16} />
        Resource permissions are separate from navigation permissions. Upstream
        DNS resolution remains an explicit trust boundary.
      </div>
      {editing === "policy" && (
        <PolicyEditor
          token={token}
          initial={policyEdit}
          close={() => {
            setEditing(null);
            setPolicyEdit(undefined);
          }}
        />
      )}{" "}
      {editing === "inventory" && (
        <InventoryEditor
          token={token}
          initial={inventoryEdit}
          close={() => {
            setEditing(null);
            setInventoryEdit(undefined);
          }}
        />
      )}{" "}
      {selected && (
        <Modal
          title="Connection selection rationale"
          close={() => setSelected(null)}
        >
          <div className="modal-body">
            <DetailGrid
              items={[
                ["Lease", selected.id],
                ["Session", selected.session_id],
                ["Status", selected.status],
                [
                  "Environment epoch",
                  selected.epoch ?? selected.environment_epoch,
                ],
              ]}
            />
            <pre className="json-preview">
              {typeof (selected.rationale ?? selected.selection_rationale) ===
              "object"
                ? JSON.stringify(
                    selected.rationale ?? selected.selection_rationale,
                    null,
                    2,
                  )
                : humanValue(
                    selected.rationale ?? selected.selection_rationale,
                  )}
            </pre>
          </div>
        </Modal>
      )}
    </div>
  );
}
