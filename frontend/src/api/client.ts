import {
  fixtureConversations,
  fixtureGroups,
  fixturePlans,
  fixtureReminders,
} from "./fixtures";
import type {
  ConversationTurn,
  GroupContext,
  Plan,
  PlanCreate,
  PlanFilter,
  PlanUpdate,
  Reminder,
} from "./types";

const FIXTURE_DELAY_MS = 50;

function useFixtures(): boolean {
  // Default ON so `npm run dev` is self-contained; set VITE_USE_FIXTURES=0
  // to point the UI at a live backend.
  return import.meta.env.VITE_USE_FIXTURES !== "0";
}

function apiBase(): string {
  const base = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";
  return base.replace(/\/$/, "");
}

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => {
    setTimeout(() => resolve(value), FIXTURE_DELAY_MS);
  });
}

async function httpJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${apiBase()}/api/v1${path}`, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    throw new Error(`Request failed: ${res.status} ${res.statusText}`);
  }
  // 204 / empty-body responses (e.g. DELETE) must not hit res.json().
  if (res.status === 204 || res.headers.get("content-length") === "0") {
    return undefined as T;
  }
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

function clonePlans(): Plan[] {
  return fixturePlans.map((p) => ({ ...p, tags: [...p.tags] }));
}

function matchesFilter(plan: Plan, filter?: PlanFilter): boolean {
  if (!filter) return true;
  if (filter.status && plan.status !== filter.status) return false;
  if (filter.owner_id && plan.owner_id !== filter.owner_id) return false;
  if (filter.group_id && plan.group_id !== filter.group_id) return false;
  return true;
}

export const api = {
  plans: {
    async list(filter?: PlanFilter): Promise<Plan[]> {
      if (useFixtures()) {
        return delay(clonePlans().filter((p) => matchesFilter(p, filter)));
      }
      const qs = new URLSearchParams();
      if (filter?.status) qs.set("status", filter.status);
      if (filter?.owner_id) qs.set("owner_id", filter.owner_id);
      if (filter?.group_id) qs.set("group_id", filter.group_id);
      const suffix = qs.toString() ? `?${qs.toString()}` : "";
      return httpJson<Plan[]>(`/plans${suffix}`);
    },
    async get(id: string): Promise<Plan> {
      if (useFixtures()) {
        const hit = clonePlans().find((p) => p.id === id);
        if (!hit) throw new Error(`Plan not found: ${id}`);
        return delay(hit);
      }
      return httpJson<Plan>(`/plans/${encodeURIComponent(id)}`);
    },
    async create(payload: PlanCreate): Promise<Plan> {
      if (useFixtures()) {
        const now = new Date().toISOString();
        const created: Plan = {
          ...payload,
          id: `plan_${Math.random().toString(36).slice(2, 8)}`,
          created_at: now,
          updated_at: now,
        };
        return delay(created);
      }
      return httpJson<Plan>("/plans", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },
    async update(id: string, patch: PlanUpdate): Promise<Plan> {
      if (useFixtures()) {
        const current = clonePlans().find((p) => p.id === id);
        if (!current) throw new Error(`Plan not found: ${id}`);
        const merged: Plan = {
          ...current,
          ...patch,
          id: current.id,
          updated_at: new Date().toISOString(),
        };
        return delay(merged);
      }
      return httpJson<Plan>(`/plans/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
    },
    async delete(id: string): Promise<void> {
      if (useFixtures()) {
        await delay(null);
        return;
      }
      await httpJson<void>(`/plans/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
    },
  },
  groups: {
    async list(): Promise<GroupContext[]> {
      if (useFixtures()) return delay(fixtureGroups.map((g) => ({ ...g })));
      return httpJson<GroupContext[]>("/groups");
    },
    async get(id: string): Promise<GroupContext> {
      if (useFixtures()) {
        const hit = fixtureGroups.find((g) => g.id === id);
        if (!hit) throw new Error(`Group not found: ${id}`);
        return delay({ ...hit });
      }
      return httpJson<GroupContext>(`/groups/${encodeURIComponent(id)}`);
    },
    async conversations(id: string): Promise<ConversationTurn[]> {
      if (useFixtures()) {
        const turns = fixtureConversations[id] ?? [];
        return delay(turns.map((t) => ({ ...t })));
      }
      return httpJson<ConversationTurn[]>(
        `/groups/${encodeURIComponent(id)}/conversations`,
      );
    },
  },
  reminders: {
    async list(planId: string): Promise<Reminder[]> {
      if (useFixtures()) {
        return delay(
          fixtureReminders
            .filter((r) => r.plan_id === planId)
            .map((r) => ({ ...r })),
        );
      }
      return httpJson<Reminder[]>(
        `/plans/${encodeURIComponent(planId)}/reminders`,
      );
    },
  },
};

export type Api = typeof api;
