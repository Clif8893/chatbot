import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PaymentWorkspace from "./PaymentWorkspace.jsx";

const enrollments = [
  { id: "enr-1", full_name: "Aisha Tan", friendly_reference: "ENR-001", course: "Dental Assisting", intake_display: "10 Dec 2026", outstanding_amount: "180.00" },
  { id: "enr-2", full_name: "Ben Lee", friendly_reference: "ENR-002", course: "Front Desk", outstanding_amount: "0.00" },
];
const reviews = [
  { id: "review-1", enrollment_id: "enr-1", evidence_id: "evidence-1", status: "pending_review", reasons: ["recipient_mismatch", "staff_reconciliation_required"], created_at: "2026-10-28T08:00:00Z", extracted: { amount: "120.00", recipient: "Wrong recipient", reference: "BANK1234", transaction_date: "2026-10-27", currency: "SGD", confidence: 0.7, unclear_fields: ["recipient"] }, original_extracted: {}, evidence: { payment_type: "paynow", download_url: "/api/staff/evidence/evidence-1" }, invoice: { number: "INV-001", course_fee: "300.00", skillsfuture_amount: "0.00" }, verified_payments: [], outstanding_amount: "180.00" },
  { id: "review-2", enrollment_id: "enr-2", evidence_id: "evidence-2", status: "clarification_requested", reasons: [], created_at: "2026-10-27T08:00:00Z", extracted: { amount: "300.00" }, evidence: { payment_type: "skillsfuture", download_url: "/api/staff/evidence/evidence-2" }, invoice: { number: "INV-002", course_fee: "300.00" }, verified_payments: [], outstanding_amount: "0.00", decided_by: "accountant", note: "Need claim number" },
];

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => "blob:payment-image");
  URL.revokeObjectURL = vi.fn();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(["image"], { type: "image/png" }) }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("PaymentWorkspace", () => {
  it("filters live review records and loads the selected protected image", async () => {
    render(<PaymentWorkspace reviews={reviews} enrollments={enrollments} action={vi.fn()} />);
    await waitFor(() => expect(screen.getByAltText("Payment screenshot for submission review-1")).toBeTruthy());
    expect(fetch).toHaveBeenCalledWith("/api/staff/evidence/evidence-1", expect.objectContaining({ credentials: "include" }));
    fireEvent.change(screen.getByLabelText("Review status"), { target: { value: "clarification" } });
    expect(screen.getAllByText("Ben Lee")).toHaveLength(2);
    expect(screen.queryByText("Aisha Tan")).toBeNull();
    expect(screen.getAllByText("Needs clarification").length).toBeGreaterThan(0);
  });

  it("requires a review note and a second confirmation before approval", async () => {
    const action = vi.fn().mockResolvedValue({ ok: true });
    render(<PaymentWorkspace reviews={[reviews[0]]} enrollments={enrollments} action={action} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve payment" }));
    expect(action).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Review note"), { target: { value: "Bank settlement matched the invoice" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm decision" }));
    await waitFor(() => expect(action).toHaveBeenCalledWith(
      "/api/staff/payment-reviews/review-1/decision",
      expect.objectContaining({ decision: "approve", amount: "120.00", reference: "BANK1234", transaction_date: "2026-10-27", note: "Bank settlement matched the invoice" }),
      "Payment review decision saved.",
    ));
  });

  it.each([
    ["Request clarification", "clarify"],
    ["Reject evidence", "reject"],
  ])("confirms %s with a staff note", async (button, decision) => {
    const action = vi.fn().mockResolvedValue({ ok: true });
    render(<PaymentWorkspace reviews={[reviews[0]]} enrollments={enrollments} action={action} />);
    fireEvent.click(screen.getByRole("button", { name: button }));
    expect(action).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Review note"), { target: { value: "Missing settlement proof" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm decision" }));
    await waitFor(() => expect(action).toHaveBeenCalledWith(
      "/api/staff/payment-reviews/review-1/decision",
      expect.objectContaining({ decision, note: "Missing settlement proof" }),
      "Payment review decision saved.",
    ));
  });
});
