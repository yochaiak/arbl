import type { ReactNode } from "react";
import { Activity, AlertCircle, ChevronDown, FlaskConical } from "lucide-react";
import { label } from "@/lib/api";

export function Brand() {
  return <div className="brand"><span className="brand-mark"><Activity size={22} strokeWidth={2.5} /></span><span>ABRL<small>Browser research lab</small></span></div>;
}

export function Badge({ value, tone }: { value: string; tone?: "green" | "amber" | "rose" | "quiet" }) {
  const normalized = value.toLowerCase();
  const color = tone ?? (
    ["completed", "succeeded", "success", "qualified", "available", "ready", "verified"].includes(normalized) ? "green" :
    ["demo", "simulated", "paused", "pending", "pilot", "running", "queued"].includes(normalized) ? "amber" :
    ["failed", "blocked", "error", "unavailable", "cancelled"].includes(normalized) ? "rose" : "quiet"
  );
  return <span className={`badge ${color}`}><span aria-hidden="true" />{label(value)}</span>;
}

export function Empty({ title, detail, action }: { title: string; detail: string; action?: ReactNode }) {
  return <div className="empty">
    <span className="empty-icon"><FlaskConical size={22} /></span>
    <strong>{title}</strong>
    <p>{detail}</p>
    {action}
  </div>;
}

export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return <div role="alert" className="notice error"><AlertCircle size={17} /><span>{error instanceof Error ? error.message : String(error)}</span></div>;
}

export function Loading() {
  return <p role="status" className="loading"><span className="spinner" /> Loading workspace data…</p>;
}

export function Field({ label: title, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="field"><span>{title}</span>{children}{hint && <small>{hint}</small>}</label>;
}

export function Select({ children, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <span className="select-wrap"><select {...props}>{children}</select><ChevronDown size={15} aria-hidden="true" /></span>;
}

export function SectionTitle({ title, detail, action }: { title: string; detail?: string; action?: ReactNode }) {
  return <div className="section-title"><div><h2>{title}</h2>{detail && <p>{detail}</p>}</div>{action}</div>;
}
