import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "../api/client";
import type { Plan, Reminder } from "../api/types";
import { PlansBoard } from "../pages/PlansBoard";
import { CHENCHEN, PENG } from "../lib/users";

function renderBoard() {
  return render(
    <MemoryRouter>
      <PlansBoard />
    </MemoryRouter>,
  );
}

describe("PlansBoard (fixtures)", () => {
  it("renders owner columns for 鹏鹏 and 辰辰", async () => {
    renderBoard();
    await waitFor(() =>
      expect(
        screen.getByTestId(`owner-column-${PENG.displayName}`),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByTestId(`owner-column-${CHENCHEN.displayName}`),
    ).toBeInTheDocument();
  });

  it("renders plan cards from fixture data", async () => {
    renderBoard();
    await waitFor(() =>
      expect(screen.getByTestId("plan-card-plan_001")).toBeInTheDocument(),
    );
    // All fixture plans should land in one of the owner columns without
    // crashing, even though their owners aren't 鹏鹏/辰辰.
    for (const id of [
      "plan_001",
      "plan_002",
      "plan_003",
      "plan_004",
      "plan_005",
    ]) {
      expect(screen.getByTestId(`plan-card-${id}`)).toBeInTheDocument();
    }
  });
});

function makePlan(partial: Partial<Plan> & Pick<Plan, "id" | "owner_id" | "owner_name" | "status">): Plan {
  return {
    title: partial.title ?? `Plan ${partial.id}`,
    description: "",
    status: partial.status,
    priority: partial.priority ?? "medium",
    owner_id: partial.owner_id,
    owner_name: partial.owner_name,
    group_id: partial.group_id ?? null,
    start_at: partial.start_at ?? null,
    due_at: partial.due_at ?? null,
    recurrence: partial.recurrence ?? { kind: "none" },
    tags: partial.tags ?? [],
    created_at: "2026-04-20T00:00:00Z",
    updated_at: "2026-04-22T00:00:00Z",
    id: partial.id,
  };
}

describe("PlansBoard (live-mode grouping)", () => {
  // Use `any` here: vi.spyOn's generic return type conflicts with strict TS
  // when we store the handle and later call mockRestore(). The runtime
  // contract is fine.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let listSpy: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let remSpy: any;

  beforeEach(() => {
    const plans: Plan[] = [
      makePlan({
        id: "p_peng_active",
        owner_id: PENG.wechatUserId,
        owner_name: PENG.displayName,
        status: "active",
        title: "鹏鹏 active plan",
      }),
      makePlan({
        id: "p_chen_overdue",
        owner_id: CHENCHEN.wechatUserId,
        owner_name: CHENCHEN.displayName,
        status: "overdue",
        title: "辰辰 overdue plan",
      }),
      makePlan({
        id: "p_other_draft",
        owner_id: "o_unknown@im.wechat",
        owner_name: "陌生人",
        status: "draft",
        title: "Unknown owner plan",
      }),
    ];
    listSpy = vi.spyOn(api.plans, "list").mockResolvedValue(plans);
    remSpy = vi.spyOn(api.reminders, "list").mockResolvedValue([] as Reminder[]);
  });

  afterEach(() => {
    listSpy.mockRestore();
    remSpy.mockRestore();
  });

  it("groups three owners with an overdue badge on the overdue plan", async () => {
    renderBoard();

    await waitFor(() =>
      expect(screen.getByTestId("plan-card-p_peng_active")).toBeInTheDocument(),
    );

    // Three distinct owner columns: 鹏鹏, 辰辰, and the fallback for the
    // unknown wechat id (uses owner_name verbatim).
    const peng = screen.getByTestId(`owner-column-${PENG.displayName}`);
    const chen = screen.getByTestId(`owner-column-${CHENCHEN.displayName}`);
    const other = screen.getByTestId("owner-column-陌生人");
    expect(peng).toBeInTheDocument();
    expect(chen).toBeInTheDocument();
    expect(other).toBeInTheDocument();

    // Each plan lives in the right owner column.
    expect(
      within(peng).getByTestId("plan-card-p_peng_active"),
    ).toBeInTheDocument();
    expect(
      within(chen).getByTestId("plan-card-p_chen_overdue"),
    ).toBeInTheDocument();
    expect(
      within(other).getByTestId("plan-card-p_other_draft"),
    ).toBeInTheDocument();

    // The Overdue lane renders under 辰辰's column, and the card shows the
    // 已逾期 badge.
    expect(
      within(chen).getByTestId(`lane-${CHENCHEN.displayName}-overdue`),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("badge-overdue-p_chen_overdue"),
    ).toBeInTheDocument();
  });
});
