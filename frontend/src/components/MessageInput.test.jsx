import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import MessageInput from "./MessageInput.jsx";


afterEach(cleanup);


describe("MessageInput", () => {
  it("prevents a rapid duplicate submission", () => {
    const onSend = vi.fn();
    render(<MessageInput onSend={onSend} isLoading={false} error="" />);
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Confirm" },
    });
    const form = screen.getByRole("button", { name: "Send" }).closest("form");
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith("Confirm");
  });
});
