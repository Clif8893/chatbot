import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ChatbotPage from "./ChatbotPage.jsx";
import {
  createConversation,
  getConversationMessages,
  listConversations,
  sendChatMessage,
} from "../services/chatApi.js";

vi.mock("../services/chatApi.js", () => ({
  apiBaseUrl: "",
  createConversation: vi.fn(),
  getConversationMessages: vi.fn(),
  listConversations: vi.fn(),
  sendChatMessage: vi.fn(),
  uploadEvidence: vi.fn(),
}));

const menu = ["Fees & funding", "Upcoming dates", "Talk to staff"];

function reply(content, quickReplies = []) {
  return {
    conversation_id: "conv-1",
    message: { id: `bot-${content}`, role: "assistant", content, timestamp: "2026-10-30T09:00:00Z" },
    enrollment: { status: null },
    quick_replies: quickReplies,
  };
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  listConversations.mockResolvedValue({ conversations: [] });
  createConversation.mockResolvedValue({ conversation_id: "conv-1", quick_replies: menu });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
});

describe("ChatbotPage quick replies", () => {
  it("shows the starter menu from a new conversation", async () => {
    render(<ChatbotPage userId="user-1" />);
    expect(await screen.findByRole("button", { name: "Talk to staff" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Enroll now" })).toBeNull();
  });

  it("sends a chosen suggestion as plain text and shows the reply's suggestions", async () => {
    sendChatMessage.mockResolvedValue(reply("The course fee is S$600 nett.", ["Upcoming dates"]));
    render(<ChatbotPage userId="user-1" />);

    fireEvent.click(await screen.findByRole("button", { name: "Fees & funding" }));

    await waitFor(() => expect(sendChatMessage).toHaveBeenCalledWith("Fees & funding", "conv-1"));
    expect(await screen.findByText("The course fee is S$600 nett.")).toBeTruthy();
    const suggestions = screen.getByRole("navigation", { name: "Suggested replies" });
    expect(suggestions.textContent).toBe("Upcoming dates");
  });

  it("shows Calculate funding after a fee and funding answer", async () => {
    sendChatMessage.mockResolvedValue(reply(
      "The course fee is S$600 nett.",
      ["Calculate funding", "Upcoming dates"],
    ));
    render(<ChatbotPage userId="user-1" />);

    fireEvent.click(await screen.findByRole("button", { name: "Fees & funding" }));

    expect(await screen.findByRole("button", { name: "Calculate funding" })).toBeTruthy();
  });

  it("hides suggestions when the reply has none", async () => {
    sendChatMessage.mockResolvedValue(reply("Please choose a course."));
    render(<ChatbotPage userId="user-1" />);

    fireEvent.click(await screen.findByRole("button", { name: "Upcoming dates" }));

    expect(await screen.findByText("Please choose a course.")).toBeTruthy();
    expect(screen.queryByRole("navigation", { name: "Suggested replies" })).toBeNull();
  });

  it("restores the suggestions saved with a reopened chat", async () => {
    localStorage.setItem("qm-training-conversation-id:user-1", "conv-9");
    getConversationMessages.mockResolvedValue({
      messages: [],
      enrollment: {},
      quick_replies: menu,
    });
    render(<ChatbotPage userId="user-1" />);
    expect(await screen.findByRole("button", { name: "Upcoming dates" })).toBeTruthy();
    expect(createConversation).not.toHaveBeenCalled();
  });
});

describe("ChatbotPage follow-up reminders", () => {
  afterEach(() => vi.useRealTimers());

  it("shows a reminder the server posts while the chat is open", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const question = { id: "m1", role: "user", content: "How much is the course?", timestamp: "2026-10-30T09:00:00Z" };
    const answer = { id: "m2", role: "assistant", content: "It is S$600 nett.", timestamp: "2026-10-30T09:00:01Z" };
    const reminder = { id: "m3", role: "assistant", content: "Just checking in on your enquiry.", timestamp: "2026-10-30T09:31:00Z" };
    localStorage.setItem("qm-training-conversation-id:user-1", "conv-9");
    getConversationMessages
      .mockResolvedValueOnce({ messages: [question, answer], enrollment: {}, quick_replies: [] })
      .mockResolvedValue({ messages: [question, answer, reminder], enrollment: {}, quick_replies: [] });
    render(<ChatbotPage userId="user-1" />);
    expect(await screen.findByText("It is S$600 nett.")).toBeTruthy();
    expect(screen.queryByText("Just checking in on your enquiry.")).toBeNull();

    await vi.advanceTimersByTimeAsync(60_000);

    expect(await screen.findByText("Just checking in on your enquiry.")).toBeTruthy();
  });

  it("keeps the greeting of a new chat when a reminder arrives", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    sendChatMessage.mockResolvedValue(reply("It is S$600 nett."));
    render(<ChatbotPage userId="user-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Fees & funding" }));
    expect(await screen.findByText("It is S$600 nett.")).toBeTruthy();
    getConversationMessages.mockResolvedValue({
      messages: [
        { id: "m1", role: "user", content: "Fees & funding", timestamp: "2026-10-30T09:00:00Z" },
        { id: "bot-It is S$600 nett.", role: "assistant", content: "It is S$600 nett.", timestamp: "2026-10-30T09:00:01Z" },
        { id: "m3", role: "assistant", content: "Just checking in on your enquiry.", timestamp: "2026-10-30T09:31:00Z" },
      ],
      enrollment: {},
      quick_replies: [],
    });

    await vi.advanceTimersByTimeAsync(60_000);

    expect(await screen.findByText("Just checking in on your enquiry.")).toBeTruthy();
    expect(screen.getByText(/I’m the Q&M Training Assistant/)).toBeTruthy();
  });
});
