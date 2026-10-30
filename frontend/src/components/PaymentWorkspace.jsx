import React, { useEffect, useMemo, useState } from "react";
import { apiBaseUrl } from "../services/chatApi.js";
import "./payment-workspace.css";

const CURRENCY = new Intl.NumberFormat("en-SG", { style: "currency", currency: "SGD" });
const amount = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value))
  ? CURRENCY.format(Number(value)) : "Not shown";
const day = (value) => {
  if (!value) return "Not recorded";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : new Intl.DateTimeFormat("en-SG", { dateStyle: "medium" }).format(parsed);
};
const timestamp = (value) => {
  if (!value) return "Not recorded";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : new Intl.DateTimeFormat("en-SG", { dateStyle: "medium", timeStyle: "short" }).format(parsed);
};
const label = (value) => String(value || "").replaceAll("_", " ");

export function paymentStatus(status) {
  if (["pending", "pending_review", "flagged", "needs_review", "open"].includes(status)) return "pending";
  if (status === "clarification_requested") return "clarification";
  if (status === "approved") return "verified";
  if (status === "rejected") return "rejected";
  return "pending";
}

const STATUS_LABELS = {
  pending: "Pending review",
  clarification: "Needs clarification",
  verified: "Verified",
  rejected: "Rejected",
};

export function PaymentStatusBadge({ status }) {
  const state = paymentStatus(status);
  return <span className={`payment-status payment-status--${state}`}><span aria-hidden="true" />{STATUS_LABELS[state]}</span>;
}

function EvidencePreview({ review }) {
  const [state, setState] = useState({ url: "", error: "", loading: true });
  useEffect(() => {
    let active = true;
    let objectUrl = "";
    const controller = new AbortController();
    setState({ url: "", error: "", loading: true });
    async function load() {
      try {
        const path = review.evidence?.download_url || `/api/staff/evidence/${review.evidence_id}`;
        const response = await fetch(`${apiBaseUrl}${path}`, { credentials: "include", signal: controller.signal });
        if (!response.ok) throw new Error("The screenshot could not be opened.");
        const blob = await response.blob();
        if (!blob.type.startsWith("image/")) throw new Error("The attachment is not a supported image.");
        objectUrl = URL.createObjectURL(blob);
        if (active) setState({ url: objectUrl, error: "", loading: false });
      } catch (error) {
        if (active && error.name !== "AbortError") setState({ url: "", error: error.message, loading: false });
      }
    }
    load();
    return () => {
      active = false;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [review.evidence_id, review.evidence?.download_url]);

  return <section className="payment-evidence" aria-label="Uploaded payment screenshot">
    <div className="payment-panel-heading"><div><span className="payment-kicker">Original attachment</span><h3>Payment screenshot</h3></div><span className="payment-file-type">{label(review.evidence?.payment_type || "image")}</span></div>
    <div className="payment-image-frame">
      {state.loading && <p role="status">Loading protected screenshot…</p>}
      {state.error && <p role="alert">{state.error}</p>}
      {state.url && <img src={state.url} alt={`Payment screenshot for submission ${review.id}`} />}
    </div>
    <div className="payment-evidence-footer"><span>Submitted {timestamp(review.created_at)}</span>{state.url && <a href={state.url} target="_blank" rel="noreferrer">Open full image ↗</a>}</div>
  </section>;
}

function ReviewField({ title, value, original, flags = [], children, className = "" }) {
  const isFlagged = flags.length > 0;
  return <div className={`payment-field ${isFlagged ? "payment-field--flagged" : ""} ${className}`}>
    <div className="payment-field-title"><span>{title}</span>{isFlagged && <span className="payment-field-warning">Check this</span>}</div>
    {children || <strong>{value || "Not shown"}</strong>}
    {original !== undefined && original !== null && String(original) !== String(value) && <small>Originally detected: {original}</small>}
    {isFlagged && <small>{flags.map(label).join(" · ")}</small>}
  </div>;
}

const FIELD_FLAGS = {
  recipient: ["recipient_mismatch", "payee_not_configured"],
  amount: ["amount_mismatch", "amount_unreadable", "multiple_payments"],
  currency: ["currency_mismatch"],
  reference: ["reference_missing", "duplicate_transaction_reference", "duplicate_screenshot"],
  transaction_date: ["transaction_date_implausible", "transaction_date_unreadable"],
  invoice_number: ["invoice_reference_missing_or_mismatch"],
  screenshot_type: ["screenshot_type_mismatch"],
  skillsfuture_claim_amount: ["skillsfuture_claim_requires_accounts_confirmation"],
};

function PaymentReviewDetail({ review, enrollment, reviews, action, working }) {
  const extracted = review.extracted || {};
  const original = review.original_extracted || {};
  const [decision, setDecision] = useState("");
  const [note, setNote] = useState("");
  const [verifiedAmount, setVerifiedAmount] = useState(extracted.amount || "");
  const [reference, setReference] = useState(extracted.reference || "");
  const [transactionDate, setTransactionDate] = useState(extracted.transaction_date || "");
  const [corrections, setCorrections] = useState({
    recipient: extracted.recipient || "",
    currency: extracted.currency || "",
    invoice_number: extracted.invoice_number || "",
    screenshot_type: extracted.screenshot_type || "",
    provider: extracted.provider || "",
    payer: extracted.payer || "",
    skillsfuture_claim_amount: extracted.skillsfuture_claim_amount || "",
    skillsfuture_claim_status: extracted.skillsfuture_claim_status || "",
  });
  const invoice = review.invoice || enrollment?.invoice || {};
  const payments = review.verified_payments || enrollment?.payments || [];
  const flags = Array.isArray(review.reasons) ? review.reasons : [];
  const fieldFlags = (field) => [
    ...flags.filter((reason) => FIELD_FLAGS[field]?.includes(reason)),
    ...(Array.isArray(extracted.unclear_fields) && extracted.unclear_fields.includes(field) ? ["unclear in screenshot"] : []),
  ];
  const history = reviews.filter((item) => item.enrollment_id === review.enrollment_id)
    .sort((a, b) => String(b.decided_at || b.created_at).localeCompare(String(a.decided_at || a.created_at)));
  const pending = paymentStatus(review.status) === "pending";
  const change = (key, value) => setCorrections((current) => ({ ...current, [key]: value }));

  async function submitDecision(event) {
    event.preventDefault();
    const payload = {
      decision: decision === "clarification" ? "clarify" : decision === "verified" ? "approve" : "reject",
      note: note.trim(),
      corrections,
      ...(decision === "verified" ? { amount: verifiedAmount, reference: reference.trim(), transaction_date: transactionDate } : {}),
    };
    const result = await action(`/api/staff/payment-reviews/${review.id}/decision`, payload, "Payment review decision saved.");
    if (result) setDecision("");
  }

  const edit = (key, title, options = {}) => <ReviewField title={title} value={corrections[key]} original={original[key]} flags={fieldFlags(key)}>
    <input aria-label={title} value={corrections[key]} onChange={(event) => change(key, event.target.value)} disabled={!pending || working} {...options} />
  </ReviewField>;

  return <section className="payment-detail" aria-label="Selected payment submission">
    <header className="payment-detail-header">
      <div><span className="payment-kicker">Submission · {review.id.slice(0, 8)}</span><h2>{enrollment?.full_name || "Participant"}</h2><p>{enrollment?.course || "Course not recorded"} · {enrollment?.intake_display || "Intake to confirm"}</p></div>
      <PaymentStatusBadge status={review.status} />
    </header>
    <div className="payment-balance-strip">
      <div><span>Invoice total</span><strong>{amount(invoice.course_fee)}</strong></div>
      <div><span>Approved payments</span><strong>{amount(payments.reduce((sum, row) => sum + Number(row.amount || 0), 0))}</strong></div>
      <div className="payment-balance-remaining"><span>Remaining balance</span><strong>{amount(review.outstanding_amount ?? enrollment?.outstanding_amount)}</strong></div>
    </div>
    <div className="payment-review-columns">
      <EvidencePreview review={review} />
      <div className="payment-facts">
        <section className="payment-section">
          <div className="payment-panel-heading"><div><span className="payment-kicker">Linked record</span><h3>Customer &amp; invoice</h3></div></div>
          <div className="payment-facts-grid"><ReviewField title="Invoice number" value={invoice.number || "Not linked"} /><ReviewField title="Enrollment reference" value={enrollment?.friendly_reference || review.enrollment_id} /><ReviewField title="Contact" value={enrollment?.email || enrollment?.mobile_number || "Not shown"} /></div>
        </section>
        {flags.length > 0 && <section className="payment-flag-panel" aria-label="Review flags"><strong>Items to check</strong><div>{flags.map((flag) => <span key={flag}>{label(flag)}</span>)}</div></section>}
        <section className="payment-section">
          <div className="payment-panel-heading"><div><span className="payment-kicker">Image extraction</span><h3>Payment details</h3></div><span className="payment-confidence">Confidence {typeof extracted.confidence === "number" ? `${Math.round(extracted.confidence * 100)}%` : "unavailable"}</span></div>
          <div className="payment-facts-grid">
            {edit("recipient", "Payee shown")}
            {edit("provider", "Bank or provider")}
            {edit("payer", "Payer shown")}
            {edit("currency", "Currency")}
            {edit("invoice_number", "Invoice reference shown")}
            {edit("screenshot_type", "Screenshot type")}
            <ReviewField title="Amount shown" value={amount(extracted.amount)} flags={fieldFlags("amount")} />
            <ReviewField title="Reference shown" value={extracted.reference} flags={fieldFlags("reference")} />
            <ReviewField title="Transaction date shown" value={day(extracted.transaction_date)} flags={fieldFlags("transaction_date")} />
            <ReviewField title="Transaction time" value={extracted.transaction_time} />
          </div>
        </section>
        <section className="payment-section">
          <div className="payment-panel-heading"><div><span className="payment-kicker">Funding</span><h3>SkillsFuture claim</h3></div></div>
          <p className="payment-explainer">A claim shown in a screenshot is not a verified payment.</p>
          <div className="payment-facts-grid"><ReviewField title="Invoice allocation" value={amount(invoice.skillsfuture_amount)} />{edit("skillsfuture_claim_amount", "Claim amount shown", { inputMode: "decimal" })}{edit("skillsfuture_claim_status", "Claim status shown")}</div>
        </section>
      </div>
    </div>
    <div className="payment-bottom-grid">
      <section className="payment-section payment-history">
        <div className="payment-panel-heading"><div><span className="payment-kicker">Recorded decisions</span><h3>Review history</h3></div></div>
        <ol>{history.map((item) => <li key={item.id}><PaymentStatusBadge status={item.status} /><div><strong>{item.decided_by || "Awaiting staff review"}</strong><span>{timestamp(item.decided_at || item.created_at)} · {item.note || "No decision note yet"}</span></div></li>)}</ol>
      </section>
      <section className="payment-section payment-history">
        <div className="payment-panel-heading"><div><span className="payment-kicker">Ledger</span><h3>Approved payments</h3></div></div>
        {payments.length ? <ol>{payments.map((item) => <li key={item.id}><strong>{amount(item.amount)}</strong><div><strong>{label(item.method)}</strong><span>{item.reference} · {day(item.transaction_date)} · {item.confirmed_by}</span></div></li>)}</ol> : <p className="payment-explainer">No verified payments recorded for this invoice.</p>}
      </section>
    </div>
    {pending && <section className="payment-decision-panel">
      <div className="payment-panel-heading"><div><span className="payment-kicker">Staff decision</span><h3>Complete review</h3></div></div>
      <p>Check the bank or settlement record before marking any amount as verified.</p>
      <div className="payment-verify-grid">
        <label>Verified amount (SGD)<input type="number" min="0.01" step="0.01" value={verifiedAmount} onChange={(event) => setVerifiedAmount(event.target.value)} disabled={working} /></label>
        <label>Verified transaction date<input type="date" value={transactionDate} onChange={(event) => setTransactionDate(event.target.value)} disabled={working} /></label>
        <label>Verified transaction reference<input value={reference} onChange={(event) => setReference(event.target.value)} disabled={working} /></label>
      </div>
      <div className="payment-action-row">
        <button type="button" className="primary-button" onClick={() => setDecision("verified")} disabled={working}>Approve payment</button>
        <button type="button" className="secondary-button" onClick={() => setDecision("clarification")} disabled={working}>Request clarification</button>
        <button type="button" className="danger-button" onClick={() => setDecision("rejected")} disabled={working}>Reject evidence</button>
      </div>
      {decision && <form className="payment-confirmation" onSubmit={submitDecision}>
        <div><strong>Confirm: {STATUS_LABELS[decision]}</strong><p>{decision === "verified" ? `Record ${amount(verifiedAmount)} against invoice ${invoice.number || "this invoice"}?` : decision === "clarification" ? "Ask the participant for more information?" : "Reject this screenshot without recording a payment?"}</p></div>
        <label>Review note<textarea value={note} onChange={(event) => setNote(event.target.value)} required rows={3} placeholder="Record what you checked and why" /></label>
        {decision === "verified" && (!verifiedAmount || !reference.trim() || !transactionDate) && <p className="payment-inline-error">Enter the verified amount, transaction date and unique reference before approval.</p>}
        <div className="payment-action-row"><button className="primary-button" type="submit" disabled={working || (decision === "verified" && (!verifiedAmount || !reference.trim() || !transactionDate))}>{working ? "Saving…" : "Confirm decision"}</button><button className="secondary-button" type="button" onClick={() => setDecision("")} disabled={working}>Go back</button></div>
      </form>}
    </section>}
  </section>;
}

export default function PaymentWorkspace({ reviews = [], enrollments = [], action, working = false }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [type, setType] = useState("all");
  const [selectedId, setSelectedId] = useState("");
  const counts = useMemo(() => reviews.reduce((result, review) => {
    result[paymentStatus(review.status)] += 1;
    return result;
  }, { pending: 0, clarification: 0, verified: 0, rejected: 0 }), [reviews]);
  const filtered = useMemo(() => reviews.filter((review) => {
    if (status !== "all" && paymentStatus(review.status) !== status) return false;
    if (type !== "all" && review.evidence?.payment_type !== type) return false;
    const enrollment = enrollments.find((item) => item.id === review.enrollment_id);
    const haystack = [enrollment?.full_name, enrollment?.friendly_reference, enrollment?.course,
      review.invoice?.number, review.extracted?.reference, review.evidence_id, ...(review.reasons || [])].join(" ").toLowerCase();
    return haystack.includes(query.trim().toLowerCase());
  }), [reviews, enrollments, status, type, query]);
  const selected = filtered.find((review) => review.id === selectedId) || filtered[0];
  const enrollment = selected && enrollments.find((item) => item.id === selected.enrollment_id);

  return <div className="payment-workspace">
    <div className="payment-summary" aria-label="Payment review summary">
      <div><span>Pending review</span><strong>{counts.pending}</strong><small>Awaiting a staff decision</small></div>
      <div><span>Needs clarification</span><strong>{counts.clarification}</strong><small>Waiting for customer details</small></div>
      <div><span>Verified submissions</span><strong>{counts.verified}</strong><small>Approved by accounts</small></div>
      <div><span>Outstanding balances</span><strong>{amount(enrollments.reduce((sum, row) => sum + Number(row.outstanding_amount || 0), 0))}</strong><small>Across current invoices</small></div>
    </div>
    <div className="payment-toolbar">
      <div><span className="payment-kicker">Accounts queue</span><p>Review each screenshot against the original invoice and settlement record.</p></div>
      <a className="secondary-button" href={`${apiBaseUrl}/api/staff/reports/payments.xlsx`}>Export Excel ↓</a>
    </div>
    <div className="payment-filters">
      <label className="payment-search">Search submissions<input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Name, invoice, course or reference" /></label>
      <label>Review status<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All statuses</option>{Object.entries(STATUS_LABELS).map(([key, title]) => <option key={key} value={key}>{title}</option>)}</select></label>
      <label>Evidence type<select value={type} onChange={(event) => setType(event.target.value)}><option value="all">All types</option><option value="paynow">PayNow</option><option value="skillsfuture">SkillsFuture</option></select></label>
    </div>
    <div className="payment-workbench">
      <section className="payment-queue" aria-label="Payment submissions list"><div className="payment-queue-heading"><strong>{filtered.length} {filtered.length === 1 ? "submission" : "submissions"}</strong><span>Newest first</span></div>
        {filtered.length ? <div className="payment-queue-items">{filtered.map((review) => {
          const participant = enrollments.find((item) => item.id === review.enrollment_id);
          return <button key={review.id} type="button" className={`payment-queue-item ${selected?.id === review.id ? "is-selected" : ""}`} aria-current={selected?.id === review.id ? "true" : undefined} onClick={() => setSelectedId(review.id)}>
            <span className="payment-queue-item-top"><strong>{participant?.full_name || "Participant"}</strong><PaymentStatusBadge status={review.status} /></span>
            <span className="payment-queue-item-course">{participant?.course || "Course not recorded"}</span>
            <span className="payment-queue-item-bottom"><span>{review.invoice?.number || "Invoice pending"}</span><strong>{amount(review.extracted?.amount)}</strong></span>
            <span className="payment-queue-item-bottom"><span>{day(review.created_at)}</span><span>{(review.reasons || []).length} review flags</span></span>
          </button>;
        })}</div> : <div className="payment-empty"><strong>No submissions match</strong><p>Try another search or status filter.</p><button type="button" className="text-button" onClick={() => { setQuery(""); setStatus("all"); setType("all"); }}>Clear filters</button></div>}
      </section>
      {selected ? <PaymentReviewDetail key={selected.id} review={selected} enrollment={enrollment} reviews={reviews} action={action} working={working} /> : <div className="payment-detail payment-empty"><strong>Select a submission</strong><p>Payment screenshots will appear here when customers upload them.</p></div>}
    </div>
  </div>;
}
