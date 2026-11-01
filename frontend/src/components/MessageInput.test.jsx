import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("previews a screenshot and sends it without requiring text", async () => {
    URL.createObjectURL = vi.fn(() => "blob:test-payment");
    URL.revokeObjectURL = vi.fn();
    const onSend = vi.fn().mockResolvedValue(true);
    const { container } = render(<MessageInput onSend={onSend} isLoading={false} error="" />);
    const file = new File(["image"], "payment.png", { type: "image/png" });
    fireEvent.change(container.querySelector('input[type="file"]'), { target: { files: [file] } });
    expect(screen.getByAltText("Payment screenshot preview")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Screenshot type"), { target: { value: "skillsfuture" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend.mock.calls[0][0]).toBe("");
    expect(onSend.mock.calls[0][1]).toBe(file);
    expect(onSend.mock.calls[0][3]).toBe("skillsfuture");
    await waitFor(() => expect(screen.queryByAltText("Payment screenshot preview")).toBeNull());
  });
});
