import {
  Children,
  cloneElement,
  isValidElement,
  useId,
  type ReactNode,
  type ReactElement,
} from "react";
import { Activity, AlertCircle, ChevronDown, FlaskConical } from "lucide-react";
import { label } from "@/lib/api";

export function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        <Activity size={22} strokeWidth={2.5} />
      </span>
      <span>
        ABRL<small>Browser workflow workspace</small>
      </span>
    </div>
  );
}

export function Badge({
  value,
  tone,
}: {
  value: string;
  tone?: "green" | "amber" | "rose" | "quiet";
}) {
  const normalized = value.toLowerCase();
  const color =
    tone ??
    ([
      "completed",
      "succeeded",
      "success",
      "qualified",
      "available",
      "ready",
      "verified",
      "clean",
    ].includes(normalized)
      ? "green"
      : [
            "demo",
            "simulated",
            "paused",
            "pending",
            "pilot",
            "running",
            "queued",
            "awaiting_operator",
            "admitted",
            "starting",
            "preparing",
          ].includes(normalized)
        ? "amber"
        : ["failed", "creation_failed", "blocked", "error", "unavailable", "cancelled"].includes(
              normalized,
            )
          ? "rose"
          : "quiet");
  return (
    <span className={`badge ${color}`} title={label(value)}>
      <span aria-hidden="true" />
      <span className="badge-copy">{label(value)}</span>
    </span>
  );
}

export function Empty({
  title,
  detail,
  action,
}: {
  title: string;
  detail: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <FlaskConical size={22} />
      </span>
      <strong>{title}</strong>
      <p>{detail}</p>
      {action}
    </div>
  );
}

export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div role="alert" className="notice error">
      <AlertCircle size={17} />
      <span>{error instanceof Error ? error.message : String(error)}</span>
    </div>
  );
}

export function Loading() {
  return (
    <p role="status" className="loading">
      <span className="spinner" /> Loading workspace data…
    </p>
  );
}

export function Field({
  label: title,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{title}</label>
      {Children.map(children, (child) =>
        isValidElement(child) &&
        (["input", "select", "textarea"].includes(String(child.type)) ||
          child.type === Select)
          ? cloneElement(
              child as ReactElement<{
                id: string;
                "aria-describedby"?: string;
              }>,
              { id, "aria-describedby": hint ? `${id}-hint` : undefined },
            )
          : child,
      )}
      {hint && <small id={`${id}-hint`}>{hint}</small>}
    </div>
  );
}

export function Select({
  children,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <span className="select-wrap">
      <select {...props}>{children}</select>
      <ChevronDown size={15} aria-hidden="true" />
    </span>
  );
}

export function SectionTitle({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: string;
  action?: ReactNode;
}) {
  return (
    <div className="section-title">
      <div>
        <h2>{title}</h2>
        {detail && <p>{detail}</p>}
      </div>
      {action}
    </div>
  );
}
