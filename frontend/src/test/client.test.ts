import { describe, it, expect } from "vitest";

import { api } from "../api/client";

describe("api client fixture round-trip", () => {
  it("lists plans and fetches one by id", async () => {
    const plans = await api.plans.list();
    expect(plans.length).toBeGreaterThan(0);

    const first = await api.plans.get(plans[0].id);
    expect(first.id).toBe(plans[0].id);
  });

  it("filters plans by status", async () => {
    const draft = await api.plans.list({ status: "draft" });
    expect(draft.every((p) => p.status === "draft")).toBe(true);

    const active = await api.plans.list({ status: "active" });
    expect(active.every((p) => p.status === "active")).toBe(true);
  });

  it("lists groups and conversations and reminders", async () => {
    const groups = await api.groups.list();
    expect(groups.length).toBeGreaterThan(0);

    const convo = await api.groups.conversations(groups[0].id);
    expect(Array.isArray(convo)).toBe(true);
    expect(convo.length).toBeGreaterThan(0);

    const plans = await api.plans.list();
    const reminders = await api.reminders.list(plans[0].id);
    expect(Array.isArray(reminders)).toBe(true);
  });
});
