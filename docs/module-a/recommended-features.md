# Module A feature additions

The six features in the recommended implementation sequence are implemented. Voice-note transcription, translated replies and office-hours estimates remain optional future work.

## Participant experience

- Click-to-WhatsApp referral data records the first ad ID, headline and click ID. Later direct messages retain attribution. Ad copy provides an untrusted first-contact topic hint; approved KB content still controls factual answers.
- Replies carry native WhatsApp buttons for up to three short labels, otherwise a list with up to ten choices. IDs preserve full labels when list titles are shortened. Answers longer than the interactive body limit retain their full text and queue the menu separately. Replies outside the customer-service window continue to require an approved template.
- Send **Calculate funding** to see costs for the course selected in this chat, or choose a course first when none is selected. Both courses use the same questions for basic-tier SkillsFuture Credit, NTUC membership and age band, and the same final response format. Front Desk Operations adds the 10% Q&M-funded discount (S$45), applied before funding: S$100 of SkillsFuture Credit leaves S$305 payable upfront; UTAP with no SkillsFuture Credit gives a possible S$202.50 reimbursement on the S$405 discounted fee. The discount ends on 15 November 2026 at 11:59 pm Singapore time; from 16 November funding is calculated against S$450. Previously recorded fees are preserved. Mid-Career credits and combined UTAP/SkillsFuture use are rejected. Enter **cancel** to leave. STOP and other global policy signals interrupt the calculator. No payment allocation is changed automatically.

Funding replies show the UTAP eligibility and claim-approval qualification only when UTAP is selected. SkillsFuture replies omit the zero-value reimbursement line. The Front Desk discount is described as a two-week enrolment promotion to make the course more affordable for new participants. Customer copy shows the expiry date and time without a timezone label; expiry calculations continue to use Singapore time.

## Staff workflow

For an unanswered-question escalation, write the participant-facing answer and select its course scope, then resolve the case. The existing case-reply endpoint queues delivery and creates a pending knowledge draft in one transaction. Web-only cases without a delivery contact must have a contact recorded before this workflow can send an answer.

Under **Course & FAQ content → Pending KB entries**, review the proposed text and approve it. Only approval through `PATCH /api/staff/content` adds it to the runtime KB, model context and fact allow-list. Approved entries persist in the database across restarts; the source JSON is unchanged. Pending drafts are removed when their source case expires under retention.

The overview includes containment and escalation rates, escalation categories, cited KB topics, unanswered subjects, median first-response time, follow-up re-engagement and source/ad enrollment and payment funnels. The Leads view displays source, ad headline and rule-based Hot/Warm/Cold priority, sorted by score. Priority reasons are available on the badge.

## Metric definitions

Metrics cover all retained records. Conversation rates use conversations containing participant messages. Containment requires at least one recorded grounded answer and no escalation or staff reply. Escalation rates count conversations with any historical escalation, including resolved cases; category rates count unique conversations within each category. First-response time measures the first persisted assistant message after the first participant message, not provider delivery latency.

Topic counts use message citations recorded after this change. Follow-up metrics count unique leads per latest preceding sent step; this measures temporal association, not causation. Enrollment requires a confirmed, noncancelled registration; paid requires recorded payments covering a positive invoice fee. Each source/ad bucket counts leads, not individual selected courses.

Priority adds one point each for dates, fees/payment, a provided registration name, an opened registration and replying after a sent follow-up. Three or more is Hot, one or two Warm, zero Cold. This is a staff sorting aid rather than an enrollment prediction.

## Verification

Run `python -m pytest backend/tests` and `npm test` / `npm run build` in `frontend`. `backend/tests/enquiry_tests/test_recommended_features.py` covers calculator limits, menu round-trips and service windows, referral ingestion, approval gating, analytics, priority and repeatable migrations. Funding conversations are included in the existing evaluation dataset.

The authenticated development inbound endpoint accepts the same `referral` and `interactive` fields as WhatsApp messages. Integration tests exercise that endpoint and the inbox worker without contacting Meta or sending messages.

Validation on 31 October 2026: the full backend run passed 886 tests with 2 deselected. The final feature-specific run passed 26 tests, and the frontend passed all 33 tests plus its production build. Synthetic HTTP smoke checks on ports 5002 and 5175 verified inbound acceptance, duplicate detection, inbox processing and frontend serving. Both temporary servers were stopped afterward.
