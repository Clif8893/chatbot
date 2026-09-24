# Requirements Document

<!-- Navigation Metadata -->
<!-- Template: Requirements | Level: Completed | Baseline author: Clifton Chen Yi -->

## Document Information

- **Feature Name**: AI-Powered Enquiry & Enrollment System
- **Version**: 1.1
- **Date**: 15 September 2026
- **Baseline Author**: Clifton Chen Yi
- **Review Audience**: Mr. Wee and Q&M stakeholders
- **Stakeholders**: Q&M operations staff, accounting staff, course administrators, prospective participants, enrolled participants, and the project team

## Introduction

Q&M's Basic Certificate in Dental Assisting enquiry and enrollment process currently requires staff to respond to leads, answer questions, collect participant details, prepare invoices, check payment evidence, send confirmations, and update the course schedule manually.

The system will support this workflow from a Facebook advertisement enquiry through enrollment and payment follow-up. It will communicate naturally with participants, preserve retrievable records, and give staff visibility and control where confirmation or intervention is required.

### Scope

**Included**

- Enquiries originating from Facebook advertisements.
- Participant communication through WhatsApp and email.
- Course, funding, payment, eligibility, UTAP, and job-opportunity replies.
- Lead follow-up, enrollment-data collection, course-date confirmation, invoicing, payment-evidence handling, payment confirmation, and course-schedule updates.
- Staff management of course dates and participant numbers.

**Excluded**

- Accessing a participant's SkillsFuture account or submitting a claim on the participant's behalf.
- Guaranteeing employment after course completion.
- Treating example or expired intake dates as permanent course dates.

## Source Register

| Priority | Source | Use |
|---|---|---|
| 1 | Company workflow presentation | Primary source for the enquiry-to-course-schedule workflow. |
| 2 | Forwarded company email dated 5 August 2026 | Confirmed clarifications and updated operating rules. |
| 3 | Two-day course reply template | Approved course replies, enrollment information, payment methods, and FAQs. |
| Baseline | Clifton's version 1.0 requirements | Existing scope retained where it does not conflict with the company information above. |

## Confirmed Course Information

| Topic | Confirmed information |
|---|---|
| Course | Basic Certificate in Dental Assisting |
| Fee | S$600 nett for the full course |
| Duration | Two consecutive training days |
| Training time | 9:30am to 5:30pm on each day |
| SkillsFuture | Basic-tier SkillsFuture Credits may be used. Mid-Career SkillsFuture Credits are not accepted for this course. |
| PayNow | PayNow is accepted using the current approved company payment details. The supplied UEN is 201841969G. |
| Minimum qualification | GCE N Levels are preferred. Applicants without GCE N Levels may be considered if they have basic English reading and writing skills because the assessment is conducted in English. |
| Job opportunities | Suitable candidates may be considered for opportunities after the course, subject to the normal selection and interview process. No job is guaranteed. |
| Intake dates | Staff-maintained dates shall be used. The July and August 2026 dates in the reply template are historical examples only. |
| Course location | Not yet confirmed because the supplied sources show different locations; see Open Questions. |

### UTAP Guidance

The participant reply shall explain that eligible NTUC Union members may claim 50% of the unfunded course fee, capped at S$250 per year or S$500 per year for members aged 40 and above. Participants must pay first, attend both training days, and submit their claim within six months after course completion with proof of payment and completion. UTAP and SkillsFuture Credits cannot be used together for the same course fee. The participant remains responsible for checking current eligibility and submitting the claim.

## Requirements

### Requirement 1: Receive and Introduce Facebook Leads (REQ-01)

**User Story:** As a prospective participant, I want a prompt introduction after enquiring through a Facebook advertisement, so that I know how Q&M can assist me.

#### Acceptance Criteria

1. WHEN the system receives an enquiry originating from a Facebook advertisement THEN the system SHALL create a lead record and associate it with the participant's conversation.
2. WHEN a new lead is received THEN the system SHALL send an introductory response that identifies the course-enquiry service and invites questions or enrollment interest.
3. IF the lead cannot be contacted automatically THEN the system SHALL make the failed contact visible to staff.

**Priority:** High  
**Source:** Workflow presentation, Step 1

### Requirement 2: Answer Participant Questions Naturally (REQ-02)

**User Story:** As a prospective participant, I want to ask questions in ordinary language, so that I can understand the course before enrolling.

#### Acceptance Criteria

1. WHEN a participant asks a course question through WhatsApp or email THEN the system SHALL respond naturally using approved course information.
2. WHEN asked about fees, duration, training time, SkillsFuture, PayNow, UTAP, minimum qualifications, or job opportunities THEN the system SHALL provide the confirmed information in this document without overstating funding eligibility or employment prospects.
3. WHEN asked about SkillsFuture balance or claim submission THEN the system SHALL direct the participant to the approved SkillsFuture claim page and SHALL NOT access or submit the participant's claim.
4. IF requested information is not approved, is outdated, or conflicts with current course records THEN the system SHALL refer the question to staff rather than invent an answer.

**Priority:** High  
**Source:** Workflow presentation, Steps 2a–2b; course reply template

### Requirement 3: Follow Up Missing Leads (REQ-03)

**User Story:** As Q&M operations staff, I want interested but unresponsive leads followed up consistently, so that genuine enrollment opportunities are not missed.

#### Acceptance Criteria

1. IF a participant does not reply after the initial enquiry response THEN the system SHALL make three follow-up attempts at two-day intervals.
2. WHEN the participant responds, declines, enrolls, or is marked for staff handling THEN the system SHALL stop any remaining automatic lead follow-ups.
3. WHEN a follow-up attempt is made THEN the system SHALL record its sequence number and time.
4. IF all three attempts receive no response THEN the system SHALL mark the lead as requiring no further automatic follow-up while keeping the record available to staff.

**Priority:** High  
**Source:** Email clarification dated 5 August 2026; timing conflict recorded under Open Questions

### Requirement 4: Collect and Confirm Enrollment Details (REQ-04)

**User Story:** As a participant, I want to provide registration details conversationally and confirm my course date, so that Q&M can enroll me accurately.

#### Acceptance Criteria

1. WHEN a participant chooses to enroll THEN the system SHALL collect:
   - full name as shown on NRIC;
   - NRIC number;
   - date of birth;
   - email address;
   - mobile number;
   - selected course date;
   - intended payment method; and
   - intended SkillsFuture and PayNow amounts, where applicable.
2. IF required information is missing THEN the system SHALL request only the missing information and preserve details already supplied.
3. BEFORE invoicing THEN the system SHALL show or send the selected course date and payment allocation for participant confirmation.
4. IF a selected date is no longer available THEN the system SHALL request another staff-approved course date and SHALL NOT confirm the unavailable date.
5. WHEN the participant confirms the details THEN the system SHALL record the enrollment and confirmed course date.

**Priority:** High  
**Source:** Workflow presentation, Steps 3–4; course reply template

### Requirement 5: Record Payment Allocation and Issue the Invoice (REQ-05)

**User Story:** As an enrolled participant, I want an accurate invoice and claim instructions, so that I can complete payment correctly.

#### Acceptance Criteria

1. WHEN confirmed enrollment details are ready THEN the system SHALL record the amount assigned to SkillsFuture Credits and the amount assigned to PayNow or bank transfer.
2. WHEN an invoice is generated THEN it SHALL include the approved invoice number, invoice date, participant details, course and confirmed date, S$600 nett total fee, payment allocation, and current approved company payment details.
3. WHEN the invoice is ready THEN the system SHALL preserve a retrievable copy of it.
4. WHEN the invoice is issued THEN the system SHALL email it to the participant with the approved SkillsFuture claim and payment instructions.
5. IF invoice delivery fails THEN the system SHALL retain the correct delivery status and alert staff for follow-up.

**Priority:** High  
**Source:** Workflow presentation, Steps 4–5; course reply template

### Requirement 6: Receive and Store Payment Evidence (REQ-06)

**User Story:** As a participant, I want to submit payment evidence through an available channel, so that Q&M can confirm my payment.

#### Acceptance Criteria

1. WHEN payment evidence is requested THEN the system SHALL tell the participant that email is the preferred submission channel.
2. WHEN a participant submits a PayNow or SkillsFuture screenshot by email or WhatsApp THEN the system SHALL associate it with the correct participant, enrollment, invoice, and payment type.
3. WHEN evidence is received THEN the system SHALL preserve a retrievable copy and record the submission channel and time.
4. IF evidence is missing, unreadable, mismatched, or cannot be associated confidently THEN the system SHALL refer the case to staff and SHALL NOT record the payment as confirmed automatically.

**Priority:** High  
**Source:** Workflow presentation, Steps 6–7; email clarification dated 5 August 2026

### Requirement 7: Retain Retrievable Communications and Records (REQ-07)

**User Story:** As an authorised staff member, I want to retrieve earlier participant interactions and documents, so that I can continue or review a case with its full context.

#### Acceptance Criteria

1. WHILE a participant case is retained THEN the system SHALL preserve its WhatsApp conversation history in a form that authorised staff can retrieve.
2. WHEN staff review a participant case THEN the system SHALL make its enrollment details, invoices, payment evidence, follow-up history, and relevant email communications retrievable together.
3. WHEN new messages or documents are received THEN the system SHALL associate them with the existing participant case where the association is known.

**Priority:** High  
**Source:** Email clarification dated 5 August 2026; workflow presentation, Steps 7–8

### Requirement 8: Confirm Payment and Update the Course Schedule (REQ-08)

**User Story:** As a confirmed participant, I want payment confirmation and an accurate course booking, so that I know when and where to attend.

#### Acceptance Criteria

1. WHEN the required payment amounts are confirmed THEN the system SHALL update the participant's payment status and send a payment confirmation by email.
2. WHEN sending payment confirmation THEN the system SHALL include the approved course name, confirmed date, training time, current venue, and any approved attendance instructions.
3. WHEN payment is confirmed THEN the system SHALL add or update the participant on the selected course schedule.
4. WHEN a receipt is approved for issue THEN the system SHALL send it to the participant and preserve a retrievable copy.
5. IF payment confirmation or receipt delivery fails THEN the system SHALL show the failure to staff and SHALL NOT record the item as successfully delivered.

**Priority:** High  
**Source:** Workflow presentation, Steps 8–9; forwarded email chain

### Requirement 9: Chase Outstanding Balances (REQ-09)

**User Story:** As Q&M operations staff, I want outstanding balances followed up before training, so that payment issues can be resolved before the course begins.

#### Acceptance Criteria

1. IF an enrolled participant has an outstanding balance seven days before the confirmed course date THEN the system SHALL send the approved balance reminder.
2. WHEN a balance reminder is sent THEN the system SHALL record the outstanding amount and reminder time.
3. WHEN the outstanding balance is confirmed as paid THEN the system SHALL stop further balance reminders for that enrollment.
4. IF the balance remains unresolved or disputed THEN the system SHALL make the case visible to staff for a decision.

**Priority:** High  
**Source:** Email clarification dated 5 August 2026

### Requirement 10: Manage Course Dates and Participant Numbers (REQ-10)

**User Story:** As a course administrator, I want to maintain course dates and participant numbers, so that enrollment replies and schedules use current availability.

#### Acceptance Criteria

1. WHEN authorised staff add, change, close, or remove a course date THEN the system SHALL use the updated availability for subsequent participant replies and enrollment confirmations.
2. WHEN staff view a course date THEN the system SHALL show the number of participants assigned to it.
3. WHEN a confirmed participant is added, moved, or removed THEN the system SHALL update the participant count for the affected course dates.
4. IF a date is closed or unavailable THEN the system SHALL prevent new automatic confirmations for that date.

**Priority:** High  
**Source:** Email clarification dated 5 August 2026; workflow presentation, Step 9

### Requirement 11: Staff Review and Exceptions (REQ-11)

**User Story:** As Q&M staff, I want uncertain cases presented with their relevant context, so that I can make the required business decision.

#### Acceptance Criteria

1. IF a participant question cannot be answered from approved information THEN the system SHALL refer it to staff.
2. IF enrollment details, payment allocation, or payment evidence are inconsistent THEN the system SHALL hold the affected action for staff review.
3. WHEN a case is referred THEN the system SHALL show the participant, conversation, enrollment, invoice, and evidence context available for that case.
4. WHEN staff record a decision THEN the system SHALL continue the workflow from that decision without losing the existing case history.

**Priority:** High  
**Source:** Clifton's baseline requirements; forwarded email chain

## Constraints and Assumptions

### Business Constraints

- Participant-facing course dates, venues, funding rules, payment details, and reply wording must come from current staff-approved information.
- Email is the preferred channel for payment screenshots, but WhatsApp submissions must also be accepted.
- The S$600 fee and funding guidance apply to the Basic Certificate in Dental Assisting described by the supplied course reply template.

### Assumptions

- Staff will maintain current course dates, venue details, participant limits, and approved communication wording.
- Participants are responsible for checking their available SkillsFuture balance and completing their own SkillsFuture or UTAP claims.
- Payment or funding cases that cannot be confirmed from the available evidence require staff review.

## Success Criteria

- [ ] A Facebook advertisement lead can progress from introductory response to confirmed course-schedule entry.
- [ ] Participants can ask natural-language questions and receive the approved course, funding, payment, qualification, UTAP, and job-opportunity information.
- [ ] Missing leads receive no more than three follow-ups at the agreed interval.
- [ ] Enrollment details, selected date, SkillsFuture amount, and PayNow amount are confirmed before invoicing.
- [ ] Invoices, communications, payment evidence, and course-schedule records remain retrievable by authorised staff.
- [ ] Payment evidence is accepted through email and WhatsApp, with email identified as preferred.
- [ ] Outstanding balances trigger a reminder seven days before the course.
- [ ] Staff can maintain course dates and see participant numbers.
- [ ] Q&M stakeholders confirm the open questions below.

## Open Questions

1. **Lead follow-up timing:** The workflow presentation specifies attempts after 30 minutes, one day, and two days. The later 5 August 2026 email specifies three attempts at two-day intervals. REQ-03 uses the later email rule, but Q&M should confirm that it supersedes the presentation timing.
2. **Course venue:** The reply template states "HQ, Clementi Loop"; workflow examples show the Q&M Dental Centre auditorium at 180 Kitchener Road; and the email signature identifies the head office at 2 Jurong East Street 21, IMM Building. Which venue should be sent for each future intake?
3. **Future intake dates:** The July and August 2026 dates in the reply template and dates shown in workflow screenshots are historical. Who approves and maintains the current dates, booking limits, and closed/full status?
4. **Payment confirmation rule:** What evidence or staff approval is required before PayNow and SkillsFuture amounts are treated as confirmed, especially while a SkillsFuture claim is pending?
5. **Receipt timing:** Should the receipt be issued immediately after full payment confirmation or only after course completion for some payment or UTAP cases?
6. **Record retention:** How long must WhatsApp conversations, emails, invoices, enrollment records, and payment screenshots remain retrievable, and which staff roles may access them?
7. **Outstanding-balance escalation:** What action should follow if a balance remains unpaid after the seven-day reminder?

## Glossary

| Term | Definition |
|---|---|
| SkillsFuture basic-tier credits | The SkillsFuture credit tier accepted for this course. |
| Mid-Career SkillsFuture Credits | A separate credit tier that the supplied course reply says is not accepted for this course. |
| PayNow | An accepted payment method using Q&M's approved company payment details. |
| UTAP | Union Training Assistance Programme reimbursement available to eligible NTUC Union members after payment and course completion. |
| Payment evidence | A PayNow or SkillsFuture screenshot or other approved record submitted to support payment confirmation. |
| Missing lead | A prospective participant who has not replied after the initial enquiry response. |

## Requirements Review Checklist

- [x] Requirements are numbered and testable.
- [x] Acceptance criteria describe required behaviour rather than implementation.
- [x] The company workflow, email clarifications, and course reply information are traceable.
- [x] Historical intake dates are not treated as future requirements.
- [x] Conflicting follow-up timing and location information are recorded as open questions.
- [x] Unconfirmed architecture, security, and performance requirements are excluded.
- [ ] Q&M stakeholders have reviewed and resolved the Open Questions.
