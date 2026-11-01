import React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { EnquiryInsights, PendingKnowledge } from "./EnquiryInsights.jsx";

afterEach(cleanup);

it("renders observed enquiry metrics and ad funnel", () => {
  render(<EnquiryInsights analytics={{ conversations: 2, containment_rate: 0.5, escalation_rate: 0.5,
    median_first_response_seconds: 2, top_unanswered_subjects: { Parking: 1 },
    escalations_by_category: { unanswered_question: 1 }, escalation_rates_by_category: { unanswered_question: 0.5 },
    funnel: [{ source: "facebook_ad", ad_id: "ad-123", enquiry: 2, enrolled: 1, paid: 0 }] }} />);
  expect(screen.getByText(/Containment 50.0%/)).toBeTruthy();
  expect(screen.getByText("Parking: 1")).toBeTruthy();
  expect(screen.getByText("unanswered question: 1 (50.0%)")).toBeTruthy();
  expect(screen.getByText("facebook_ad ad-123")).toBeTruthy();
});

it("requires an explicit approval action to promote a pending answer", () => {
  const action = vi.fn();
  render(<PendingKnowledge drafts={[{ id: "draft-1", topic: "Parking", answer: "Ask reception", course_id: "general" }]} action={action} />);
  expect(action).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Approve answer"));
  expect(action).toHaveBeenCalledWith("/api/staff/content", { approve_draft_id: "draft-1" }, expect.any(String), "PATCH");
});
