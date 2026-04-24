import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { RefreshCw } from "lucide-react";

import { api } from "../api/client";
import type { Plan, PlanStatus, Reminder } from "../api/types";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent } from "../components/ui/card";
import { cn } from "../lib/utils";
import { formatRecurrence, formatRelativeFire, priorityColor } from "../lib/format";
import { OWNER_COLUMN_ORDER, resolveOwnerKey } from "../lib/users";

const POLL_INTERVAL_MS = 30_000;

type LaneKey = PlanStatus | "other";

interface Lane {
  key: LaneKey;
  label: string;
  accent: string;
}

const LANES: Lane[] = [
  { key: "draft", label: "Draft", accent: "bg-slate-500" },
  { key: "active", label: "Active", accent: "bg-emerald-500" },
  { key: "overdue", label: "Overdue", accent: "bg-red-500" },
  { key: "completed", label: "Completed", accent: "bg-zinc-400" },
  { key: "paused", label: "Paused", accent: "bg-amber-500" },
  { key: "other", label: "其他", accent: "bg-purple-500" },
];

function laneFor(status: PlanStatus | string): LaneKey {
  const known: LaneKey[] = [
    "draft",
    "active",
    "overdue",
    "completed",
    "paused",
  ];
  return (known as string[]).includes(status)
    ? (status as LaneKey)
    : "other";
}

function apiBaseDisplay(): string {
  const base = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";
  return base.replace(/\/$/, "");
}

function usingFixtures(): boolean {
  return import.meta.env.VITE_USE_FIXTURES === "1";
}

export function PlansBoard() {
  const [plans, setPlans] = useState<Plan[] | null>(null);
  const [nextFireByPlan, setNextFireByPlan] = useState<Record<string, string>>(
    {},
  );
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [lastLoadedAt, setLastLoadedAt] = useState<Date | null>(null);
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setRefreshing(true);
    try {
      const data = await api.plans.list();
      setPlans(data);
      setError(null);
      setLastLoadedAt(new Date());

      // Fetch next-fire reminders for each plan in parallel. Failures on a
      // single plan's reminder list shouldn't poison the whole board.
      const reminderPairs = await Promise.all(
        data.map(async (p) => {
          try {
            const rems = await api.reminders.list(p.id);
            return [p.id, rems] as const;
          } catch {
            return [p.id, [] as Reminder[]] as const;
          }
        }),
      );
      const nowMs = Date.now();
      const next: Record<string, string> = {};
      for (const [planId, rems] of reminderPairs) {
        const upcoming = rems
          .filter((r) => r.status === "scheduled")
          .map((r) => ({ r, t: new Date(r.fire_at).getTime() }))
          .filter((x) => !Number.isNaN(x.t) && x.t >= nowMs)
          .sort((a, b) => a.t - b.t);
        if (upcoming.length > 0) next[planId] = upcoming[0].r.fire_at;
      }
      setNextFireByPlan(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      inFlight.current = false;
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Auto-poll every 30s when the tab is visible. Pause when hidden so we
  // don't thrash the backend with a stale browser tab.
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    function start() {
      if (timer !== null) return;
      timer = setInterval(() => {
        void load();
      }, POLL_INTERVAL_MS);
    }
    function stop() {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    }
    function onVisibility() {
      if (document.visibilityState === "visible") {
        void load();
        start();
      } else {
        stop();
      }
    }
    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
    };
  }, [load]);

  const ownerColumns = useMemo(() => {
    // Preserve 鹏鹏 / 辰辰 order, then append any "other" owners seen.
    const seen = new Set<string>();
    const order: string[] = [...OWNER_COLUMN_ORDER];
    for (const name of OWNER_COLUMN_ORDER) seen.add(name);
    if (plans) {
      for (const p of plans) {
        const key = resolveOwnerKey(p);
        if (!seen.has(key)) {
          seen.add(key);
          order.push(key);
        }
      }
    }
    return order;
  }, [plans]);

  const grouped = useMemo(() => {
    const out: Record<string, Record<LaneKey, Plan[]>> = {};
    for (const owner of ownerColumns) {
      out[owner] = {
        draft: [],
        active: [],
        overdue: [],
        completed: [],
        paused: [],
        other: [],
      };
    }
    if (plans) {
      for (const p of plans) {
        const owner = resolveOwnerKey(p);
        if (!out[owner]) {
          out[owner] = {
            draft: [],
            active: [],
            overdue: [],
            completed: [],
            paused: [],
            other: [],
          };
        }
        out[owner][laneFor(p.status)].push(p);
      }
    }
    return out;
  }, [plans, ownerColumns]);

  const banner = error && !usingFixtures() ? (
    <Card data-testid="backend-error" className="border-destructive/40">
      <CardContent className="py-4 text-sm text-destructive">
        后端未连接（{apiBaseDisplay()}）
        <span className="block text-xs text-muted-foreground">
          {error}
        </span>
      </CardContent>
    </Card>
  ) : null;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-3xl font-semibold tracking-tight">Plans</h2>
          <p className="text-sm text-muted-foreground">
            {usingFixtures()
              ? "Fixture mode — offline demo data."
              : `Live · ${apiBaseDisplay()}`}
            {lastLoadedAt && (
              <>
                {" · "}
                <span>updated {lastLoadedAt.toLocaleTimeString()}</span>
              </>
            )}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            void load();
          }}
          disabled={refreshing}
          data-testid="refresh-button"
          aria-label="Refresh"
        >
          <RefreshCw
            className={cn(
              "mr-2 h-3.5 w-3.5",
              refreshing && "animate-spin",
            )}
          />
          {refreshing ? "Refreshing" : "Refresh"}
        </Button>
      </div>

      {banner}

      <div
        data-testid="plans-board"
        className={cn(
          "grid grid-cols-1 gap-6",
          ownerColumns.length >= 2 && "lg:grid-cols-2",
          ownerColumns.length >= 3 && "xl:grid-cols-3",
        )}
      >
        {ownerColumns.map((owner) => (
          <OwnerColumn
            key={owner}
            owner={owner}
            lanes={grouped[owner]}
            plans={plans}
            nextFireByPlan={nextFireByPlan}
          />
        ))}
      </div>
    </div>
  );
}

function OwnerColumn({
  owner,
  lanes,
  plans,
  nextFireByPlan,
}: {
  owner: string;
  lanes: Record<LaneKey, Plan[]>;
  plans: Plan[] | null;
  nextFireByPlan: Record<string, string>;
}) {
  const total = Object.values(lanes).reduce((acc, v) => acc + v.length, 0);
  return (
    <section
      data-testid={`owner-column-${owner}`}
      className="flex min-h-[300px] flex-col gap-4 rounded-xl border bg-card/40 p-4"
    >
      <header className="flex items-baseline justify-between">
        <h3 className="text-lg font-semibold tracking-tight">{owner}</h3>
        <Badge variant="secondary">{total}</Badge>
      </header>
      <div className="flex flex-col gap-4">
        {LANES.map((lane) => {
          const items = lanes[lane.key];
          if (items.length === 0 && lane.key !== "active") {
            // Keep "Active" lane visible even when empty so a fresh account
            // doesn't look broken; other lanes collapse to save space.
            return null;
          }
          return (
            <div
              key={lane.key}
              data-testid={`lane-${owner}-${lane.key}`}
              className="space-y-2"
            >
              <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-muted-foreground">
                <span
                  className={cn("h-2 w-2 rounded-full", lane.accent)}
                  aria-hidden
                />
                <span className="font-semibold">{lane.label}</span>
                <span>·</span>
                <span>{items.length}</span>
              </div>
              <div className="flex flex-col gap-2">
                {plans === null && (
                  <div className="h-16 animate-pulse rounded-lg bg-muted/50" />
                )}
                {plans !== null && items.length === 0 && (
                  <p className="rounded-md border border-dashed px-3 py-4 text-center text-xs text-muted-foreground">
                    No plans.
                  </p>
                )}
                {items.map((plan) => (
                  <PlanCard
                    key={plan.id}
                    plan={plan}
                    nextFireAt={nextFireByPlan[plan.id]}
                  />
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function PlanCard({
  plan,
  nextFireAt,
}: {
  plan: Plan;
  nextFireAt: string | undefined;
}) {
  const fire = nextFireAt ?? plan.due_at ?? plan.start_at;
  return (
    <Link
      to={`/plans/${plan.id}`}
      data-testid={`plan-card-${plan.id}`}
      className="group block"
    >
      <Card className="transition-colors group-hover:border-primary/60 group-hover:bg-accent/40">
        <CardContent className="space-y-2 p-3">
          <div className="flex items-start gap-2">
            <span
              aria-label={`priority ${plan.priority}`}
              className={cn(
                "mt-1.5 h-2 w-2 shrink-0 rounded-full",
                priorityColor(plan.priority),
              )}
            />
            <h4 className="text-sm font-medium leading-snug">{plan.title}</h4>
          </div>
          {plan.status === "overdue" && (
            <Badge
              data-testid={`badge-overdue-${plan.id}`}
              variant="outline"
              className="border-red-500/60 text-red-600 dark:text-red-400"
            >
              已逾期
            </Badge>
          )}
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <span className="font-medium text-foreground/80">
              下一次 {formatRelativeFire(fire)}
            </span>
            <span aria-hidden>·</span>
            <span>{formatRecurrence(plan.recurrence)}</span>
          </div>
          {plan.tags.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {plan.tags.slice(0, 2).map((tag) => (
                <Badge key={tag} variant="outline" className="text-[10px]">
                  {tag}
                </Badge>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </Link>
  );
}
