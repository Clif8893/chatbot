import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import QuickActions from "./QuickActions.jsx";


afterEach(cleanup);


describe("QuickActions", () => {
  it("sends the button's message together with its id", () => {
    const onSelect = vi.fn();
    render(
      <QuickActions
        actions={[{ id: "fee", label: "Course fee", message: "How much is the course fee?" }]}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Course fee" }));
    expect(onSelect).toHaveBeenCalledWith("How much is the course fee?", "fee");
  });
});
