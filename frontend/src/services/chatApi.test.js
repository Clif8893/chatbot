import { afterEach, expect, it, vi } from "vitest";
import { setCsrfToken, uploadEvidence } from "./chatApi.js";

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken("");
});

it("retries a disconnected screenshot upload with the same submission id", async () => {
  const fetchMock = vi.fn()
    .mockRejectedValueOnce(new TypeError("connection closed"))
    .mockResolvedValueOnce(new Response(JSON.stringify({ verdict: "pending_review" }), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    }));
  vi.stubGlobal("fetch", fetchMock);
  setCsrfToken("test-csrf");
  const file = new File(["image"], "payment.png", { type: "image/png" });

  const result = await uploadEvidence("conversation-1", file, "paynow", "Paid", "same-submission");

  expect(result.verdict).toBe("pending_review");
  expect(fetchMock).toHaveBeenCalledTimes(2);
  const first = fetchMock.mock.calls[0][1];
  const second = fetchMock.mock.calls[1][1];
  expect(first.body).toBe(second.body);
  expect(first.body.get("submission_id")).toBe("same-submission");
  expect(second.headers.get("X-CSRF-Token")).toBe("test-csrf");
});
