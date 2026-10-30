import React, { useEffect, useRef, useState } from "react";
import { documentUrl, downloadParticipantInvoice, getConversationStatus, resendParticipantInvoiceEmail } from "../../services/chatApi.js";

const money = (amount) => new Intl.NumberFormat("en-SG", { style: "currency", currency: "SGD" }).format(Number(amount || 0));

export default function ParticipantPanel({ conversationId, revision, onUpdated }) {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [downloadingInvoice, setDownloadingInvoice] = useState(false);
  const [resendingInvoice, setResendingInvoice] = useState(false);
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
          ? "Your chat session has expired. Refresh the page and enter your name again."
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
          ? "Your chat session has expired. Refresh the page and enter your name again."
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
  const courses = status?.enrollment?.courses || [];
  const enrollmentStatus = finance?.status || status?.enrollment?.status;
  return <aside className="participant-panel" aria-labelledby="enrollment-panel-title" aria-busy={loading}>
    <span className="eyebrow">Your course journey</span>
    <h2 id="enrollment-panel-title">Enrollment &amp; invoice</h2>
    <p className="muted">Enrollment details and invoices for this conversation.</p>
    {error && <p className="notice-error" role="alert">{error} <button type="button" className="text-button" onClick={onUpdated}>Retry</button></p>}
    {notice && <p className="notice-success" role="status">{notice}</p>}
    {loading && !status && <p role="status">Loading your enrollment…</p>}
    {enrollmentStatus ? <><span className="status-badge">{String(enrollmentStatus).replaceAll("_", " ")}</span>{status?.enrollment?.missing_fields?.length > 0 && <p className="muted">The assistant will help you complete: {status.enrollment.missing_fields.join(", ").replaceAll("_", " ")}.</p>}</> : !loading && <div className="empty-state"><p>You have no enrollment yet.</p><p className="muted">Select “Enroll now” in the chat to get started.</p></div>}
    {courses.length > 1 ? <dl className="detail-list">{courses.map((course) => <React.Fragment key={course.id || course.course_id}><div><dt>Course {course.position}</dt><dd>{course.course_name}</dd></div><div><dt>Course {course.position} intake</dt><dd>{course.selected_intake?.display_date || "Awaiting selection"}</dd></div><div><dt>Course {course.position} status</dt><dd>{String(course.status || enrollmentStatus).replaceAll("_", " ")}</dd></div></React.Fragment>)}{(finance?.friendly_reference || status?.enrollment?.friendly_reference) && <div><dt>Enrollment reference</dt><dd>{finance?.friendly_reference || status.enrollment.friendly_reference}</dd></div>}</dl> : (finance?.course || status?.enrollment?.course_name) && <dl className="detail-list"><div><dt>Course</dt><dd>{finance?.course || status.enrollment.course_name}</dd></div>{(finance?.friendly_reference || status?.enrollment?.friendly_reference) && <div><dt>Enrollment reference</dt><dd>{finance?.friendly_reference || status.enrollment.friendly_reference}</dd></div>}{(finance?.intake_display || status?.enrollment?.selected_intake?.display_date) && <div><dt>Intake</dt><dd>{finance?.intake_display || status.enrollment.selected_intake.display_date}</dd></div>}</dl>}
    {invoice && <dl className="detail-list"><div><dt>Invoice</dt><dd>{invoice.number || invoice.invoice_number}</dd></div><div><dt>Course fee</dt><dd>{money(invoice.course_fee)}</dd></div><div><dt>Total amount due</dt><dd>{money(invoice.course_fee)}</dd></div>{invoice.due_date && <div><dt>Due date</dt><dd>{invoice.due_date.slice(0, 10)}</dd></div>}</dl>}
    {invoiceDocument && <InvoiceDownloadAction document={invoiceDocument} delivery={invoiceEmail} downloading={downloadingInvoice} resending={resendingInvoice} onDownload={handleInvoiceDownload} onResend={handleInvoiceResend} />}
    {documents.length > 0 && <section className="panel-section"><h3>Your documents</h3><div className="document-links">{documents.map((document) => <a key={document.id} href={documentUrl(document.id, conversationId)} target="_blank" rel="noreferrer">{String(document.kind || document.type || "document").replaceAll("_", " ")} <span>{document.number || "Open PDF"} ↗</span></a>)}</div></section>}
    <p className="muted small-copy">Your invoice is also delivered to your email address on record.</p>
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
