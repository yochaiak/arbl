"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { Select } from "./ui";

export function useListState(limit = 50) {
  const [search, updateSearch] = useState("");
  const [order, updateOrder] = useState("newest");
  const [offset, setOffset] = useState(0);
  const [filters, updateFilters] = useState<Record<string, string>>({});
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset), order });
  if (search) params.set("q", search);
  Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
  return { search, order, offset, filters, params: params.toString(), setOffset,
    setSearch: (value: string) => { updateSearch(value); setOffset(0); },
    setOrder: (value: string) => { updateOrder(value); setOffset(0); },
    setFilter: (key: string, value: string) => { updateFilters((previous) => ({ ...previous, [key]: value })); setOffset(0); },
  };
}

export function ListControls({ label, state, children, orders }: {
  label: string;
  state: ReturnType<typeof useListState>;
  children?: ReactNode;
  orders?: Array<{ value: string; label: string }>;
}) {
  return <div className="filter-row">
    <input aria-label={`Search ${label}`} placeholder={`Search ${label}`} maxLength={100} value={state.search} onChange={(event) => state.setSearch(event.target.value)} />
    {children}
    <Select aria-label={`Sort ${label}`} value={state.order} onChange={(event) => state.setOrder(event.target.value)}>
      {(orders ?? [{ value: "newest", label: "Newest first" }, { value: "oldest", label: "Oldest first" }, { value: "name_asc", label: "Name A–Z" }, { value: "name_desc", label: "Name Z–A" }]).map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
    </Select>
  </div>;
}

export function Modal({
  title,
  detail,
  close,
  children,
  wide = false,
}: {
  title: string;
  detail?: string;
  close: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const detailId = `${titleId}-details`;
  useEffect(() => {
    ref.current?.showModal();
    const node = ref.current;
    return () => node?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className={`platform-modal ${wide ? "wide" : ""}`}
      aria-labelledby={titleId}
      aria-describedby={detail ? detailId : undefined}
      onCancel={close}
      onClick={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div className="modal-header">
        <div>
          <h2 id={titleId}>{title}</h2>
          {detail && <p id={detailId}>{detail}</p>}
        </div>
        <button
          className="icon-button"
          aria-label="Close dialog"
          onClick={close}
        >
          <X size={20} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function Pagination({
  total,
  offset,
  limit,
  onChange,
}: {
  total: number;
  offset: number;
  limit: number;
  onChange: (value: number) => void;
}) {
  return (
    <div className="pagination">
      <span>
        {total === 0
          ? "No results"
          : `${offset + 1}–${Math.min(offset + limit, total)} of ${total.toLocaleString()}`}
      </span>
      <div className="actions">
        <button
          className="icon-button"
          aria-label="Previous page"
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          <ChevronLeft size={17} />
        </button>
        <button
          className="icon-button"
          aria-label="Next page"
          disabled={offset + limit >= total}
          onClick={() => onChange(offset + limit)}
        >
          <ChevronRight size={17} />
        </button>
      </div>
    </div>
  );
}
export function DetailGrid({ items }: { items: Array<[string, ReactNode]> }) {
  return (
    <dl className="detail-grid">
      {items.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}
export function Segments<T extends string>({
  value,
  onChange,
  options,
  label,
}: {
  value: T;
  onChange: (value: T) => void;
  options: Array<{ value: T; label: string }>;
  label: string;
}) {
  return (
    <div className="segments" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          type="button"
          key={option.value}
          aria-pressed={value === option.value}
          className={value === option.value ? "selected" : ""}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
