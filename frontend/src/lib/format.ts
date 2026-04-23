import type { PlanPriority, PlanRecurrence } from "../api/types";

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatRecurrence(r: PlanRecurrence): string {
  switch (r.kind) {
    case "none":
      return "One-off";
    case "daily":
      return `Daily @ ${r.time}`;
    case "weekly": {
      const days = [
        "Sunday",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
      ];
      return `Weekly · ${days[r.day_of_week] ?? "?"} @ ${r.time}`;
    }
    case "monthly":
      return `Monthly · day ${r.day_of_month} @ ${r.time}`;
  }
}

export function priorityColor(priority: PlanPriority): string {
  switch (priority) {
    case "high":
      return "bg-red-500";
    case "medium":
      return "bg-amber-500";
    case "low":
      return "bg-emerald-500";
  }
}
