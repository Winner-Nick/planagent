import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { PlanDetail } from "../pages/PlanDetail";

function renderDetail(id: string) {
  return render(
    <MemoryRouter initialEntries={[`/plans/${id}`]}>
      <Routes>
        <Route path="/plans/:id" element={<PlanDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("PlanDetail", () => {
  it("renders plan fields and reminder timeline", async () => {
    renderDetail("plan_002");

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: /Daily standup reminder/i }),
      ).toBeInTheDocument(),
    );

    const titleInput = screen.getByLabelText(/Title/i) as HTMLInputElement;
    expect(titleInput.value).toBe("Daily standup reminder");

    const description = screen.getByLabelText(
      /Description/i,
    ) as HTMLTextAreaElement;
    expect(description.value.length).toBeGreaterThan(0);

    expect(screen.getByTestId("reminders-upcoming")).toBeInTheDocument();
    expect(screen.getByTestId("reminders-past")).toBeInTheDocument();

    // At least one reminder entry rendered (either upcoming or past bucket).
    await waitFor(() =>
      expect(
        screen.getAllByText(/standup/i).length,
      ).toBeGreaterThan(0),
    );
  });
});
