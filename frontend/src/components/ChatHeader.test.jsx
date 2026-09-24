import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ChatHeader from "./ChatHeader.jsx";


afterEach(cleanup);


describe("ChatHeader", () => {
  it("exposes separate Previous Chats and New Chat controls", () => {
    const onToggleHistory = vi.fn();
    const onNewChat = vi.fn();
    render(
      <ChatHeader
        onToggleHistory={onToggleHistory}
        onNewChat={onNewChat}
        historyOpen={false}
        disabled={false}
      />,
    );

    const history = screen.getByRole("button", { name: "Previous Chats" });
    expect(history.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(history);
    fireEvent.click(screen.getByRole("button", { name: "New Chat" }));

    expect(onToggleHistory).toHaveBeenCalledTimes(1);
    expect(onNewChat).toHaveBeenCalledTimes(1);
  });
});
