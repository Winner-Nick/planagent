import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { Plan, PlanStatus } from "../api/types";
import { Badge } from "../components/ui/badge";
import { Card, CardContent } from "../components/ui/card";
import { cn } from "../lib/utils";
import { formatDate, formatRecurrence, priorityColor } from "../lib/format";

const COLUMNS: { key: PlanStatus; label: string; hint: string }[] = [
  { key: "draft", label: "Draft", hint: "Ideas and proposals" },
  { key: "active", label: "Active", hint: "Running now" },
  { key: "completed", label: "Completed", hint: "Archive" },
  { key: "paused", label: "Paused", hint: "Waiting on something" },
];

export function PlansBoard() {
  const [plans, setPlans] = useState<Plan[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api.plans
      .list()
      .then((data) => {
        if (active) setPlans(data);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      active = false;
    };
  }, []);

  const byStatus = useMemo(() => {
    const buckets: Record<PlanStatus, Plan[]> = {
      draft: [],
      active: [],
      completed: [],
      paused: [],
    };
    if (plans) {
      for (const p of plans) buckets[p.status].push(p);
    }
    return buckets;
  }, [plans]);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-3xl font-semibold tracking-tight">Plans</h2>
        <p className="text-sm text-muted-foreground">
          Everything the agent is tracking across your groups, split by status.
        </p>
      </div>

      {error && (
        <Card>
          <CardContent className="py-6 text-sm text-destructive">
            Failed to load plans: {error}
          </CardContent>
        </Card>
      )}

      <div
        data-testid="plans-board"
        className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4"
      >
        {COLUMNS.map((col) => {
          const items = byStatus[col.key];
          return (
            <section
              key={col.key}
              data-testid={`column-${col.key}`}
              className="flex min-h-[300px] flex-col gap-3 rounded-xl border bg-card/40 p-4"
            >
              <header className="flex items-baseline justify-between">
                <div>
                  <h3 className="text-sm font-semibold tracking-tight">
                    {col.label}
                  </h3>
                  <p className="text-xs text-muted-foreground">{col.hint}</p>
                </div>
                <Badge variant="secondary">{items.length}</Badge>
              </header>
              <div className="flex flex-col gap-3">
                {plans === null && (
                  <div className="h-20 animate-pulse rounded-lg bg-muted/50" />
                )}
                {plans !== null && items.length === 0 && (
                  <p className="rounded-md border border-dashed px-3 py-6 text-center text-xs text-muted-foreground">
                    No plans here yet.
                  </p>
                )}
                {items.map((plan) => (
                  <PlanCard key={plan.id} plan={plan} />
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}

function PlanCard({ plan }: { plan: Plan }) {
  const when = plan.due_at ?? plan.start_at;
  return (
    <Link
      to={`/plans/${plan.id}`}
      data-testid={`plan-card-${plan.id}`}
      className="group block"
    >
      <Card className="transition-colors group-hover:border-primary/60 group-hover:bg-accent/40">
        <CardContent className="space-y-3 p-4">
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
          <p className="line-clamp-2 text-xs text-muted-foreground">
            {plan.description}
          </p>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>{formatDate(when)}</span>
            <span aria-hidden>·</span>
            <span>{formatRecurrence(plan.recurrence)}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">
              {plan.owner_name}
            </span>
            <div className="flex gap-1">
              {plan.tags.slice(0, 2).map((tag) => (
                <Badge key={tag} variant="outline" className="text-[10px]">
                  {tag}
                </Badge>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}
