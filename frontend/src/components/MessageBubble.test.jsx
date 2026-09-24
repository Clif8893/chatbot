import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import MessageBubble from "./MessageBubble.jsx";
import { mapStoredMessages } from "./ChatbotPage.jsx";


const timestamp = "2026-09-17T10:00:00Z";

afterEach(cleanup);


describe("FAQ image attachments", () => {
  it("renders an accessible image link only when metadata exists", () => {
    render(<MessageBubble message={{
      id: "assistant-1",
      sender: "bot",
      text: "Approved SkillsFuture answer",
      timestamp,
      imageUrl: "/assets/skillsfuture-credit-guide-awaiting-approval.svg",
      imageAlt: "Updated SkillsFuture guide awaiting approval",
    }} />);

    const image = screen.getByRole("img", {
      name: "Updated SkillsFuture guide awaiting approval",
    });
    expect(image.getAttribute("src")).toBe(
      "/assets/skillsfuture-credit-guide-awaiting-approval.svg",
    );
    expect(image.closest("a").getAttribute("target")).toBe("_blank");
  });

  it("renders no image without attachment metadata", () => {
    render(<MessageBubble message={{
      id: "assistant-2",
      sender: "bot",
      text: "Course fee answer",
      timestamp,
    }} />);
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("retains attachment metadata when history is mapped after reload", () => {
    const [message] = mapStoredMessages([{
      id: "assistant-3",
      role: "assistant",
      content: "Approved SkillsFuture answer",
      timestamp,
      image_url: "/assets/skillsfuture-credit-guide-awaiting-approval.svg",
      image_alt: "Updated SkillsFuture guide awaiting approval",
      image_status: "awaiting_updated_client_approval",
    }]);

    expect(message.imageUrl).toBe(
      "/assets/skillsfuture-credit-guide-awaiting-approval.svg",
    );
    expect(message.imageAlt).toBe(
      "Updated SkillsFuture guide awaiting approval",
    );
    expect(message.imageStatus).toBe("awaiting_updated_client_approval");
  });
});
