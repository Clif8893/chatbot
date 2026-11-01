import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import QuickReplies from "./QuickReplies.jsx";


afterEach(cleanup);


describe("QuickReplies", () => {
  it("sends the chosen label as the message", () => {
    const onSelect = vi.fn();
    render(<QuickReplies replies={["Upcoming dates", "Talk to staff"]} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: "Talk to staff" }));
    expect(onSelect).toHaveBeenCalledWith("Talk to staff");
  });

  it("renders one button per suggestion", () => {
    render(<QuickReplies replies={["Fees & funding", "Upcoming dates"]} onSelect={vi.fn()} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons.map((button) => button.textContent)).toEqual(["Fees & funding", "Upcoming dates"]);
  });
});
