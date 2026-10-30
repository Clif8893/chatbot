import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App.jsx";
import { createGuestSession, getAuthSession } from "./services/chatApi.js";

vi.mock("./components/ChatbotPage.jsx", () => ({
  default: () => <div>Chat is ready</div>,
}));
vi.mock("./components/enrollment/ParticipantPanel.jsx", () => ({
  default: () => <div>Enrollment panel</div>,
}));
vi.mock("./components/StaffPortal.jsx", () => ({
  default: () => <div>Staff portal</div>,
}));
vi.mock("./services/chatApi.js", () => ({
  createGuestSession: vi.fn(),
  getAuthSession: vi.fn(),
  setCsrfToken: vi.fn(),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
});

describe("participant guest entry", () => {
  it("asks only for a name and opens chat without login credentials", async () => {
    getAuthSession.mockResolvedValue({ authenticated: false, csrf_token: null });
    createGuestSession.mockResolvedValue({
      authenticated: true,
      csrf_token: "guest-csrf",
      user: { id: "guest-1", full_name: "Dhiraj", email: null, guest: true },
    });

    render(<App />);

    const name = await screen.findByRole("textbox", { name: "Your name" });
    expect(screen.queryByLabelText("Email")).toBeNull();
    expect(screen.queryByLabelText("Password")).toBeNull();
    expect(screen.queryByText("Sign in")).toBeNull();

    fireEvent.change(name, { target: { value: "Dhiraj" } });
    fireEvent.click(screen.getByRole("button", { name: "Start chat" }));

    await waitFor(() => expect(createGuestSession).toHaveBeenCalledWith("Dhiraj"));
    expect(await screen.findByText("Chat is ready")).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "Participant chat" }).textContent)
      .toContain("Hi, Dhiraj");
  });
});
