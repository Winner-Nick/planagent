import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChevronLeft, ChevronDown, ChevronRight } from "lucide-react";

import { api } from "../api/client";
import type {
  Plan,
  PlanPriority,
  PlanStatus,
  Reminder,
} from "../api/types";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Textarea } from "../components/ui/textarea";
import { cn } from "../lib/utils";
import { formatDate, formatRecurrence, priorityColor } from "../lib/format";

const STATUSES: PlanStatus[] = ["draft", "active", "completed", "paused"];
const PRIORITIES: PlanPriority[] = ["low", "medium", "high"];

export function PlanDetail() {
  const { id } = useParams<{ id: string }>();
  const [plan, setPlan] = useState<Plan | null>(null);
  const [draft, setDraft] = useState<Plan | null>(null);
  const [reminders, setReminders] = useState<Reminder[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let active = true;
    setError(null);
    setPlan(null);
    setDraft(null);
    setReminders(null);
    Promise.all([api.plans.get(id), api.reminders.list(id)])
      .then(([p, r]) => {
        if (!active) return;
        setPlan(p);
        setDraft(p);
        setReminders(r);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      active = false;
    };
  }, [id]);

  const dirty = useMemo(() => {
    if (!plan || !draft) return false;
    return (
      plan.title !== draft.title ||
      plan.description !== draft.description ||
      plan.status !== draft.status ||
      plan.priority !== draft.priority ||
      plan.owner_name !== draft.owner_name
    );
  }, [plan, draft]);

  async function onSave() {
    if (!plan || !draft) return;
    setSaving(true);
    try {
      const updated = await api.plans.update(plan.id, {
        title: draft.title,
        description: draft.description,
        status: draft.status,
        priority: draft.priority,
        owner_name: draft.owner_name,
      });
      setPlan(updated);
      setDraft(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  if (error) {
    return (
      <div className="space-y-4">
        <BackLink />
        <Card>
          <CardContent className="py-6 text-sm text-destructive">
            {error}
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!plan || !draft) {
    return (
      <div className="space-y-4">
        <BackLink />
        <div className="h-64 animate-pulse rounded-xl bg-muted/40" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <BackLink />
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "h-2 w-2 rounded-full",
                priorityColor(plan.priority),
              )}
            />
            <h2 className="text-2xl font-semibold tracking-tight">
              {plan.title}
            </h2>
          </div>
          <p className="text-sm text-muted-foreground">
            Updated {formatDate(plan.updated_at)}
          </p>
        </div>
        <Button onClick={onSave} disabled={!dirty || saving}>
          {saving ? "Saving…" : "Save"}
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[2fr_1fr]">
        <Card>
          <CardHeader>
            <CardTitle>Plan</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="title">Title</Label>
              <Input
                id="title"
                value={draft.title}
                onChange={(e) =>
                  setDraft({ ...draft, title: e.target.value })
                }
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="description">Description</Label>
              <Textarea
                id="description"
                rows={5}
                value={draft.description}
                onChange={(e) =>
                  setDraft({ ...draft, description: e.target.value })
                }
              />
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <div className="space-y-1.5">
                <Label>Status</Label>
                <div className="flex flex-wrap gap-1.5">
                  {STATUSES.map((s) => (
                    <button
                      type="button"
                      key={s}
                      onClick={() => setDraft({ ...draft, status: s })}
                      className={cn(
                        "rounded-full border px-2.5 py-1 text-xs capitalize transition-colors",
                        draft.status === s
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-input text-muted-foreground hover:text-foreground",
                      )}
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>
              <div className="space-y-1.5">
                <Label>Priority</Label>
                <div className="flex flex-wrap gap-1.5">
                  {PRIORITIES.map((p) => (
                    <button
                      type="button"
                      key={p}
                      onClick={() => setDraft({ ...draft, priority: p })}
                      className={cn(
                        "rounded-full border px-2.5 py-1 text-xs capitalize transition-colors",
                        draft.priority === p
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-input text-muted-foreground hover:text-foreground",
                      )}
                    >
                      {p}
                    </button>
                  ))}
                </div>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="owner">Owner</Label>
                <Input
                  id="owner"
                  value={draft.owner_name}
                  onChange={(e) =>
                    setDraft({ ...draft, owner_name: e.target.value })
                  }
                />
              </div>
            </div>
            <dl className="grid grid-cols-2 gap-4 border-t pt-4 text-sm">
              <MetaRow label="Start">{formatDate(plan.start_at)}</MetaRow>
              <MetaRow label="Due">{formatDate(plan.due_at)}</MetaRow>
              <MetaRow label="Recurrence">
                {formatRecurrence(plan.recurrence)}
              </MetaRow>
              <MetaRow label="Group">{plan.group_id ?? "—"}</MetaRow>
            </dl>
            <div className="flex flex-wrap gap-1.5 pt-2">
              {plan.tags.map((t) => (
                <Badge key={t} variant="secondary">
                  {t}
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>

        <ReminderTimeline reminders={reminders} />
      </div>
    </div>
  );
}

function MetaRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="text-sm">{children}</dd>
    </div>
  );
}

function BackLink() {
  return (
    <Link
      to="/"
      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
    >
      <ChevronLeft className="h-3 w-3" /> Back to plans
    </Link>
  );
}

function ReminderTimeline({
  reminders,
}: {
  reminders: Reminder[] | null;
}) {
  const [showPast, setShowPast] = useState(false);

  const now = Date.now();
  const sorted = (reminders ?? [])
    .slice()
    .sort(
      (a, b) => new Date(a.fire_at).getTime() - new Date(b.fire_at).getTime(),
    );
  const upcoming = sorted.filter(
    (r) => new Date(r.fire_at).getTime() >= now,
  );
  const past = sorted
    .filter((r) => new Date(r.fire_at).getTime() < now)
    .reverse();

  return (
    <Card>
      <CardHeader>
        <CardTitle>Reminders</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <section
          data-testid="reminders-upcoming"
          className="space-y-2"
          aria-label="Upcoming reminders"
        >
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Upcoming
          </h4>
          {reminders === null && (
            <div className="h-16 animate-pulse rounded-md bg-muted/40" />
          )}
          {reminders !== null && upcoming.length === 0 && (
            <p className="text-xs text-muted-foreground">No upcoming reminders.</p>
          )}
          <ul className="space-y-2">
            {upcoming.map((r) => (
              <ReminderRow key={r.id} reminder={r} />
            ))}
          </ul>
        </section>

        <section data-testid="reminders-past" className="space-y-2">
          <button
            type="button"
            onClick={() => setShowPast((v) => !v)}
            className="flex w-full items-center justify-between text-xs font-semibold uppercase tracking-wide text-muted-foreground hover:text-foreground"
          >
            <span>Past ({past.length})</span>
            {showPast ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
          </button>
          {showPast && (
            <ul className="space-y-2">
              {past.map((r) => (
                <ReminderRow key={r.id} reminder={r} faded />
              ))}
            </ul>
          )}
        </section>
      </CardContent>
    </Card>
  );
}

function ReminderRow({
  reminder,
  faded,
}: {
  reminder: Reminder;
  faded?: boolean;
}) {
  return (
    <li
      className={cn(
        "rounded-md border p-3 text-sm",
        faded && "opacity-60",
      )}
    >
      <div className="flex items-center justify-between">
        <span className="font-medium">{formatDate(reminder.fire_at)}</span>
        <Badge variant="outline" className="capitalize">
          {reminder.status}
        </Badge>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{reminder.message}</p>
      <p className="mt-1 text-[10px] uppercase tracking-wide text-muted-foreground">
        via {reminder.channel}
      </p>
    </li>
  );
}
