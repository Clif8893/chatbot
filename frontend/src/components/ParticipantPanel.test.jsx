import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InvoiceDownloadAction } from "./ParticipantPanel.jsx";

afterEach(cleanup);

describe("participant invoice download states", () => {
  it("shows masked delivery status and allows a generated invoice resend", () => {
    const resend = vi.fn();
    render(
      <InvoiceDownloadAction
        document={{ status: "generated" }}
        delivery={{
          status: "sent",
          message: "Invoice emailed to t***@example.com",
          can_resend: true,
        }}
        downloading={false}
        resending={false}
        onDownload={vi.fn()}
        onResend={resend}
      />,
    );
    expect(screen.getByText("Invoice emailed to t***@example.com")).toBeTruthy();
    expect(screen.queryByText("test.student@example.com")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Resend Invoice Email" }));
    expect(resend).toHaveBeenCalledTimes(1);
  });

  it("prevents duplicate resend clicks while an email request is processing", () => {
    render(
      <InvoiceDownloadAction
        document={{ status: "generated" }}
        delivery={{ status: "pending", message: "Preparing invoice email...", can_resend: false }}
        downloading={false}
        resending
        onDownload={vi.fn()}
        onResend={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Requesting resend..." }).disabled).toBe(true);
  });

  it("shows an accessible download action only when the PDF is generated", () => {
    const download = vi.fn();
    render(
      <InvoiceDownloadAction
        document={{ status: "generated" }}
        downloading={false}
        onDownload={download}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Download Invoice PDF" }));
    expect(download).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["pending", "Generating invoice…"],
    ["needs_review", "Invoice needs review"],
    ["failed", "Invoice generation failed"],
    ["unavailable", "Invoice unavailable"],
  ])("renders %s as a disabled status", (status, label) => {
    render(
      <InvoiceDownloadAction
        document={{ status }}
        downloading={false}
        onDownload={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: label }).disabled).toBe(true);
  });

  it("disables the action while a download is already running", () => {
    render(
      <InvoiceDownloadAction
        document={{ status: "generated" }}
        downloading
        onDownload={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Download Invoice PDF" }).disabled).toBe(true);
    expect(screen.getByText("Downloading invoice…")).toBeTruthy();
  });
});
