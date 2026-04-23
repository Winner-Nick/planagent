import { describe, it, expect } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { PlansBoard } from "../pages/PlansBoard";

function renderBoard() {
  return render(
    <MemoryRouter>
      <PlansBoard />
    </MemoryRouter>,
  );
}

describe("PlansBoard", () => {
  it("renders four status columns", async () => {
    renderBoard();
    await waitFor(() =>
      expect(screen.getByTestId("column-draft")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("column-active")).toBeInTheDocument();
    expect(screen.getByTestId("column-completed")).toBeInTheDocument();
    expect(screen.getByTestId("column-paused")).toBeInTheDocument();
  });

  it("places plans in the correct columns", async () => {
    renderBoard();

    await waitFor(() =>
      expect(screen.getByTestId("plan-card-plan_001")).toBeInTheDocument(),
    );

    const draftCol = screen.getByTestId("column-draft");
    const activeCol = screen.getByTestId("column-active");
    const completedCol = screen.getByTestId("column-completed");
    const pausedCol = screen.getByTestId("column-paused");

    expect(
      within(draftCol).getByTestId("plan-card-plan_001"),
    ).toBeInTheDocument();
    expect(
      within(activeCol).getByTestId("plan-card-plan_002"),
    ).toBeInTheDocument();
    expect(
      within(completedCol).getByTestId("plan-card-plan_004"),
    ).toBeInTheDocument();
    expect(
      within(pausedCol).getByTestId("plan-card-plan_005"),
    ).toBeInTheDocument();
  });
});
