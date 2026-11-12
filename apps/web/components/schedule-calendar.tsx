"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import type { Schedule } from "@/lib/platform";
import { Empty, SectionTitle } from "./ui";

const zone = "Asia/Jerusalem";
function localDay(value: string | Date) {
  const parts = new Intl.DateTimeFormat("en", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(value));
  return ["year", "month", "day"].map((part) => parts.find((item) => item.type === part)!.value).join("-");
}
function shift(day: string, days: number) {
  const date = new Date(day + "T12:00:00Z");
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}
function startOfWeek(day: string) {
  return shift(day, -new Date(day + "T12:00:00Z").getUTCDay());
}
function dayLabel(day: string) {
  return new Intl.DateTimeFormat("en", { timeZone: "UTC", weekday: "long", month: "short", day: "numeric", year: "numeric" }).format(new Date(day + "T12:00:00Z"));
}

export function ScheduleCalendar({ schedules, total, open }: { schedules: Schedule[]; total: number; open: (schedule: Schedule) => void }) {
  const today = localDay(new Date());
  const [week, setWeek] = useState(() => startOfWeek(today));
  const [selected, setSelected] = useState(today);
  const shouldFocus = useRef(false);
  const buttons = useRef<Record<string, HTMLButtonElement | null>>({});
  const dates = Array.from({ length: 7 }, (_, index) => shift(week, index));
  const events = schedules.filter((schedule) => schedule.enabled && schedule.next_fire_at);
  const occurrences = (date: string) => events.filter((schedule) => localDay(schedule.next_fire_at!) === date);
  useEffect(() => {
    if (shouldFocus.current) { buttons.current[selected]?.focus(); shouldFocus.current = false; }
  }, [selected, week]);
  function navigate(day: string, focus = false) {
    shouldFocus.current = focus;
    setSelected(day); setWeek(startOfWeek(day));
  }
  function keyboard(event: KeyboardEvent<HTMLButtonElement>, date: string) {
    const offset = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[event.key];
    if (offset !== undefined) { event.preventDefault(); navigate(shift(date, offset), true); }
    if (event.key === "Home" || event.key === "End") { event.preventDefault(); navigate(shift(startOfWeek(date), event.key === "Home" ? 0 : 6), true); }
  }
  return <section className="panel data-panel schedule-calendar" aria-label="Upcoming schedule calendar">
    <SectionTitle title="Upcoming week" detail={`Persisted next execution only, for ${schedules.length} schedules on this filtered page (${total} matching). Paused schedules are excluded. Display timezone: ${zone}.`} />
    <div className="calendar-toolbar">
      <button className="icon-button" aria-label="Previous calendar week" onClick={() => navigate(shift(selected, -7))}><ChevronLeft size={17} /></button>
      <strong aria-live="polite">{dayLabel(week)} – {dayLabel(shift(week, 6))}</strong>
      <button className="icon-button" aria-label="Next calendar week" onClick={() => navigate(shift(selected, 7))}><ChevronRight size={17} /></button>
      <button className="button secondary small" onClick={() => navigate(today)}>This week</button>
    </div>
    <div className="calendar-days" role="group" aria-label="Calendar days">
      {dates.map((date) => <button key={date} ref={(button) => { buttons.current[date] = button; }} className={`calendar-day ${date === today ? "today" : ""}`} aria-pressed={date === selected} aria-label={`${dayLabel(date)}, ${occurrences(date).length} scheduled`} tabIndex={date === selected ? 0 : -1} onClick={() => setSelected(date)} onKeyDown={(event) => keyboard(event, date)}>
        <span>{new Intl.DateTimeFormat("en", { weekday: "short", timeZone: "UTC" }).format(new Date(date + "T12:00:00Z"))}</span>
        <strong>{Number(date.slice(-2))}</strong>
        <small>{occurrences(date).length || "—"}</small>
      </button>)}
    </div>
    <div className="calendar-agenda" aria-live="polite">
      <h3>{dayLabel(selected)}</h3>
      {occurrences(selected).length ? <ul>{occurrences(selected).map((schedule) => <li key={schedule.id}>
        <time dateTime={schedule.next_fire_at!}>{new Intl.DateTimeFormat("en", { timeZone: zone, hour: "2-digit", minute: "2-digit" }).format(new Date(schedule.next_fire_at!))}</time>
        <button className="text-button" onClick={() => open(schedule)}>{schedule.name}</button>
        <small className="muted">Schedule timezone: {schedule.timezone}</small>
      </li>)}</ul> : <Empty title="No next executions on this day" detail="This view shows the next stored occurrence for the current filtered page, not every future repeat." />}
    </div>
  </section>;
}
