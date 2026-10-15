import React, { useEffect, useRef, useState } from "react";
import {
  apiBaseUrl,
  documentUrl,
  postJson,
  requestJson,
  setCsrfToken,
} from "../services/chatApi.js";

const money = (value) =>
  new Intl.NumberFormat("en-SG", { style: "currency", currency: "SGD" }).format(
    Number(value || 0),
  );
const readable = (value) => String(value || "—").replaceAll("_", " ");
const date = (value) =>
  value
    ? new Intl.DateTimeFormat("en-SG", { dateStyle: "medium" }).format(
        new Date(value),
      )
    : "—";
const normal = (value) =>
  String(value || "")
    .toLowerCase()
    .replaceAll(" ", "_");

function Badge({ children }) {
  return (
    <span className={`status-badge status-badge--${normal(children)}`}>
      {readable(children)}
    </span>
  );
}
function Empty({ children }) {
  return <div className="empty-state">{children}</div>;
}

export default function StaffPortal() {
  const [session, setSession] = useState(null);
  const [checking, setChecking] = useState(true);
  const [sessionError, setSessionError] = useState("");
  useEffect(() => {
    requestJson("/api/staff/session")
      .then((result) => {
        setCsrfToken(result.csrf_token);
        if (result.authenticated) setSession(result);
      })
      .catch((error) => {
        if (error.status !== 401) setSessionError(error.message);
      })
      .finally(() => setChecking(false));
  }, []);

  async function logout() {
    try {
      await postJson("/api/staff/logout", {});
      setCsrfToken("");
      setSession(null);
      setSessionError("");
    } catch (error) {
      setSessionError(error.message);
    }
  }
  if (checking)
    return (
      <main className="login-page">
        <p role="status">Checking your staff session…</p>
      </main>
    );
  if (!session)
    return (
      <StaffLogin
        error={sessionError}
        onLogin={(result) => {
          setCsrfToken(result.csrf_token);
          setSession(result);
          setSessionError("");
        }}
      />
    );
  return (
    <StaffWorkspace
      session={session}
      onLogout={logout}
      sessionError={sessionError}
    />
  );
}

function StaffLogin({ onLogin, error: sessionError }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function login(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await postJson("/api/staff/login", {
        username: username.trim(),
        password,
      });
      setPassword("");
      onLogin(result);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-page">
      <div className="login-intro">
        <a href="/" className="brand-link">
          Q&amp;M Academy
        </a>
        <span className="eyebrow">Course administration</span>
        <h1>A clear view of every enrollment.</h1>
        <p>
          Manage participant enquiries, course intakes, and accounts in one
          place.
        </p>
        <a className="back-link" href="/">
          ← Return to participant chat
        </a>
      </div>
      <section className="login-card" aria-labelledby="login-title">
        <span className="eyebrow">Staff &amp; accounting team</span>
        <h2 id="login-title">Sign in to your workspace</h2>
        <p className="muted">
          Use the staff account provided by your administrator.
        </p>
        <form className="stack-form" onSubmit={login}>
          <label>
            Username
            <input
              name="username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
              autoFocus
            />
          </label>
          <label>
            Password
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          {(error || sessionError) && (
            <p className="notice-error" role="alert">
              {error || sessionError}
            </p>
          )}
          <button className="primary-button" disabled={busy} type="submit">
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </section>
    </main>
  );
}

function StaffWorkspace({ session, onLogout, sessionError }) {
  const [tab, setTab] = useState("overview");
  const [data, setData] = useState({
    enrollments: [],
    reviews: [],
    leads: [],
    escalations: [],
    course_dates: [],
    content: null,
    dashboard: {},
    master_invoice: null,
  });
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selectedEnrollment, setSelectedEnrollment] = useState(null);
  const requestNumber = useRef(0);
  const financeAccess = ["admin", "accountant"].includes(session.role);
  const operationsAccess = ["admin", "operations"].includes(session.role);
  const tabs = [
    { id: "overview", label: "Overview", symbol: "◫" },
    { id: "enrollments", label: "Enrollments", symbol: "☷" },
    ...(financeAccess
      ? [{ id: "accounts", label: "Accounts", symbol: "$" }]
      : []),
    { id: "delivery", label: "Messages & delivery", symbol: "\u2197" },
    { id: "quality", label: "Quality review", symbol: "\u2713" },
    ...(operationsAccess
      ? [
          { id: "contacts", label: "Leads & escalations", symbol: "◎" },
          { id: "schedule", label: "Course dates", symbol: "▦" },
          { id: "content", label: "Course & FAQ content", symbol: "≡" },
        ]
      : []),
  ];
  const titles = {
    delivery: "Messages & delivery",
    quality: "Quality review",
    overview: "Course administration",
    enrollments: "Enrollments",
    accounts: "Accounts & payment review",
    contacts: "Leads & escalations",
    schedule: "Course dates",
    content: "Course & FAQ content",
  };
  const descriptions = {
    delivery: "Review failed messages and recover interrupted processing.",
    quality: "Measure accuracy using staff-reviewed samples.",
    overview: "Keep every participant moving from first enquiry to receipt.",
    enrollments: "Review participant records and their current course status.",
    accounts: "Review evidence, issue receipts, and approve credit notes.",
    contacts:
      "Follow up with prospective participants and resolve staff cases.",
    schedule: "Publish available intakes and manage places on each course.",
    content: "Keep the assistant’s course information and answers current.",
  };

  async function loadData() {
    const number = ++requestNumber.current;
    setLoading(true);
    setError("");
    const paths = ["overview", "enrollments", "accounts"].includes(tab)
      ? [
          "dashboard",
          "enrollments",
          ...(financeAccess
            ? [
                "payment-reviews",
                ...(tab === "accounts" ? ["master-invoice/status"] : []),
              ]
            : []),
        ]
      : tab === "delivery"
        ? ["deliveries", "inbox"]
        : tab === "quality"
          ? ["quality"]
          : tab === "contacts"
            ? ["leads", "escalations"]
            : tab === "schedule"
              ? ["course-dates"]
              : ["content"];
    const results = await Promise.allSettled(
      paths.map((path) => requestJson(`/api/staff/${path}`)),
    );
    if (number !== requestNumber.current) return;
    const next = {};
    const failures = [];
    results.forEach((result, index) => {
      if (result.status === "rejected") {
        failures.push(result.reason.message);
        return;
      }
      const payload = result.value;
      if (paths[index] === "dashboard") next.dashboard = payload;
      else Object.assign(next, payload);
    });
    setData((current) => ({ ...current, ...next }));
    if (next.enrollments)
      setSelectedEnrollment((current) =>
        current
          ? next.enrollments.find((item) => item.id === current.id) || current
          : null,
      );
    setError([...new Set(failures)].join(" "));
    setLoading(false);
  }
  useEffect(() => {
    setNotice("");
    setSelectedEnrollment(null);
    loadData();
    return () => {
      requestNumber.current += 1;
    };
  }, [tab]);

  async function action(path, payload, success, method = "POST") {
    setWorking(true);
    setError("");
    setNotice("");
    try {
      const result = await postJson(path, payload, method);
      if (result.enrollment) setSelectedEnrollment(result.enrollment);
      await loadData();
      setNotice(success);
      return result;
    } catch (requestError) {
      setError(requestError.message);
      return null;
    } finally {
      setWorking(false);
    }
  }
  async function openEnrollment(id) {
    setWorking(true);
    setError("");
    try {
      setSelectedEnrollment(await requestJson(`/api/staff/enrollments/${id}`));
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setWorking(false);
    }
  }
  return (
    <div className="staff-layout">
      <aside className="staff-sidebar">
        <a href="/staff" className="staff-brand">
          <span className="staff-brand-mark">Q</span>
          <span>
            Q&amp;M Academy<small>Staff workspace</small>
          </span>
        </a>
        <nav aria-label="Staff workspace">
          {tabs.map((item) => (
            <button
              type="button"
              key={item.id}
              className={tab === item.id ? "active" : ""}
              aria-current={tab === item.id ? "page" : undefined}
              onClick={() => setTab(item.id)}
            >
              <span aria-hidden="true">{item.symbol}</span>
              {item.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <span className="user-avatar" aria-hidden="true">
            {session.username?.[0]?.toUpperCase() || "S"}
          </span>
          <div>
            <strong>{session.username}</strong>
            <small>{readable(session.role)}</small>
          </div>
          <button type="button" className="text-button" onClick={onLogout}>
            Sign out
          </button>
        </div>
        <a className="sidebar-public-link" href="/">
          Participant chat ↗
        </a>
      </aside>
      <main className="staff-main">
        <header className="workspace-header">
          <div>
            <span className="eyebrow">Q&amp;M Training course operations</span>
            <h1>{titles[tab]}</h1>
            <p className="muted">{descriptions[tab]}</p>
          </div>
          <button
            className="secondary-button"
            type="button"
            disabled={loading || working}
            onClick={loadData}
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </header>
        {(error || sessionError) && (
          <p className="notice-error" role="alert">
            {error || sessionError}
          </p>
        )}
        {notice && (
          <p className="notice-success" role="status">
            {notice}
          </p>
        )}
        <div aria-busy={loading || working}>
          {tab === "overview" && (
            <>
              <Metrics dashboard={data.dashboard} />
              <div className="overview-panels">
                <section className="workspace-card">
                  <h2>Enrollment progress</h2>
                  <div className="status-summary">
                    {Object.entries(data.dashboard.status_counts || {}).map(
                      ([status, count]) => (
                        <div key={status}>
                          <Badge>{status}</Badge>
                          <strong>{count}</strong>
                        </div>
                      ),
                    )}
                  </div>
                  {!Object.keys(data.dashboard.status_counts || {}).length && (
                    <Empty>Enrollment activity will appear here.</Empty>
                  )}
                </section>
                <section className="workspace-card">
                  <span className="eyebrow">Next actions</span>
                  <h2>Keep accounts up to date</h2>
                  <ul className="action-summary">
                    <li>
                      <span>Payment evidence awaiting review</span>
                      <strong>{data.dashboard.pending_reviews || 0}</strong>
                    </li>
                    <li>
                      <span>Paid enrollments awaiting a receipt</span>
                      <strong>{data.dashboard.pending_receipts || 0}</strong>
                    </li>
                    <li>
                      <span>Credit note requests</span>
                      <strong>
                        {data.dashboard.credit_note_requests || 0}
                      </strong>
                    </li>
                  </ul>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() =>
                      setTab(financeAccess ? "accounts" : "contacts")
                    }
                  >
                    {financeAccess ? "Open accounts" : "Open participant cases"}
                  </button>
                </section>
              </div>
              <EnrollmentList
                enrollments={data.enrollments.slice(0, 8)}
                onOpen={openEnrollment}
                compact
              />
            </>
          )}
          {tab === "enrollments" && (
            <EnrollmentList
              enrollments={data.enrollments}
              onOpen={openEnrollment}
            />
          )}
          {tab === "accounts" && (
            <>
              <Metrics dashboard={data.dashboard} accounts />
              <MasterInvoicePanel
                status={data.master_invoice}
                action={action}
                working={working}
              />
              <div className="section-heading">
                <h2>Payment review</h2>
                <a
                  className="secondary-button"
                  href={`${apiBaseUrl}/api/staff/reports/payments.csv`}
                >
                  Export payment report ↓
                </a>
              </div>
              <ReviewList
                reviews={data.reviews}
                enrollments={data.enrollments}
                action={action}
                working={working}
                canApprove={financeAccess}
              />
              <CreditNotes
                enrollments={data.enrollments}
                action={action}
                working={working}
                canApprove={financeAccess}
              />
              <EnrollmentList
                enrollments={data.enrollments}
                onOpen={openEnrollment}
                accounts
              />
            </>
          )}
          {selectedEnrollment &&
            ["overview", "enrollments", "accounts"].includes(tab) && (
              <EnrollmentDetail
                enrollment={selectedEnrollment.enrollment || selectedEnrollment}
                action={action}
                working={working}
                financeAccess={financeAccess}
                onClose={() => setSelectedEnrollment(null)}
              />
            )}
          {tab === "delivery" && (
            <DeliveryMonitor
              data={data}
              action={action}
              working={working}
              financeAccess={financeAccess}
            />
          )}
          {tab === "quality" && (
            <QualityPanel
              metrics={data.metrics || {}}
              action={action}
              working={working}
            />
          )}
          {tab === "contacts" && (
            <LeadWorkspace data={data} action={action} working={working} />
          )}
          {tab === "schedule" && (
            <CourseDates
              dates={data.course_dates}
              action={action}
              working={working}
            />
          )}
          {tab === "content" && data.content && (
            <ContentEditor
              content={data.content}
              action={action}
              working={working}
            />
          )}
        </div>
      </main>
    </div>
  );
}

function Metrics({ dashboard, accounts }) {
  const items = accounts
    ? [
        ["Outstanding balance", money(dashboard.outstanding_total)],
        ["Confirmed payments", money(dashboard.paid_total)],
        ["Overdue accounts", dashboard.overdue || 0],
        ["Pending receipts", dashboard.pending_receipts || 0],
      ]
    : [
        ["Total enrollments", dashboard.total_enrollments || 0],
        ["Confirmed payments", money(dashboard.paid_total)],
        ["Awaiting review", dashboard.pending_reviews || 0],
        ["Overdue accounts", dashboard.overdue || 0],
      ];
  return (
    <div className="metric-grid">
      {items.map(([label, value], index) => (
        <section
          className={`metric-card ${index === 0 ? "metric-card--primary" : ""}`}
          key={label}
        >
          <span>{label}</span>
          <strong>{value}</strong>
          <small>
            {index === 1 ? "Across all enrollments" : "Current course records"}
          </small>
        </section>
      ))}
    </div>
  );
}

function MasterInvoicePanel({ status, action, working }) {
  const value = status || {};
  const needsAttention =
    (value.pending_exports || 0) +
    (value.failed_exports || 0) +
    (value.needs_update || 0);
  return (
    <section className="workspace-card">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Private accounting export</span>
          <h2>Master Invoice List</h2>
        </div>
        <Badge>{needsAttention ? "Needs attention" : "Up to date"}</Badge>
      </div>
      <div className="master-invoice-summary">
        <div>
          <span>Last generated</span>
          <strong>{date(value.last_generated_at)}</strong>
        </div>
        <div>
          <span>Confirmed records</span>
          <strong>{value.confirmed_records || 0}</strong>
        </div>
        <div>
          <span>Pending exports / updates required</span>
          <strong>
            {(value.pending_exports || 0) + (value.needs_update || 0)}
          </strong>
        </div>
        <div>
          <span>Failed exports</span>
          <strong>{value.failed_exports || 0}</strong>
        </div>
      </div>
      <div className="button-row">
        {value.download_available && (
          <a
            className="primary-button"
            href={`${apiBaseUrl}/api/staff/master-invoice/download`}
          >
            Download Master Invoice List ↓
          </a>
        )}
        <button
          className="secondary-button"
          type="button"
          disabled={working}
          onClick={() =>
            action(
              "/api/staff/master-invoice/regenerate",
              {},
              "Master Invoice List regenerated from confirmed database records.",
            )
          }
        >
          Regenerate workbook
        </button>
        <button
          className="secondary-button"
          type="button"
          disabled={working || !needsAttention}
          onClick={() =>
            action(
              "/api/staff/master-invoice/retry",
              {},
              "Pending Master Invoice exports retried.",
            )
          }
        >
          Retry failed exports
        </button>
      </div>
      {Boolean(value.records?.length) && (
        <div className="table-scroll panel-section">
          <table>
            <thead>
              <tr>
                <th>Invoice / enrollment</th>
                <th>Participant</th>
                <th>Intake</th>
                <th>Payment split</th>
                <th>Export status</th>
                <th>
                  <span className="visually-hidden">Action</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {value.records.map((record) => (
                <tr key={record.enrollment_id}>
                  <td>
                    <strong>{record.invoice_number || "Pending number"}</strong>
                    <small>{record.enrollment_id}</small>
                  </td>
                  <td>{record.participant_name}</td>
                  <td>{date(record.selected_intake)}</td>
                  <td>
                    SkillsFuture {money(record.skillsfuture_amount)} + PayNow{" "}
                    {money(record.paynow_amount)}
                  </td>
                  <td>
                    <Badge>{record.status}</Badge>
                    <small>
                      {date(record.exported_at || record.updated_at)}
                    </small>
                    {record.failure_message && (
                      <small>{record.failure_message}</small>
                    )}
                  </td>
                  <td>
                    {record.retry_available && (
                      <button
                        className="text-button"
                        type="button"
                        disabled={working}
                        onClick={() =>
                          action(
                            `/api/staff/master-invoice/retry/${record.enrollment_id}`,
                            {},
                            "Master Invoice export retried.",
                          )
                        }
                      >
                        Retry
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function EnrollmentList({ enrollments, onOpen, compact, accounts }) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const options = [...new Set(enrollments.map((item) => item.status))];
  const list = enrollments.filter((item) => {
    const searchMatches = [
      item.full_name,
      item.email,
      item.mobile_number,
      item.invoice_document?.invoice_number,
      item.invoice?.number,
    ].some((value) =>
      String(value || "")
        .toLowerCase()
        .includes(query.toLowerCase()),
    );
    const overdue =
      item.invoice?.due_date &&
      new Date(item.invoice.due_date) < new Date() &&
      !["paid", "receipt_issued", "credit_note_issued", "cancelled"].includes(
        normal(item.status),
      );
    return (
      searchMatches &&
      (filter === "all" ||
        (filter === "overdue" && overdue) ||
        (filter === "pending_receipt" && normal(item.status) === "paid") ||
        item.status === filter)
    );
  });
  return (
    <section className="workspace-card enrollment-list">
      <div className="section-heading">
        <h2>
          {compact
            ? "Recent enrollments"
            : accounts
              ? "Payment status by enrollment"
              : "Participant enrollments"}
        </h2>
        <span className="muted small-copy">{list.length} records</span>
      </div>
      {!compact && (
        <div className="table-controls">
          <label>
            <span className="visually-hidden">Search enrollments</span>
            <input
              type="search"
              placeholder="Search name, email, or invoice"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <label>
            <span className="visually-hidden">Filter status</span>
            <select
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
            >
              <option value="all">All statuses</option>
              {accounts && (
                <>
                  <option value="overdue">Overdue</option>
                  <option value="pending_receipt">Pending receipt</option>
                </>
              )}
              {options.map((status) => (
                <option key={status} value={status}>
                  {readable(status)}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Participant</th>
              <th scope="col">Course date</th>
              <th scope="col">Invoice</th>
              <th scope="col">Status</th>
              <th scope="col">
                <span className="visually-hidden">View record</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {list.map((enrollment) => (
              <tr key={enrollment.id}>
                <td>
                  <strong>{enrollment.full_name}</strong>
                  <small>{enrollment.email}</small>
                </td>
                <td>
                  {enrollment.intake_display || date(enrollment.course_date)}
                  {enrollment.intake_is_demo && <small>Demo intake</small>}
                </td>
                <td>
                  {enrollment.invoice_document?.invoice_number ||
                    enrollment.invoice?.number ||
                    "Not issued"}
                  <small>
                    {enrollment.invoice_document
                      ? readable(enrollment.invoice_document.status)
                      : enrollment.invoice
                        ? money(enrollment.invoice.net_payable)
                        : ""}
                  </small>
                </td>
                <td>
                  <Badge>{enrollment.status}</Badge>
                </td>
                <td>
                  <button
                    className="text-button"
                    type="button"
                    onClick={() => onOpen(enrollment.id)}
                    aria-label={`View enrollment for ${enrollment.full_name}`}
                  >
                    View →
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!list.length && <Empty>No enrollments match this view.</Empty>}
    </section>
  );
}

export function EnrollmentDetail({
  enrollment,
  action,
  working,
  financeAccess,
  onClose,
}) {
  const [reason, setReason] = useState("");
  const invoice = enrollment.invoice;
  const invoiceDocument = enrollment.invoice_document;
  const invoiceEmail = enrollment.invoice_email;
  const path = `/api/staff/enrollments/${enrollment.id}`;
  return (
    <section
      className="workspace-card record-detail"
      aria-labelledby="record-title"
    >
      <div className="section-heading">
        <div>
          <span className="eyebrow">Participant record</span>
          <h2 id="record-title">{enrollment.full_name}</h2>
        </div>
        <button type="button" className="secondary-button" onClick={onClose}>
          Close record
        </button>
      </div>
      <div className="record-columns">
        <div>
          <Badge>{enrollment.status}</Badge>
          <dl className="detail-list">
            <div>
              <dt>Email</dt>
              <dd>{enrollment.email}</dd>
            </div>
            <div>
              <dt>Mobile</dt>
              <dd>{enrollment.mobile_number || "—"}</dd>
            </div>
            <div>
              <dt>NRIC</dt>
              <dd>{enrollment.nric_masked || "Protected"}</dd>
            </div>
            <div>
              <dt>Course</dt>
              <dd>{enrollment.course || "Course not selected"}</dd>
            </div>
            <div>
              <dt>Selected intake</dt>
              <dd>
                {enrollment.intake_display || date(enrollment.course_date)}
                {enrollment.intake_is_demo ? " (Demo)" : ""}
              </dd>
            </div>
            {enrollment.previous_intake_display && (
              <div>
                <dt>Previous intake</dt>
                <dd>{enrollment.previous_intake_display}</dd>
              </div>
            )}
            {enrollment.cancellation_requested_at && (
              <div>
                <dt>Cancellation requested</dt>
                <dd>{date(enrollment.cancellation_requested_at)}</dd>
              </div>
            )}
            {enrollment.cancelled_at && (
              <div>
                <dt>Cancelled</dt>
                <dd>{date(enrollment.cancelled_at)}</dd>
              </div>
            )}
            {enrollment.cancellation_reason && (
              <div>
                <dt>Cancellation reason</dt>
                <dd>{enrollment.cancellation_reason}</dd>
              </div>
            )}
            {enrollment.reschedule_requested_at && (
              <div>
                <dt>Reschedule requested</dt>
                <dd>{date(enrollment.reschedule_requested_at)}</dd>
              </div>
            )}
            {enrollment.rescheduled_at && (
              <div>
                <dt>Rescheduled</dt>
                <dd>{date(enrollment.rescheduled_at)}</dd>
              </div>
            )}
            {enrollment.refund_review_required && (
              <div>
                <dt>Payment/refund review</dt>
                <dd>Staff confirmation required</dd>
              </div>
            )}
            <div>
              <dt>Payment allocation</dt>
              <dd>
                {(enrollment.payment_allocation || [])
                  .map(
                    (item) =>
                      `${item.method}${item.amount ? ` ${money(item.amount)}` : ""}`,
                  )
                  .join(" + ") || "Not selected"}
              </dd>
            </div>
            <div>
              <dt>Payment verification</dt>
              <dd>
                {enrollment.payment_verification_status ||
                  "Awaiting staff verification"}
              </dd>
            </div>
          </dl>
        </div>
        <div>
          <h3>Invoice &amp; payment</h3>
          {invoice ? (
            <dl className="detail-list">
              <div>
                <dt>Invoice number</dt>
                <dd>{invoice.number}</dd>
              </div>
              <div>
                <dt>Course fee</dt>
                <dd>{money(invoice.course_fee)}</dd>
              </div>
              <div>
                <dt>SkillsFuture credits</dt>
                <dd>{money(invoice.skillsfuture_amount)}</dd>
              </div>
              <div>
                <dt>PayNow balance</dt>
                <dd>{money(invoice.net_payable)}</dd>
              </div>
              <div>
                <dt>Due date</dt>
                <dd>{date(invoice.due_date)}</dd>
              </div>
            </dl>
          ) : (
            <p className="muted">
              The official template PDF is tracked separately from payment
              records.
            </p>
          )}
          {invoiceDocument && (
            <dl className="detail-list">
              <div>
                <dt>Template invoice</dt>
                <dd>{invoiceDocument.invoice_number}</dd>
              </div>
              <div>
                <dt>Generation status</dt>
                <dd>
                  <Badge>{invoiceDocument.status}</Badge>
                </dd>
              </div>
              <div>
                <dt>Document version</dt>
                <dd>{invoiceDocument.version || "Not generated"}</dd>
              </div>
              {invoiceDocument.failure_message && (
                <div>
                  <dt>Staff action</dt>
                  <dd>{invoiceDocument.failure_message}</dd>
                </div>
              )}
            </dl>
          )}
          {invoiceEmail && (
            <dl className="detail-list">
              <div><dt>Email delivery</dt><dd><Badge>{invoiceEmail.status}</Badge></dd></div>
              <div><dt>Recipient</dt><dd>{invoiceEmail.recipient_masked}</dd></div>
              <div><dt>Attempts</dt><dd>{invoiceEmail.attempt_count || 0}</dd></div>
              {invoiceEmail.sent_at && <div><dt>Sent</dt><dd>{date(invoiceEmail.sent_at)}</dd></div>}
              {invoiceEmail.failure_category && <div><dt>Failure category</dt><dd>{readable(invoiceEmail.failure_category)}</dd></div>}
            </dl>
          )}
          <div className="button-row">
            {financeAccess && (
              <button
                className="secondary-button"
                disabled={working}
                type="button"
                onClick={() =>
                  action(
                    invoiceDocument
                      ? `${path}/invoice/retry`
                      : `${path}/invoice`,
                    {},
                    invoiceDocument
                      ? "Invoice generation retried."
                      : "Invoice generation requested.",
                  )
                }
              >
                {invoiceDocument
                  ? "Generate / regenerate invoice"
                  : "Generate invoice"}
              </button>
            )}
            {financeAccess && invoiceDocument?.download_url && (
              <a
                className="primary-button"
                href={`${apiBaseUrl}${invoiceDocument.download_url}`}
                target="_blank"
                rel="noreferrer"
              >
                Download invoice PDF â†—
              </a>
            )}
            {financeAccess && invoiceDocument?.status === "generated" && (
              <button
                className="secondary-button"
                disabled={working || invoiceEmail?.can_resend === false}
                type="button"
                onClick={() => action(`${path}/invoice-email/resend`, {}, "Invoice email resent.")}
              >
                Resend Invoice Email
              </button>
            )}
            {financeAccess && invoiceEmail?.id && ["failed", "disabled", "sending"].includes(invoiceEmail.status) && (
              <button
                className="secondary-button"
                disabled={working}
                type="button"
                onClick={() => action(`${path}/invoice-email/retry`, { delivery_id: invoiceEmail.id }, "Invoice email retry requested.")}
              >
                Retry Invoice Email
              </button>
            )}
            {financeAccess && normal(enrollment.status) === "paid" && (
              <button
                className="primary-button"
                disabled={working}
                type="button"
                onClick={() =>
                  action(
                    `${path}/receipt`,
                    {},
                    "Receipt issued and email delivery queued.",
                  )
                }
              >
                Issue receipt
              </button>
            )}
            {(enrollment.documents || []).some(
              (document) => document.kind === "receipt",
            ) && (
              <button
                className="secondary-button"
                disabled={working}
                type="button"
                onClick={() =>
                  action(
                    `${path}/documents/resend`,
                    { kind: "receipt" },
                    "Receipt delivery queued.",
                  )
                }
              >
                Resend receipt
              </button>
            )}
          </div>
        </div>
      </div>
      <h3>Documents &amp; evidence</h3>
      {financeAccess &&
        Number(invoice?.skillsfuture_amount) > 0 &&
        !enrollment.payments?.some(
          (item) => item.method === "skillsfuture",
        ) && (
          <SkillsFutureConfirmation
            enrollment={enrollment}
            action={action}
            working={working}
          />
        )}
      <div className="document-links">
        {(enrollment.documents || [])
          .filter((document) => document.kind !== "invoice")
          .map((document) => (
            <a
              href={documentUrl(document.id)}
              key={document.id}
              target="_blank"
              rel="noreferrer"
            >
              {readable(document.kind)}{" "}
              <span>{document.number || "Open PDF"} ↗</span>
            </a>
          ))}
        {(enrollment.payments || enrollment.evidence || [])
          .filter((payment) => payment.evidence_id || payment.id)
          .map((payment) => (
            <a
              key={`evidence-${payment.evidence_id || payment.id}`}
              href={`${apiBaseUrl}/api/staff/evidence/${payment.evidence_id || payment.id}`}
              target="_blank"
              rel="noreferrer"
            >
              Payment evidence <span>{readable(payment.status)} ↗</span>
            </a>
          ))}
      </div>
      {financeAccess &&
        !["paid", "receipt_issued", "credit_note_issued"].includes(
          normal(enrollment.status),
        ) && (
          <form
            className="inline-form panel-section"
            onSubmit={async (event) => {
              event.preventDefault();
              const result = await action(
                `${path}/credit-notes`,
                { reason },
                "Credit note request submitted for review.",
              );
              if (result) setReason("");
            }}
          >
            <label>
              Credit note reason
              <input
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Reason for cancellation or unpaid enrollment"
                required
              />
            </label>
            <button
              className="secondary-button"
              type="submit"
              disabled={working}
            >
              Request credit note
            </button>
          </form>
        )}
    </section>
  );
}

function ReviewList({ reviews, enrollments, action, working, canApprove }) {
  const pending = reviews.filter((review) =>
    ["pending", "pending_review", "flagged", "needs_review", "open"].includes(
      normal(review.status),
    ),
  );
  return (
    <div className="review-grid">
      {pending.map((review) => (
        <PaymentReview
          key={review.id}
          review={review}
          enrollment={enrollments.find(
            (item) => item.id === review.enrollment_id,
          )}
          action={action}
          working={working}
          canApprove={canApprove}
        />
      ))}
      {!pending.length && (
        <Empty>No payment screenshots are awaiting staff review.</Empty>
      )}
    </div>
  );
}

function PaymentReview({ review, enrollment, action, working, canApprove }) {
  const [note, setNote] = useState("");
  const [reference, setReference] = useState("");
  const extracted = review.extracted || {};
  const [verifiedAmount, setVerifiedAmount] = useState(extracted.amount || "");
  const [transactionDate, setTransactionDate] = useState(
    extracted.transaction_date || "",
  );
  return (
    <article className="workspace-card review-card">
      <div className="section-heading">
        <h3>{enrollment?.full_name || "Payment submission"}</h3>
        <Badge>{review.status}</Badge>
      </div>
      <p className="muted small-copy">
        {enrollment?.invoice?.number || `Enrollment ${review.enrollment_id}`} ·{" "}
        {date(review.created_at)}
      </p>
      <p className="review-reasons">
        {Array.isArray(review.reasons)
          ? review.reasons.join(" · ")
          : review.reasons || "Staff confirmation is required."}
      </p>
      <dl className="detail-list">
        <div>
          <dt>Expected payment</dt>
          <dd>
            {enrollment?.invoice
              ? money(
                  review.evidence?.payment_type === "skillsfuture"
                    ? enrollment.invoice.skillsfuture_amount
                    : enrollment.invoice.net_payable,
                )
              : "—"}
          </dd>
        </div>
        <div>
          <dt>Detected amount</dt>
          <dd>
            {extracted.amount != null ? money(extracted.amount) : "Unreadable"}
          </dd>
        </div>
        <div>
          <dt>Recipient</dt>
          <dd>
            {extracted.recipient ||
              extracted.payee ||
              extracted.uen ||
              "Unreadable"}
          </dd>
        </div>
        <div>
          <dt>Reference</dt>
          <dd>
            {extracted.reference ||
              extracted.transaction_reference ||
              "Unreadable"}
          </dd>
        </div>
      </dl>
      <a
        className="secondary-button"
        href={`${apiBaseUrl}/api/staff/evidence/${review.evidence_id}`}
        target="_blank"
        rel="noreferrer"
      >
        Open original screenshot ↗
      </a>
      {canApprove && (
        <form
          className="stack-form panel-section"
          onSubmit={(event) => {
            event.preventDefault();
            action(
              `/api/staff/payment-reviews/${review.id}/decision`,
              {
                decision: event.nativeEvent.submitter?.value || "approve",
                note,
                ...(reference.trim() ? { reference: reference.trim() } : {}),
                ...(verifiedAmount ? { amount: verifiedAmount } : {}),
                ...(transactionDate
                  ? { transaction_date: transactionDate }
                  : {}),
              },
              "Payment review decision saved.",
            );
          }}
        >
          <label>
            Verified amount (SGD)
            <input
              type="number"
              min="0"
              step="0.01"
              value={verifiedAmount}
              onChange={(event) => setVerifiedAmount(event.target.value)}
              placeholder="Amount checked against the bank or claim record"
            />
          </label>
          <label>
            Verified transaction date
            <input
              type="date"
              max={new Date().toISOString().slice(0, 10)}
              value={transactionDate}
              onChange={(event) => setTransactionDate(event.target.value)}
            />
          </label>
          <label>
            Verified transaction reference
            <input
              value={reference}
              onChange={(event) => setReference(event.target.value)}
              placeholder="Enter a reference if it was unreadable"
            />
          </label>
          <label>
            Review note
            <textarea
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="Record the checks you made"
              required
              rows={2}
            />
          </label>
          <div className="button-row">
            <button
              className="primary-button"
              type="submit"
              value="approve"
              disabled={working}
            >
              Approve payment
            </button>
            <button
              className="danger-button"
              type="submit"
              value="reject"
              disabled={working}
            >
              Reject evidence
            </button>
          </div>
        </form>
      )}
    </article>
  );
}

function CreditNotes({ enrollments, action, working, canApprove }) {
  const requests = enrollments
    .flatMap((enrollment) =>
      (enrollment.credit_notes || []).map((credit) => ({
        ...credit,
        enrollment,
      })),
    )
    .filter((credit) =>
      ["requested", "pending", "pending_review"].includes(
        normal(credit.status),
      ),
    );
  return (
    <section className="workspace-card">
      <h2>Credit note requests</h2>
      {requests.map((credit) => (
        <CreditDecision
          key={credit.id}
          credit={credit}
          action={action}
          working={working}
          canApprove={canApprove}
        />
      ))}
      {!requests.length && (
        <Empty>No credit notes are awaiting approval.</Empty>
      )}
    </section>
  );
}

function CreditDecision({ credit, action, working, canApprove }) {
  const [note, setNote] = useState("");
  return (
    <div className="credit-row">
      <div>
        <strong>{credit.enrollment.full_name}</strong>
        <p className="muted small-copy">
          {credit.reason} · {credit.enrollment.invoice?.number}
        </p>
      </div>
      {canApprove && (
        <form
          className="inline-form"
          onSubmit={(event) => {
            event.preventDefault();
            action(
              `/api/staff/credit-notes/${credit.id}/decision`,
              {
                decision: event.nativeEvent.submitter?.value || "approve",
                note,
              },
              "Credit note decision saved.",
            );
          }}
        >
          <label>
            <span className="visually-hidden">Credit note review note</span>
            <input
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="Review note"
              required
            />
          </label>
          <button
            className="primary-button"
            type="submit"
            value="approve"
            disabled={working}
          >
            Approve credit note
          </button>
          <button
            className="danger-button"
            type="submit"
            value="reject"
            disabled={working}
          >
            Reject
          </button>
        </form>
      )}
    </div>
  );
}

function LeadWorkspace({ data, action, working }) {
  const [caseDetail, setCaseDetail] = useState(null);
  const [caseError, setCaseError] = useState("");
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState("all");
  async function openCase(id) {
    setLoading(true);
    setCaseError("");
    try {
      setCaseDetail(await requestJson(`/api/staff/cases/${id}`));
    } catch (error) {
      setCaseError(error.message);
    } finally {
      setLoading(false);
    }
  }
  const escalations = data.escalations.filter(
    (item) => item.status !== "resolved",
  );
  return (
    <>
      <section className="workspace-card">
        <div className="section-heading">
          <h2>Open escalations</h2>
          <span className="status-badge">{escalations.length} open</span>
        </div>
        {escalations.map((item) => (
          <EscalationRow
            key={item.id}
            escalation={item}
            action={action}
            working={working}
            onOpen={() => openCase(item.conversation_id)}
          />
        ))}
        {!escalations.length && (
          <Empty>No participant cases need staff attention.</Empty>
        )}
      </section>
      <section className="workspace-card">
        <div className="section-heading">
          <h2>Lead follow-up</h2>
          <label>
            <span className="visually-hidden">Filter leads</span>
            <select
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
            >
              <option value="all">All leads</option>
              <option value="awaiting_response">Awaiting response</option>
              <option value="escalated">Escalated</option>
              <option value="declined">Declined</option>
            </select>
          </label>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Participant</th>
                <th>Status</th>
                <th>Last activity</th>
                <th>Follow-ups</th>
                <th>Consent</th>
                <th>
                  <span className="visually-hidden">Open case</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {data.leads
                .filter((lead) => filter === "all" || lead.status === filter)
                .map((lead) => (
                  <tr key={lead.id}>
                    <td>
                      <strong>{lead.full_name || "New enquiry"}</strong>
                      <small>
                        {lead.email || lead.recipient || lead.channel}
                      </small>
                    </td>
                    <td>
                      <Badge>{lead.status}</Badge>
                    </td>
                    <td>
                      {date(lead.last_inbound_at || lead.last_contact_at)}
                    </td>
                    <td>{lead.followup_count || 0} / 3</td>
                    <td>
                      {lead.consent?.granted ? "Accepted" : "Not accepted"}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => openCase(lead.conversation_id)}
                      >
                        Open case →
                      </button>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
        {!data.leads.length && (
          <Empty>Lead records will appear after the first enquiry.</Empty>
        )}
      </section>
      {loading && <p role="status">Loading participant case…</p>}
      {caseError && (
        <p className="notice-error" role="alert">
          {caseError}
        </p>
      )}
      {caseDetail && (
        <CaseDetail
          detail={caseDetail}
          action={action}
          working={working}
          onClose={() => setCaseDetail(null)}
        />
      )}
    </>
  );
}

function EscalationRow({ escalation, action, working, onOpen }) {
  const [note, setNote] = useState("");
  return (
    <article className="escalation-row">
      <div>
        <Badge>{escalation.category}</Badge>
        <h3>{escalation.reason}</h3>
        <p className="muted small-copy">Opened {date(escalation.created_at)}</p>
        <button type="button" className="text-button" onClick={onOpen}>
          Read participant case →
        </button>
      </div>
      <form
        className="inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          action(
            `/api/staff/escalations/${escalation.id}`,
            { status: "resolved", note },
            "Escalation resolved.",
            "PATCH",
          );
        }}
      >
        <label>
          <span className="visually-hidden">Resolution note</span>
          <input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Resolution note"
            required
          />
        </label>
        <button className="secondary-button" type="submit" disabled={working}>
          Resolve case
        </button>
      </form>
    </article>
  );
}

function CaseDetail({ detail, action, working, onClose }) {
  const [reply, setReply] = useState("");
  const [intakes, setIntakes] = useState([]);
  const [intakeId, setIntakeId] = useState("");
  useEffect(() => {
    requestJson("/api/staff/course-dates")
      .then((result) => setIntakes(result.course_dates))
      .catch(() => setIntakes([]));
  }, []);
  const lead = detail.lead;
  return (
    <section className="workspace-card">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Participant case</span>
          <h2>{lead?.full_name || lead?.recipient || "Course enquiry"}</h2>
        </div>
        <button className="secondary-button" onClick={onClose} type="button">
          Close case
        </button>
      </div>
      <p className="muted small-copy">
        {lead?.email} · {lead?.channel} · Consent{" "}
        {lead?.consent?.granted ? "accepted" : "not accepted"}
      </p>
      {detail.finance && (
        <section className="panel-section">
          <h3>Enrollment &amp; documents</h3>
          <p>
            {detail.finance.status} ? Outstanding{" "}
            {money(detail.finance.outstanding_amount)}
          </p>
          <div className="document-links">
            {detail.finance.documents.map((document) => (
              <a
                key={document.id}
                href={documentUrl(document.id)}
                target="_blank"
                rel="noreferrer"
              >
                {readable(document.kind)} {document.number} ?
              </a>
            ))}
          </div>
          <form
            className="inline-form panel-section"
            onSubmit={(event) => {
              event.preventDefault();
              action(
                `/api/staff/cases/${lead.conversation_id}/assign-course-date`,
                { course_date_id: intakeId },
                "Course date updated; the participant will receive a confirmation request if needed.",
              );
            }}
          >
            <label>
              Course intake
              <select
                value={intakeId}
                onChange={(event) => setIntakeId(event.target.value)}
                required
              >
                <option value="">Choose an open intake</option>
                {intakes
                  .filter((item) => item.status === "open")
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {date(item.date)} ({item.available_places} places)
                    </option>
                  ))}
              </select>
            </label>
            <button
              className="secondary-button"
              disabled={working || !intakeId}
            >
              Assign course date
            </button>
          </form>
        </section>
      )}
      {detail.followups?.length > 0 && (
        <section className="panel-section">
          <h3>Follow-up history</h3>
          {detail.followups.map((item, index) => (
            <p key={index}>
              Attempt {item.step} ? {date(item.created_at)}
            </p>
          ))}
        </section>
      )}
      <div className="case-messages">
        {(detail.messages || []).map((message) => (
          <article
            key={message.id}
            className={`case-message case-message--${message.role}`}
          >
            <strong>
              {message.role === "assistant"
                ? "Assistant"
                : message.role === "staff"
                  ? "Staff"
                  : "Participant"}
            </strong>
            <p>{message.content || message.body}</p>
            <small>{date(message.timestamp || message.created_at)}</small>
          </article>
        ))}
      </div>
      <div className="button-row panel-section">
        {lead?.id && (
          <>
            <button
              type="button"
              className="secondary-button"
              disabled={working}
              onClick={() =>
                action(
                  `/api/staff/leads/${lead.id}`,
                  { status: "awaiting_response" },
                  "Lead returned to follow-up.",
                  "PATCH",
                )
              }
            >
              Return to follow-up
            </button>
            <button
              type="button"
              className="danger-button"
              disabled={working}
              onClick={() =>
                action(
                  `/api/staff/leads/${lead.id}`,
                  { status: "declined" },
                  "Lead marked declined; follow-ups stopped.",
                  "PATCH",
                )
              }
            >
              Mark declined
            </button>
          </>
        )}
      </div>
      <form
        className="stack-form panel-section"
        onSubmit={async (event) => {
          event.preventDefault();
          const result = await action(
            `/api/staff/cases/${lead.conversation_id}/reply`,
            { body: reply },
            "Staff reply queued for delivery.",
          );
          if (result) setReply("");
        }}
      >
        <label>
          Reply to participant
          <textarea
            value={reply}
            onChange={(event) => setReply(event.target.value)}
            rows={3}
            required
            placeholder="Write your response"
          />
        </label>
        <button
          className="primary-button"
          type="submit"
          disabled={working || !lead?.conversation_id}
        >
          Send reply
        </button>
      </form>
      {detail.audit?.length > 0 && (
        <details className="audit-log panel-section">
          <summary>Case activity ({detail.audit.length})</summary>
          <ul>
            {detail.audit.map((event, index) => (
              <li key={event.id || index}>
                {readable(event.action || event.event_type || event.type)} ·{" "}
                {date(event.created_at)}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function CourseDates({ dates, action, working }) {
  const [selected, setSelected] = useState(null);
  return (
    <>
      <section className="workspace-card">
        <div className="section-heading">
          <h2>Published intakes</h2>
          <span className="muted small-copy">{dates.length} intakes</span>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Course dates</th>
                <th>Status</th>
                <th>Enrollment</th>
                <th>Paid</th>
                <th>Available places</th>
                <th>
                  <span className="visually-hidden">Edit intake</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {dates.map((intake) => (
                <tr key={intake.id}>
                  <td>
                    <strong>
                      {date(intake.date)} – {date(intake.end_date)}
                    </strong>
                    <small>{intake.label}</small>
                  </td>
                  <td>
                    <Badge>{intake.status}</Badge>
                  </td>
                  <td>
                    {intake.enrollment_count || 0} / {intake.capacity}
                  </td>
                  <td>{intake.paid_count || 0}</td>
                  <td>
                    {intake.available_places ??
                      Math.max(
                        0,
                        intake.capacity - (intake.enrollment_count || 0),
                      )}
                  </td>
                  <td>
                    <button
                      className="text-button"
                      type="button"
                      onClick={() => setSelected(intake)}
                    >
                      Edit →
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!dates.length && (
          <Empty>
            No course dates are published. Add an intake to make enrollment
            available.
          </Empty>
        )}
      </section>
      <IntakeEditor
        key={selected?.id || "new"}
        intake={selected}
        action={action}
        working={working}
        onDone={() => setSelected(null)}
      />
    </>
  );
}

function IntakeEditor({ intake, action, working, onDone }) {
  const [form, setForm] = useState({
    date: intake?.date?.slice(0, 10) || "",
    end_date: intake?.end_date?.slice(0, 10) || "",
    label: intake?.label || "",
    capacity: intake?.capacity || 20,
    status: intake?.configured_status || "open",
  });
  const update = (key) => (event) =>
    setForm((current) => ({ ...current, [key]: event.target.value }));
  async function save(event) {
    event.preventDefault();
    const payload = { ...form, capacity: Number(form.capacity) };
    if (!payload.end_date) delete payload.end_date;
    const result = await action(
      `/api/staff/course-dates${intake ? `/${intake.id}` : ""}`,
      payload,
      intake ? "Course intake updated." : "Course intake published.",
      intake ? "PATCH" : "POST",
    );
    if (result) {
      setForm({
        date: "",
        end_date: "",
        label: "",
        capacity: 20,
        status: "open",
      });
      onDone();
    }
  }
  return (
    <section className="workspace-card">
      <div className="section-heading">
        <h2>{intake ? "Edit course intake" : "Add a course intake"}</h2>
        {intake && (
          <button className="text-button" type="button" onClick={onDone}>
            Cancel edit
          </button>
        )}
      </div>
      <form className="intake-form" onSubmit={save}>
        <label>
          First training day
          <input
            type="date"
            value={form.date}
            onChange={update("date")}
            required
          />
        </label>
        <label>
          Second training day
          <input
            type="date"
            value={form.end_date}
            min={form.date || undefined}
            onChange={update("end_date")}
          />
        </label>
        <label>
          Display label
          <input
            value={form.label}
            onChange={update("label")}
            placeholder="Optional intake name"
          />
        </label>
        <label>
          Places available
          <input
            type="number"
            min={intake?.enrollment_count || 1}
            max="500"
            value={form.capacity}
            onChange={update("capacity")}
            required
          />
        </label>
        <label>
          Enrollment status
          <select value={form.status} onChange={update("status")}>
            <option value="open">Open for enrollment</option>
            <option value="closed">Closed</option>
            <option value="cancelled">Cancelled</option>
          </select>
        </label>
        <div className="button-row">
          <button className="primary-button" type="submit" disabled={working}>
            {intake ? "Save intake" : "Publish intake"}
          </button>
          {intake && (
            <button
              className="danger-button"
              type="button"
              disabled={working || intake.enrollment_count > 0}
              onClick={async () => {
                const result = await action(
                  `/api/staff/course-dates/${intake.id}`,
                  {},
                  "Empty course intake deleted.",
                  "DELETE",
                );
                if (result) onDone();
              }}
            >
              Delete empty intake
            </button>
          )}
        </div>
      </form>
    </section>
  );
}

function ContentEditor({ content, action, working }) {
  const [form, setForm] = useState(content);
  useEffect(() => setForm(content), [content]);
  const course = form.course || {};
  const courseNames = Object.fromEntries(
    [form.course, ...(form.additional_courses || [])]
      .filter(Boolean)
      .map((item) => [item.id, item.name]),
  );
  const setFaqCourseAnswer = (index, courseId, value) =>
    setForm((current) => ({
      ...current,
      faqs: current.faqs.map((item, itemIndex) =>
        itemIndex === index
          ? {
              ...item,
              course_answers: { ...item.course_answers, [courseId]: value },
            }
          : item,
      ),
    }));
  const setCourse = (key, value) =>
    setForm((current) => ({
      ...current,
      course: { ...current.course, [key]: value },
    }));
  const textFields = [
    ["overview", "Course overview"],
    ["minimum_qualification", "Entry requirements"],
    ["job_opportunities", "Job opportunities"],
    ["training_time", "Training times"],
    ["venue", "Training venue"],
    ["no_intakes_message", "Message when no dates are available"],
  ];
  async function save(event) {
    event.preventDefault();
    const coursePayload = Object.fromEntries(
      textFields.map(([key]) => [key, course[key] || ""]),
    );
    coursePayload.skillsfuture = {
      guidance: course.skillsfuture?.guidance || "",
    };
    coursePayload.paynow = { instructions: course.paynow?.instructions || "" };
    await action(
      "/api/staff/content",
      { course: coursePayload, faqs: form.faqs || [] },
      "Course and FAQ content saved. New assistant responses use the updated content.",
      "PATCH",
    );
  }
  return (
    <form className="content-editor" onSubmit={save}>
      <section className="workspace-card">
        <span className="eyebrow">Participant information</span>
        <h2>Course information</h2>
        <div className="stack-form">
          {textFields.map(([key, label]) => (
            <label key={key}>
              {label}
              <textarea
                rows={
                  key === "overview" || key === "minimum_qualification" ? 3 : 2
                }
                value={course[key] || ""}
                onChange={(event) => setCourse(key, event.target.value)}
                required
              />
            </label>
          ))}
          <label>
            SkillsFuture instructions
            <textarea
              rows={3}
              value={course.skillsfuture?.guidance || ""}
              onChange={(event) =>
                setCourse("skillsfuture", {
                  ...course.skillsfuture,
                  guidance: event.target.value,
                })
              }
            />
          </label>
          <label>
            PayNow instructions
            <textarea
              rows={3}
              value={course.paynow?.instructions || ""}
              onChange={(event) =>
                setCourse("paynow", {
                  ...course.paynow,
                  instructions: event.target.value,
                })
              }
              placeholder="Instructions accompanying the invoice"
            />
          </label>
        </div>
      </section>
      <section className="workspace-card">
        <div className="section-heading">
          <h2>Frequently asked questions</h2>
          <button
            className="secondary-button"
            type="button"
            onClick={() =>
              setForm((current) => ({
                ...current,
                faqs: [...(current.faqs || []), { topic: "", answer: "" }],
              }))
            }
          >
            Add question
          </button>
        </div>
        {(form.faqs || []).map((faq, index) => (
          <div className="faq-editor-row" key={index}>
            <label>
              Topic
              <input
                value={faq.topic}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    faqs: current.faqs.map((item, itemIndex) =>
                      itemIndex === index
                        ? { ...item, topic: event.target.value }
                        : item,
                    ),
                  }))
                }
                required
              />
            </label>
            <label>
              {faq.course_answers ? "Answer when no course is named" : "Answer"}
              <textarea
                value={faq.answer}
                rows={3}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    faqs: current.faqs.map((item, itemIndex) =>
                      itemIndex === index
                        ? { ...item, answer: event.target.value }
                        : item,
                    ),
                  }))
                }
                required
              />
            </label>
            {Object.entries(faq.course_answers || {}).map(([courseId, text]) => (
              <label key={courseId}>
                Answer for {courseNames[courseId] || courseId}
                <textarea
                  value={text}
                  rows={3}
                  onChange={(event) =>
                    setFaqCourseAnswer(index, courseId, event.target.value)
                  }
                  required
                />
              </label>
            ))}
            <button
              className="text-button danger-text"
              type="button"
              onClick={() =>
                setForm((current) => ({
                  ...current,
                  faqs: current.faqs.filter(
                    (_, itemIndex) => itemIndex !== index,
                  ),
                }))
              }
            >
              Remove question
            </button>
          </div>
        ))}
      </section>
      <div className="content-save-bar">
        <p className="muted small-copy">
          Updates apply to future answers and generated instructions.
        </p>
        <button className="primary-button" type="submit" disabled={working}>
          {working ? "Saving…" : "Save course & FAQ content"}
        </button>
      </div>
    </form>
  );
}

function DeliveryMonitor({ data, action, working, financeAccess }) {
  return (
    <>
      <section className="workspace-card">
        <h2>Outbound messages</h2>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Channel</th>
                <th>Purpose</th>
                <th>Status</th>
                <th>Attempts</th>
                <th>Recovery</th>
              </tr>
            </thead>
            <tbody>
              {(data.deliveries || []).map((item) => (
                <tr key={item.id}>
                  <td>{item.channel}</td>
                  <td>{readable(item.purpose)}</td>
                  <td>
                    <Badge>{item.status}</Badge>
                    <small>{item.error}</small>
                    {item.recipient_masked && <small>{item.recipient_masked}</small>}
                    {item.failure_category && <small>{readable(item.failure_category)}</small>}
                  </td>
                  <td>{item.attempts}</td>
                  <td>
                    {["failed", "disabled", "sending"].includes(item.status) &&
                      (item.email_type !== "invoice" || financeAccess) && (
                      <button
                        className="secondary-button"
                        disabled={working}
                        onClick={() =>
                          action(
                            `/api/staff/deliveries/${item.id}/retry`,
                            {},
                            "Message queued for retry.",
                          )
                        }
                      >
                        Retry after checking delivery
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!data.deliveries?.length && <Empty>No outbound messages yet.</Empty>}
      </section>
      <section className="workspace-card">
        <h2>Inbound messages</h2>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Channel</th>
                <th>Status</th>
                <th>Attempts</th>
                <th>Evidence</th>
                <th>Recovery</th>
              </tr>
            </thead>
            <tbody>
              {(data.events || []).map((item) => (
                <tr key={item.id}>
                  <td>{item.channel}</td>
                  <td>
                    <Badge>{item.status}</Badge>
                  </td>
                  <td>{item.attempts}</td>
                  <td>
                    {financeAccess && item.has_attachment && (
                      <a
                        href={`${apiBaseUrl}/api/staff/inbox/${item.id}/attachment`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Open evidence ?
                      </a>
                    )}
                  </td>
                  <td>
                    {["failed", "processing"].includes(item.status) && (
                      <button
                        className="secondary-button"
                        disabled={working}
                        onClick={() =>
                          action(
                            `/api/staff/inbox/${item.id}/retry`,
                            {},
                            "Inbound message queued for retry.",
                          )
                        }
                      >
                        Retry after checking case
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!data.events?.length && (
          <Empty>No inbound integration messages yet.</Empty>
        )}
      </section>
    </>
  );
}

function QualityPanel({ metrics, action, working }) {
  const [form, setForm] = useState({
    kind: "router",
    target_id: "",
    correct: true,
  });
  return (
    <>
      <section className="workspace-card">
        <h2>Measured quality</h2>
        <p className="muted">
          Samples are reviewed by staff. An empty sample has no measured
          accuracy.
        </p>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Sample</th>
                <th>Metric</th>
                <th>Reviewed</th>
                <th>Measured</th>
                <th>Target</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(metrics).map(([kind, value]) => (
                <tr key={kind}>
                  <td>{kind}</td>
                  <td>{readable(value.metric)}</td>
                  <td>{value.sample_count}</td>
                  <td>
                    {value.rate === null
                      ? "Not measured"
                      : `${(value.rate * 100).toFixed(1)}%`}
                  </td>
                  <td>{value.target * 100}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="workspace-card">
        <h2>Record a sample review</h2>
        <form
          className="stack-form"
          onSubmit={(event) => {
            event.preventDefault();
            action("/api/staff/quality/reviews", form, "Quality sample saved.");
          }}
        >
          <label>
            Sample type
            <select
              value={form.kind}
              onChange={(event) =>
                setForm({ ...form, kind: event.target.value })
              }
            >
              {["router", "faq", "document", "payment"].map((kind) => (
                <option key={kind}>{kind}</option>
              ))}
            </select>
          </label>
          <label>
            Message, document or payment ID
            <input
              required
              value={form.target_id}
              onChange={(event) =>
                setForm({ ...form, target_id: event.target.value })
              }
            />
          </label>
          <label>
            Review outcome
            <select
              value={String(form.correct)}
              onChange={(event) =>
                setForm({ ...form, correct: event.target.value === "true" })
              }
            >
              <option value="true">Correct</option>
              <option value="false">Incorrect</option>
            </select>
          </label>
          <button className="primary-button" disabled={working}>
            Save review
          </button>
        </form>
      </section>
    </>
  );
}

function SkillsFutureConfirmation({ enrollment, action, working }) {
  const [reference, setReference] = useState("");
  const [transactionDate, setTransactionDate] = useState("");
  const [note, setNote] = useState("");
  return (
    <form
      className="stack-form panel-section"
      onSubmit={(event) => {
        event.preventDefault();
        action(
          `/api/staff/enrollments/${enrollment.id}/skillsfuture/confirm`,
          { reference, transaction_date: transactionDate, note },
          "SkillsFuture component confirmed against the accounts record.",
        );
      }}
    >
      <h3>Reconcile SkillsFuture payment</h3>
      <p className="muted small-copy">
        Confirm S${Number(enrollment.invoice.skillsfuture_amount).toFixed(2)}{" "}
        only after checking the actual claim payment.
      </p>
      <label>
        Verified claim reference
        <input
          value={reference}
          onChange={(event) => setReference(event.target.value)}
          required
        />
      </label>
      <label>
        Claim payment date
        <input
          type="date"
          max={new Date().toISOString().slice(0, 10)}
          value={transactionDate}
          onChange={(event) => setTransactionDate(event.target.value)}
          required
        />
      </label>
      <label>
        Accounts reconciliation note
        <textarea
          value={note}
          onChange={(event) => setNote(event.target.value)}
          rows={2}
          required
        />
      </label>
      <button className="primary-button" disabled={working}>
        Confirm reconciled claim
      </button>
    </form>
  );
}
