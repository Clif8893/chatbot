import React from "react";

export function EnquiryInsights({ analytics }) {
  if (!analytics) return null;
  const groups = [
    ["Escalations by category", analytics.escalations_by_category, analytics.escalation_rates_by_category],
    ["Top KB topics", analytics.top_kb_topics],
    ["Top unanswered subjects", analytics.top_unanswered_subjects],
    ["Leads replying after follow-up step", analytics.followup_reengagement],
  ];
  return <section className="workspace-card">
    <h2>Enquiry analytics</h2>
    <p>{analytics.conversations} conversations · Containment {(analytics.containment_rate * 100).toFixed(1)}% · Escalation {(analytics.escalation_rate * 100).toFixed(1)}%</p>
    <p>Median first response: {analytics.median_first_response_seconds == null ? "No responses yet" : `${analytics.median_first_response_seconds.toFixed(1)} seconds`}</p>
    {groups.map(([title, values, rates]) => <div key={title}><h3>{title}</h3>
      {Object.keys(values || {}).length ? <ul>{Object.entries(values).map(([key, count]) => <li key={key}>{key.replaceAll("_", " ")}: {count}{rates && ` (${((rates[key] || 0) * 100).toFixed(1)}%)`}</li>)}</ul> : <p>No observations yet.</p>}
    </div>)}
    <h3>Enquiry → enrollment → paid</h3>
    <table><thead><tr><th>Source / ad</th><th>Enquiries</th><th>Enrolled</th><th>Paid</th></tr></thead>
      <tbody>{(analytics.funnel || []).map((row) => <tr key={`${row.source}:${row.ad_id}`}><td>{row.source} {row.ad_id}</td><td>{row.enquiry}</td><td>{row.enrolled}</td><td>{row.paid}</td></tr>)}</tbody></table>
    <p className="muted small-copy">{analytics.definitions}</p>
  </section>;
}

export function PendingKnowledge({ drafts = [], action, working }) {
  return <section className="workspace-card"><h2>Pending KB entries</h2>
    {!drafts.length && <p>No answers awaiting approval.</p>}
    {drafts.map((draft) => <article key={draft.id}><h3>{draft.topic}</h3><p>{draft.answer}</p><p>Course: {draft.course_id}</p>
      <button className="primary-button" disabled={working} onClick={() => action("/api/staff/content", { approve_draft_id: draft.id }, "Answer approved for future enquiries.", "PATCH")}>Approve answer</button>
    </article>)}
  </section>;
}
