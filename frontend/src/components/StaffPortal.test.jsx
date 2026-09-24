import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EnrollmentDetail } from "./StaffPortal.jsx";

afterEach(cleanup);

const enrollment = {
  id: "fictional-enrollment",
  full_name: "Test Student",
  email: "test.student@example.com",
  mobile_number: "91234567",
  nric_masked: "S*******D",
  course: "Basic Certificate in Dental Assisting",
  intake_display: "8–9 February 2027",
  status: "confirmed",
  payment_allocation: [
    { method: "SkillsFuture Credit", amount: "400.00" },
    { method: "PayNow", amount: "200.00" },
  ],
  payment_verification_status: "Awaiting staff verification",
  invoice_document: {
    id: "fictional-document",
    invoice_number: "DEMO-INV-2027-000001",
    status: "generated",
    version: 1,
    download_url: "/api/staff/invoice-documents/fictional-document/download",
  },
  documents: [],
  payments: [],
};

describe("template invoice staff controls", () => {
  it("shows status, download and regeneration to finance staff", () => {
    render(
      <EnrollmentDetail
        enrollment={enrollment}
        action={vi.fn()}
        working={false}
        financeAccess
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("DEMO-INV-2027-000001")).toBeTruthy();
    expect(screen.getByText("generated")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Generate / regenerate invoice" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: /Download invoice PDF/ }),
    ).toBeTruthy();
  });

  it("does not expose invoice actions to operations-only staff", () => {
    render(
      <EnrollmentDetail
        enrollment={enrollment}
        action={vi.fn()}
        working={false}
        financeAccess={false}
        onClose={vi.fn()}
      />,
    );

    expect(
      screen.queryByRole("button", { name: "Generate / regenerate invoice" }),
    ).toBeNull();
    expect(
      screen.queryByRole("link", { name: /Download invoice PDF/ }),
    ).toBeNull();
  });

  it("shows the safe review reason without exposing a server path", () => {
    render(
      <EnrollmentDetail
        enrollment={{
          ...enrollment,
          invoice_document: {
            ...enrollment.invoice_document,
            status: "needs_review",
            download_url: null,
            failure_message:
              "The official invoice template requires Q&M approval before customer use.",
          },
        }}
        action={vi.fn()}
        working={false}
        financeAccess
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText(/requires Q&M approval/)).toBeTruthy();
    expect(screen.queryByText(/backend[\\/]data[\\/]private/)).toBeNull();
  });
});
