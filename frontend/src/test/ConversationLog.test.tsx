import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ConversationLog } from "../pages/ConversationLog";

function renderLog(id: string) {
  return render(
    <MemoryRouter initialEntries={[`/groups/${id}`]}>
      <Routes>
        <Route path="/groups/:id" element={<ConversationLog />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ConversationLog", () => {
  it("renders user and assistant bubbles", async () => {
    renderLog("group_product");

    await waitFor(() =>
      expect(screen.getAllByTestId("bubble-user").length).toBeGreaterThan(0),
    );
    expect(screen.getAllByTestId("bubble-assistant").length).toBeGreaterThan(0);
  });

  it("expands a tool-call block on click", async () => {
    const user = userEvent.setup();
    renderLog("group_product");

    const toolCall = await screen.findByTestId("tool-call-call_001");
    const trigger = toolCall.querySelector("button");
    expect(trigger).not.toBeNull();
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await user.click(trigger!);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    // Arguments JSON is rendered after expansion.
    expect(toolCall.textContent).toContain("plan_id");
  });
});
