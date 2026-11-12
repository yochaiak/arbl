import type { Target, Workflow } from "./api";
import type { components } from "./schema";
type Schemas = components["schemas"];
export type ProfileInput = Schemas["ProfileInput"];
export type ProxyPolicyInput = Required<Schemas["ProxyPolicyInput"]>;
export type ProxyInventoryInput = Schemas["ProxyInventoryInput"] & {
  cost_per_gb: number | null;
};

export interface PageResult<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
export interface Project {
  id: string;
  name: string;
  role: string;
  concurrency: number;
}
export type Adapter = Schemas["AdapterView"] & { id: ProfileInput["adapter"] };
export type Profile = Schemas["ProfileView"] & {
  adapter: ProfileInput["adapter"];
  persistence: ProfileInput["persistence"];
  ownership_epoch?: number;
};
export type BrowserSession = Schemas["SessionView"] & {
  workflow_outcome?: string | null;
  environment_epoch?: number;
};
export type Preparation = Required<Schemas["Preparation"]> & {
  lifetime_seconds: number;
};
export type BatchInput = Omit<
  Schemas["BatchInput"],
  "preparation" | "adapters"
> & {
  preparation: Preparation;
  adapters: NonNullable<Schemas["BatchInput"]["adapters"]>;
};
export type Batch = Schemas["BatchView"] & {
  completed?: number;
  failed?: number;
  definition?: BatchInput;
  progress?: Record<string, number>;
};
export type BatchItem = Schemas["BatchItemView"];
export type ScheduleInput = Omit<Schemas["ScheduleInput"], "batch"> & {
  batch: BatchInput;
};
export type Schedule = Omit<Schemas["ScheduleView"], "batch"> & {
  batch: BatchInput;
  status?: string;
  next_run_at?: string | null;
};
export interface Occurrence {
  id: string;
  scheduled_at?: string;
  scheduled_for?: string;
  reason?: string;
  status: string;
  batch_id?: string;
  created_at: string;
  error?: string;
}
export type ProxyPolicy = Required<Schemas["PolicyView"]>;
export type ProxyInventory = Schemas["InventoryView"] & {
  status?: string;
  active_connections?: number;
  transport_success_rate?: number | null;
  latency_ms?: number | null;
};
export interface SharedProps {
  token: string;
  targets: Target[];
  workflows: Workflow[];
}
export const platformKey = (
  token: string,
  resource: string,
  ...parts: unknown[]
) => ["platform", token, resource, ...parts];
export const sessionActive = (status: string) =>
  [
    "pending",
    "queued",
    "admitted",
    "starting",
    "running",
    "preparing",
    "manual",
    "awaiting_control",
    "awaiting_operator",
    "stopping",
    "paused",
    "needs_reconciliation",
  ].includes(status);
export const numberValue = (value: unknown, fallback = 0): number =>
  typeof value === "number" ? value : fallback;
export function humanValue(value: unknown): string {
  if (value === null || value === undefined) return "Unknown";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function configuredDraft<T extends object>(
  defaults: T,
  initial?: Partial<T>,
): T {
  return Object.fromEntries(
    Object.entries(defaults).map(([key, value]) => [
      key,
      initial && key in initial ? initial[key as keyof T] : value,
    ]),
  ) as T;
}
