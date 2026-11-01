const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL || "";
export const apiBaseUrl = configuredBaseUrl.replace(/\/$/, "");
let csrfToken = "";

export function setCsrfToken(value) {
  csrfToken = value || "";
}

export function getAuthSession() {
  return requestJson("/api/auth/session");
}

export function createGuestSession(fullName) {
  return postJson("/api/auth/guest", { full_name: fullName });
}

export async function createConversation() {
  return requestJson("/api/conversations", { method: "POST" });
}

export async function listConversations() {
  return requestJson("/api/conversations");
}

export async function getConversationMessages(conversationId) {
  return requestJson(`/api/conversations/${encodeURIComponent(conversationId)}/messages`);
}

export async function sendChatMessage(message, conversationId) {
  return requestJson("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  });
}

export function getConversationStatus(conversationId) {
  return requestJson(`/api/conversations/${encodeURIComponent(conversationId)}/status`);
}

export async function downloadParticipantInvoice(enrollmentId) {
  let response;
  try {
    response = await fetch(
      `${apiBaseUrl}/api/participant/enrollments/${encodeURIComponent(enrollmentId)}/invoice`,
      { credentials: "include" },
    );
  } catch {
    throw new Error("We’re unable to connect right now. Please try again shortly.");
  }
  const contentType = response.headers.get("Content-Type") || "";
  if (!response.ok || !contentType.toLowerCase().startsWith("application/pdf")) {
    const payload = await readJson(response);
    const error = new Error(
      payload?.error?.message || payload?.message || "Your invoice is not ready to download yet.",
    );
    error.status = response.status;
    error.code = payload?.error?.code;
    throw error;
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const filenameMatch = disposition.match(/filename\*?=(?:UTF-8''|\")?([^";]+)/i);
  return {
    blob: await response.blob(),
    filename: filenameMatch ? decodeURIComponent(filenameMatch[1].replace(/^"|"$/g, "")) : "invoice.pdf",
  };
}

export function resendParticipantInvoiceEmail(enrollmentId, confirm = false) {
  return postJson(
    `/api/participant/enrollments/${encodeURIComponent(enrollmentId)}/invoice-email/resend`,
    { confirm },
  );
}

export async function uploadEvidence(conversationId, file, paymentType, message = "", submissionId = "") {
  const body = new FormData();
  body.append("file", file);
  body.append("payment_type", paymentType);
  body.append("message", message);
  body.append("submission_id", submissionId);
  const path = `/api/conversations/${encodeURIComponent(conversationId)}/evidence`;
  const options = {
    method: "POST",
    body,
  };
  try {
    return await requestJson(path, options);
  } catch (error) {
    if (error.code !== "NETWORK_ERROR") throw error;
    await new Promise((resolve) => setTimeout(resolve, 700));
    return requestJson(path, options);
  }
}

export function documentUrl(documentId, conversationId = "") {
  const path = conversationId
    ? `/api/conversations/${encodeURIComponent(conversationId)}/documents/${encodeURIComponent(documentId)}`
    : `/api/staff/documents/${encodeURIComponent(documentId)}`;
  return `${apiBaseUrl}${path}`;
}

export function postJson(path, payload, method = "POST") {
  return requestJson(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function requestJson(path, options = {}) {
  let response;
  const headers = new Headers(options.headers || {});
  if (csrfToken && !["GET", "HEAD", "OPTIONS"].includes(options.method || "GET")) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  try {
    response = await fetch(`${apiBaseUrl}${path}`, { ...options, credentials: "include", headers });
  } catch {
    const error = new Error("We’re unable to connect right now. Please try again shortly.");
    error.code = "NETWORK_ERROR";
    throw error;
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const error = new Error(payload?.error?.message || "We couldn’t send your message. Please try again.");
    error.status = response.status;
    error.code = payload?.error?.code;
    throw error;
  }
  return payload;
}

async function readJson(response) {
  try { return await response.json(); }
  catch { return {}; }
}
