import React, { useEffect, useRef, useState } from "react";
import { documentUrl, downloadParticipantInvoice, getConversationStatus, resendParticipantInvoiceEmail, uploadEvidence } from "../../services/chatApi.js";

const money = (amount) => new Intl.NumberFormat("en-SG", { style: "currency", currency: "SGD" }).format(Number(amount || 0));

export default function ParticipantPanel({ conversationId, revision, onUpdated }) {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [file, setFile] = useState(null);
  const [paymentType, setPaymentType] = useState("paynow");
  const [downloadingInvoice, setDownloadingInvoice] = useState(false);
  const [resendingInvoice, setResendingInvoice] = useState(false);
  const fileInput = useRef(null);
  const invoiceDownloadInFlight = useRef(false);
  const invoiceResendInFlight = useRef(false);

  useEffect(() => {
    let cancelled = false;
    if (!conversationId) return;
    setLoading(true);
    setError("");
    getConversationStatus(conversationId).then((result) => { if (!cancelled) setStatus(result); }).catch((requestError) => { if (!cancelled) setError(requestError.message); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [conversationId, revision]);

  async function handleUpload(event) {
    event.preventDefault();
    if (!file) { setError("Select a payment screenshot to upload."); return; }
    if (file.size > 10 * 1024 * 1024) { setError("The screenshot must be smaller than 10 MB."); return; }
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const result = await uploadEvidence(conversationId, file, paymentType);
      setNotice(result.message || "Your payment screenshot has been submitted. Your enrollment status will update after verification.");
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      onUpdated();
    } catch (requestError) { setError(requestError.message); } finally { setLoading(false); }
  }

  async function handleInvoiceDownload() {
    const enrollmentId = status?.finance?.id || status?.finance?.enrollment_id;
    if (!enrollmentId || invoiceDownloadInFlight.current) return;
    invoiceDownloadInFlight.current = true;
    setDownloadingInvoice(true);
    setError("");
    setNotice("");
    try {
      const { blob, filename } = await downloadParticipantInvoice(enrollmentId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setNotice("Your invoice download has started.");
    } catch (requestError) {
      setError(
        requestError.status === 401
          ? "Your session has expired. Please sign in again to download your invoice."
          : requestError.message,
      );
    } finally {
      invoiceDownloadInFlight.current = false;
      setDownloadingInvoice(false);
    }
  }

  async function handleInvoiceResend() {
    const enrollmentId = status?.finance?.id || status?.finance?.enrollment_id;
    if (!enrollmentId || invoiceResendInFlight.current) return;
    invoiceResendInFlight.current = true;
    setResendingInvoice(true);
    setError("");
    setNotice("");
    try {
      const confirmed = window.confirm(
        "Resend the latest invoice to your registered email address?",
      );
      if (!confirmed) {
        setNotice("The invoice will not be resent.");
        return;
      }
      const result = await resendParticipantInvoiceEmail(enrollmentId, true);
      setStatus((current) => ({
        ...current,
        finance: { ...current?.finance, invoice_email: result.invoice_email },
      }));
      setNotice(result.invoice_email?.message || "Your invoice email request was submitted.");
    } catch (requestError) {
      setError(
        requestError.status === 401
          ? "Your session has expired. Please sign in again to resend your invoice."
          : requestError.message,
      );
    } finally {
      invoiceResendInFlight.current = false;
      setResendingInvoice(false);
    }
  }

  const finance = status?.finance;
  const invoice = finance?.invoice || finance?.invoices?.[0];
  const invoiceDocument = finance?.invoice_document;
  const invoiceEmail = finance?.invoice_email;
  const documents = finance?.documents || [];
  const hasEnrollment = Boolean(finance?.id || finance?.enrollment_id || invoice);
  const enrollmentStatus = finance?.status || status?.enrollment?.status;
  return <aside className="participant-panel" aria-labelledby="enrollment-panel-title" aria-busy={loading}>
    <span className="eyebrow">Your course journey</span>
    <h2 id="enrollment-panel-title">Enrollment &amp; payment</h2>
    <p className="muted">Invoices, payment updates, and receipts for this conversation.</p>
    {error && <p className="notice-error" role="alert">{error} <button type="button" className="text-button" onClick={onUpdated}>Retry</button></p>}
    {notice && <p className="notice-success" role="status">{notice}</p>}
    {loading && !status && <p role="status">Loading your enrollment…</p>}
    {enrollmentStatus ? <><span className="status-badge">{String(enrollmentStatus).replaceAll("_", " ")}</span>{status?.enrollment?.missing_fields?.length > 0 && <p className="muted">The assistant will help you complete: {status.enrollment.missing_fields.join(", ").replaceAll("_", " ")}.</p>}</> : !loading && <div className="empty-state"><p>You have no enrollment yet.</p><p className="muted">Select “Enroll now” in the chat to get started.</p></div>}
    {(finance?.course || status?.enrollment?.course_name) && <dl className="detail-list"><div><dt>Course</dt><dd>{finance?.course || status.enrollment.course_name}</dd></div>{(finance?.friendly_reference || status?.enrollment?.friendly_reference) && <div><dt>Enrollment reference</dt><dd>{finance?.friendly_reference || status.enrollment.friendly_reference}</dd></div>}{(finance?.intake_display || status?.enrollment?.selected_intake?.display_date) && <div><dt>Intake</dt><dd>{finance?.intake_display || status.enrollment.selected_intake.display_date}</dd></div>}</dl>}
    {invoice && <dl className="detail-list"><div><dt>Invoice</dt><dd>{invoice.number || invoice.invoice_number}</dd></div><div><dt>Course fee</dt><dd>{money(invoice.course_fee)}</dd></div><div><dt>SkillsFuture credits</dt><dd>{money(invoice.skillsfuture_amount)}</dd></div><div><dt>PayNow balance</dt><dd>{money(invoice.net_payable)}</dd></div>{invoice.due_date && <div><dt>Due date</dt><dd>{invoice.due_date.slice(0, 10)}</dd></div>}</dl>}
    {invoiceDocument && <InvoiceDownloadAction document={invoiceDocument} delivery={invoiceEmail} downloading={downloadingInvoice} resending={resendingInvoice} onDownload={handleInvoiceDownload} onResend={handleInvoiceResend} />}
    {documents.length > 0 && <section className="panel-section"><h3>Your documents</h3><div className="document-links">{documents.map((document) => <a key={document.id} href={documentUrl(document.id, conversationId)} target="_blank" rel="noreferrer">{String(document.kind || document.type || "document").replaceAll("_", " ")} <span>{document.number || "Open PDF"} ↗</span></a>)}</div></section>}
    {hasEnrollment && <section className="panel-section"><h3>Submit payment evidence</h3><p className="muted small-copy">Upload a clear screenshot showing the amount, recipient, date, and transaction reference.</p><form onSubmit={handleUpload} className="stack-form"><label>Payment type<select value={paymentType} onChange={(event) => setPaymentType(event.target.value)}><option value="paynow">PayNow payment</option><option value="skillsfuture">SkillsFuture claim</option></select></label><label>Payment screenshot<input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" onChange={(event) => setFile(event.target.files?.[0] || null)} /></label><button type="submit" className="primary-button" disabled={loading}>{loading ? "Submitting…" : "Submit screenshot"}</button></form></section>}
    <p className="muted small-copy">Your invoice and receipt are also delivered to your email address on record.</p>
  </aside>;
}

export function InvoiceDownloadAction({ document, delivery, downloading, resending, onDownload, onResend }) {
  const state = document.status || "unavailable";
  if (state === "generated") {
    return <section className="panel-section" aria-live="polite">
      <h3>Your invoice</h3>
      {delivery?.message && <p className="muted small-copy" role="status">{delivery.message}</p>}
      <button
        type="button"
        className="secondary-button"
        onClick={onResend}
        disabled={resending || delivery?.can_resend === false}
        aria-busy={resending}
      >{resending ? "Requesting resend..." : "Resend Invoice Email"}</button>
      <button
        type="button"
        className="primary-button"
        onClick={onDownload}
        disabled={downloading}
        aria-label="Download Invoice PDF"
        aria-busy={downloading}
      >{downloading ? "Downloading invoice…" : "Download Invoice"}</button>
    </section>;
  }
  const labels = {
    pending: ["Generating invoice…", "Your confirmed enrollment is safe while the invoice is prepared."],
    needs_review: ["Invoice needs review", "Staff review is required before this invoice can be downloaded."],
    failed: ["Invoice generation failed", "The finance team can safely retry the invoice. Your enrollment remains confirmed."],
    unavailable: ["Invoice unavailable", "Complete and confirm your enrollment before an invoice can be prepared."],
  };
  const [label, explanation] = labels[state] || labels.failed;
  return <section className="panel-section" aria-live="polite">
    <h3>Your invoice</h3>
    <button type="button" className="primary-button" disabled aria-label={label}>{label}</button>
    <p className="muted small-copy">{explanation}</p>
  </section>;
}
