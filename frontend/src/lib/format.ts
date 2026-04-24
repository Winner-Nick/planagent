import {
  differenceInCalendarDays,
  format,
  formatDistanceToNowStrict,
  isSameDay,
} from "date-fns";
import { zhCN } from "date-fns/locale";

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

// Render a fire_at timestamp as a short human-friendly relative string in
// zh-CN. Examples: "3 分钟后", "今晚 20:00", "明天 10:00", "周五 14:00",
// "2026年6月1日 08:00" (for dates more than a week out).
//
// `now` is injected for deterministic tests.
export function formatRelativeFire(
  iso: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;

  const diffMs = d.getTime() - now.getTime();
  const absMs = Math.abs(diffMs);
  const oneMinute = 60 * 1000;
  const oneHour = 60 * oneMinute;

  // Within the next (or last) hour: "3 分钟后" / "5 分钟前".
  if (absMs < oneHour) {
    const rel = formatDistanceToNowStrict(d, { locale: zhCN, unit: "minute" });
    return diffMs >= 0 ? `${rel}后` : `${rel}前`;
  }

  const hhmm = format(d, "HH:mm");

  // Same calendar day: split morning vs. evening for natural phrasing.
  if (isSameDay(d, now)) {
    const hour = d.getHours();
    if (hour >= 18) return `今晚 ${hhmm}`;
    if (hour < 6) return `凌晨 ${hhmm}`;
    if (hour < 12) return `今早 ${hhmm}`;
    return `今天 ${hhmm}`;
  }

  const dayDelta = differenceInCalendarDays(d, now);

  if (dayDelta === 1) return `明天 ${hhmm}`;
  if (dayDelta === 2) return `后天 ${hhmm}`;
  if (dayDelta === -1) return `昨天 ${hhmm}`;

  // Within this coming week: use weekday name ("周五 14:00").
  if (dayDelta > 0 && dayDelta < 7) {
    return `${format(d, "EEEE", { locale: zhCN })} ${hhmm}`;
  }

  // Further out: full zh-CN date.
  return format(d, "yyyy年M月d日 HH:mm", { locale: zhCN });
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
