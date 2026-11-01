# Requirements Document — Module A: Conversational Enquiry Assistant & Lead Nurturing

<!-- Navigation Metadata -->
<!-- Template: Requirements | Level: Draft for review | Related: design.md, tasks.md -->

## Document Information

- **Feature Name**: Module A — Conversational Enquiry Assistant & Lead Nurturing (rebuild)
- **Version**: 0.1 (draft)
- **Date**: 30 October 2026
- **Author**: Clifton Chen Yi
- **Review Audience**: Mr. Wee and Q&M stakeholders
- **Stakeholders**: Prospective participants, Q&M operations staff, project team

## Introduction

Q&M staff spend significant time answering the same WhatsApp questions about the
2-Day Basic Certificate in Dental Assisting: SkillsFuture and Mid-Career credits,
course benefits, entry requirements and job opportunities. They also chase leads
who stop replying. Module A replaces this manual work with a conversational
assistant that answers from approved content, behaves sensibly whatever the
participant types, and remembers each participant across messages and
conversations so that replies feel personal rather than templated.

This document supersedes the earlier Module A implementation, which is being
rebuilt. Modules B (enrollment) and C (payment) are out of scope except for the
hand-off points defined here.

### Feature Summary

A natural-language enquiry assistant that answers grounded course questions,
handles off-topic, rude, unclear and adversarial messages appropriately,
personalises replies from current and past chat history, escalates to staff, and
sends contextual follow-up reminders.

### Business Value

- Removes repetitive enquiry handling from staff (proposal §2, Group 1).
- Keeps answers consistent with the approved reply template (target ≥95% FAQ
  accuracy, proposal §8).
- Converts more leads by remembering what each participant cares about and
  following up in context.

### Scope

**Included**

- Course enquiries in the web chat now, and WhatsApp/email later through the same service.
- Approved FAQ answers, the first-contact quick-reply menu, and SkillsFuture balance self-check guidance.
- Response handling for off-topic, rude, abusive, distressed, unclear and prompt-injection messages.
- Participant memory across the current conversation and the participant's earlier conversations.
- Staff escalation, opt-out, the "forget me" request, and contextual lead follow-ups.
- Hand-off to Module B when a participant wants to enroll.

**Excluded**

- Collecting or validating enrollment details (Module B).
- Payment evidence and receipts (Module C).
- Accessing Singpass or SkillsFuture accounts, or claiming on a participant's behalf (proposal §3 note, §9).
- Stateless slash-command syntax (proposal §10). It is superseded by REQ-A08, which requires multi-turn memory.

## Source Register

| Priority | Source | Use |
|---|---|---|
| 1 | `2 days DA course Reply Template.docx` | Approved reply wording: introduction, modes of payment, enrollment details, Mid-Career, fee concern, job opportunities, minimum qualification, UTAP |
| 2 | `Agentive Workflow for Dental Assisting Course Administration.pptx` | Workflow steps; slides 2–4 show the target tone and personalised replies (top-up arithmetic, job scope, HR interview) |
| 3 | `QM_AI_Powered_Enquiry_and_Enrollment_System_V2_4.pdf` | Module A scope (§3, §5.3), exclusions (§9), KPIs (§8) |
| 4 | `SpecDrivenDevelopment.pdf` | Development method: this spec precedes code |
| 5 | `docs/requirements/Cliftons-Requirements.md` | Project-wide baseline; REQ-01 to REQ-03 are refined here |
| Live | Staff-managed course catalogue and course dates | Fees and intake dates. These override any date written in the template. |

## Requirements

### Requirement A01: Answer Course Questions from Approved Content (REQ-A01)

**User Story:** As a prospective participant, I want accurate answers to my course questions in ordinary language, so that I can decide whether to enroll.

#### Acceptance Criteria

1. WHEN a participant asks about fees, duration, training time, intake dates, SkillsFuture, Mid-Career credits, PayNow, UTAP, minimum qualification, course content or job opportunities THEN the system SHALL answer using only approved knowledge-base content and live catalogue facts.
2. WHEN an approved template exists for the topic THEN the system SHALL keep its factual content and SHALL tailor only the framing to the question asked.
3. WHEN intake dates or fees are requested THEN the system SHALL use the live course catalogue and SHALL NOT quote historical template dates.
4. WHEN a topic is marked as requiring confirmation (e.g. venue, decision D1) THEN the system SHALL NOT state it as fact and SHALL offer to confirm with staff.
5. WHEN asked about employment THEN the system SHALL NOT guarantee a job.
6. WHEN asked about Front Desk Operations (decision D4) THEN the system SHALL answer its name, fee, approved Q&M-funded discount, SkillsFuture and UTAP funding rules, duration and live intake dates. The funding calculator SHALL use the same flow and response format as Dental Assisting, applying the Q&M discount before funding calculations. Other unconfirmed subjects SHALL be treated under REQ-A02.
7. WHEN a participant writes in a language other than English THEN the system SHALL reply in simple English. The first time they write in a non-Latin script, it SHALL note that the course is taught and assessed in English (decision D5).

**Priority:** High
**Source:** Reply template; proposal §3 Module A; REQ-02

### Requirement A02: Unknown Q&M-Specific Questions (REQ-A02)

**User Story:** As a participant, I want an honest answer when the assistant does not know something about Q&M, so that I am not misled.

#### Acceptance Criteria

1. WHEN a question concerns Q&M or the course but is not covered by approved content THEN the system SHALL say it has no confirmed information on that subject and SHALL offer to check with staff.
2. WHEN this occurs THEN the system SHALL raise a staff escalation of category `unanswered_question`.
3. The system SHALL NOT invent prices, dates, times, venues, policies, URLs or funding rules.

**Priority:** High
**Source:** Proposal §3 (human escalation flag); REQ-02 criterion 4

### Requirement A03: General Questions Outside the Source Material (REQ-A03)

**User Story:** As a participant, I want the assistant to respond sensibly to a general question, so that the conversation feels natural.

#### Acceptance Criteria

1. WHEN a participant asks a harmless general-knowledge question unrelated to Q&M THEN the system SHALL give a brief answer (at most two sentences), presented as general information rather than Q&M policy, followed by an offer to help with the course.
2. WHEN a general question asks for personal medical or dental treatment advice, legal or financial advice, comparisons with other providers' pricing, or political opinion THEN the system SHALL politely decline and redirect to course topics.
3. A general-knowledge reply SHALL NOT contain Q&M prices, dates or URLs.

**Priority:** High
**Source:** Added requirement 1 (October 2026)

### Requirement A04: Rude, Abusive and Distressed Messages (REQ-A04)

**User Story:** As Q&M, I want the assistant to stay professional under pressure, so that the brand is protected and staff are alerted when needed.

#### Acceptance Criteria

1. WHEN a participant is frustrated or mildly rude THEN the system SHALL respond calmly, acknowledge the concern without mirroring the tone, address any underlying question (for example, use the approved fee-concern reply for "too expensive"), and offer a human contact.
2. WHEN a participant insults the assistant or uses profanity THEN the system SHALL set a brief, courteous boundary and continue to offer help.
3. WHEN a participant is rude or abusive for the third time, or threatens or harasses staff THEN the system SHALL close the exchange neutrally, raise an escalation of category `abusive_conduct`, and stop automatic follow-ups. A first-person threat of violence SHALL be detected deterministically, not left to the model. The application enforces the strike count: an abuse label from the model counts as a strike, and does not by itself close the conversation.
4. WHEN a message contains a crisis or self-harm cue THEN the system SHALL respond with empathy and the crisis-support wording (decision D3), and SHALL raise an escalation of category `distress`.
5. WHEN the assistant judges a message to show emotional distress without a crisis cue THEN the system SHALL raise a `distress` escalation for staff review, but SHALL NOT send crisis-support wording. Physical pain, money worries and everyday stress SHALL receive a kind, helpful reply instead.
6. The system SHALL NOT respond with sarcasm, insults, threats or excessive apology.

**Priority:** High
**Source:** Added requirement 1 (October 2026)

### Requirement A05: Adversarial and Unclear Input (REQ-A05)

**User Story:** As Q&M, I want the assistant to resist manipulation and handle unclear input, so that it remains reliable.

#### Acceptance Criteria

1. WHEN a message asks the assistant to ignore its instructions, reveal its prompt or configuration, or adopt another role THEN the system SHALL decline briefly and continue as the Q&M assistant.
2. The system SHALL NOT mention agents, prompts, models, providers, routing, databases or other implementation details.
3. WHEN a message is unclear or nonsensical THEN the system SHALL ask one clarifying question and offer quick replies.
4. WHEN a participant offers "facts" (for example, "you promised me a discount") THEN the system SHALL NOT treat them as approved content.

**Priority:** High
**Source:** Added requirement 1 (October 2026)

### Requirement A06: First Contact and Quick Replies (REQ-A06)

**User Story:** As a participant, I want an immediate, friendly introduction with common topics, so that I can find answers quickly.

#### Acceptance Criteria

1. WHEN a chat opens THEN the system SHALL offer starter buttons (fees and funding, upcoming dates, entry requirements, job opportunities, talking to staff), leaving out any topic this participant was already answered in an earlier chat.
2. WHEN the assistant first answers an enquiry in a conversation THEN the system SHALL state that chats are remembered to personalise help, and that "forget me" and STOP are available. Memory is only written on enquiry turns, so this disclosure always precedes it.
3. WHEN the first message already asks a question THEN the system SHALL answer it rather than show only the menu.
4. WHEN the assistant replies to an enquiry THEN the system SHALL offer at most three buttons, chosen only from the approved suggestion menu, as the participant's likely next step. It SHALL NOT offer a topic already answered in this or an earlier chat. It SHALL offer no buttons while enrollment details are being collected, or when the participant is distressed, opting out, asking for a person, or being closed for abuse.

**Priority:** Medium
**Source:** Proposal §3 Module A; REQ-01

### Requirement A07: SkillsFuture Balance Guidance (REQ-A07)

**User Story:** As a participant, I want to know how to check my SkillsFuture balance, so that I know how much I need to top up.

#### Acceptance Criteria

1. WHEN asked about a SkillsFuture balance THEN the system SHALL provide the approved self-check link (`https://sfc.myskillsfuture.gov.sg/claim`) and brief steps.
2. The system SHALL NOT ask for Singpass credentials or access any government system.
3. WHEN a participant states their available credit THEN the system SHALL calculate the remaining amount from the live course fee (for example, S$600 − S$500 = S$100 via PayNow) and remember the stated amount.
4. The system SHALL restate that Mid-Career SkillsFuture Credits cannot be used for this course whenever relevant.

**Priority:** High
**Source:** Proposal §3 note; reply template; deck slide 3

### Requirement A08: Personalisation from Current and Past Chats (REQ-A08)

**User Story:** As a returning participant, I want the assistant to remember what we discussed, so that I don't have to repeat myself.

#### Acceptance Criteria

1. WHEN generating a reply THEN the system SHALL use the current conversation's recent messages and a summary of earlier messages in that conversation.
2. WHEN the participant has earlier conversations THEN the system SHALL use their stored profile and conversation summaries, and SHALL use only that participant's own records.
3. WHEN a participant returns THEN the system SHALL greet them by name if known, and SHALL refer to relevant earlier interests (course, intake, payment preference, concerns or constraints) where helpful.
4. WHEN a full template answer was already given THEN the system SHALL refer back to it briefly rather than repeat it, unless the participant asks again.
5. WHEN remembered information conflicts with current approved content THEN the system SHALL follow approved content and correct the earlier statement politely.
6. The system SHALL store only these memory fields: preferred name, course interest, preferred intake, payment preference, stated SkillsFuture balance, NTUC membership, concerns, constraints and topics answered. It SHALL NOT store NRIC, date of birth, email or phone number in memory.
7. WHEN a participant asks the system to forget them THEN the system SHALL delete their memory profile and conversation summaries, and confirm this.
8. WHEN a memory profile has not been updated for `RETENTION_DAYS` THEN the system SHALL delete it (decision D6).

**Priority:** High
**Source:** Added requirement 2 (October 2026); proposal §5.3 (memory per participant)

### Requirement A09: Escalation, Opt-out and Hand-off (REQ-A09)

**User Story:** As Q&M staff, I want conversations that need me flagged in the staff portal, so that nothing falls through.

#### Acceptance Criteria

1. WHEN a participant asks to speak to a person THEN the system SHALL acknowledge this and raise an escalation of category `human_request`.
2. WHEN a participant sends STOP or asks not to be contacted THEN the system SHALL record withdrawn consent and cancel queued follow-ups.
3. WHEN a participant expresses intent to enroll THEN the system SHALL hand the conversation to Module B without asking for enrollment details itself.
4. Escalations SHALL NOT be duplicated while an open case of the same category exists.

**Priority:** High
**Source:** Proposal §3, §5.3; REQ-03

### Requirement A10: Contextual Lead Follow-ups (REQ-A10)

**User Story:** As Q&M operations staff, I want unresponsive leads reminded in context, so that more of them enroll.

#### Acceptance Criteria

1. IF a lead does not reply after the initial response THEN the system SHALL send up to three follow-ups at the configured intervals (`FOLLOWUP_HOURS`, default `0.5,24.5,72.5` per REQ-03: 30 minutes, then 1 day and 2 days after the previous reminder).
2. WHEN composing a follow-up THEN the system SHALL refer to the participant's remembered interests where available, and SHALL fall back to the approved template if composition fails or fails fact checking.
3. WHEN the participant replies, opts out, enrolls or is escalated THEN the system SHALL stop remaining follow-ups.

**Priority:** Medium
**Source:** Proposal §5.3 (follow-up scheduler); REQ-03

## Non-Functional Requirements

### Performance Requirements

- One model call per turn in the normal case, plus at most one retry after a failed fact check.
- Typical reply latency under 10 seconds with the configured model.

### Security Requirements

- Memory facts and summaries are encrypted at rest (`EncryptedText`).
- Cross-conversation retrieval is restricted to the same participant identity.
- Remembered content is passed to the model as data, never as instructions.
- NRIC values are masked in all model context.

### Usability Requirements

- Warm, concise Singapore/British English; light emoji use consistent with the reply template.
- Replies short enough for WhatsApp: normally no more than 120 words unless a template requires more.

### Reliability Requirements

- If the model is unavailable or output fails validation twice, the system sends a safe fallback reply and escalates. The participant always receives a response.
- Model selection is configurable (`ENQUIRY_MODEL`) without code changes.

## Constraints and Assumptions

### Technical Constraints

- Flask + CrewAI + SQLAlchemy, matching Modules B and C. The model provider is set by environment (OpenAI or Anthropic).
- Automated tests must not call a model provider; live evaluation is opt-in.

### Business Constraints

- Only Q&M-approved content may be stated as fact.
- PDPA: memory is disclosed at first contact and erasable on request.

### Assumptions

- Participants are identified by account (web) or channel identity (WhatsApp/email).
- The decisions D1–D6 below stand unless Q&M rules otherwise.

## Success Criteria

- ≥95% FAQ answer accuracy on the evaluation set (proposal §8).
- 0 invented Q&M facts in the evaluation set (fact-guard pass rate 100% after fallback).
- 100% correct handling of the rude, abusive, injection, off-topic and unknown-fact evaluation cases.
- Returning-participant evaluation cases recall at least one relevant remembered fact.

## Decisions on Open Questions

The project team decided these on 30 October 2026. Each can be changed in
content or configuration if Q&M rules otherwise.

| # | Question | Decision | Reason | Where to change |
|---|---|---|---|---|
| D1 | **Venue.** The reply template says HQ Clementi Loop; deck slide 2 says Q&M City Square Mall Auditorium. | Do not state a venue. Say that it is confirmed in the enrollment confirmation, and offer to have staff confirm it sooner. No staff case is raised for a venue question alone. | Two Q&M sources conflict, and a wrong venue causes a missed class. The course catalogue already marks venue `confirmation_required`. | `da.venue` entry in `enquiry_knowledge_base.json` |
| D2 | **Follow-up timing.** The proposal says 24h/72h/7d, the deck 30 min/1 day/2 days, and the 5 August 2026 email two-day intervals. | Follow the deck: 30 minutes after the last contact, then 1 day and 2 days after the previous reminder (0.5, 24.5 and 72.5 hours). | The project owner chose the deck's workflow on 31 October 2026, replacing the earlier 48/96/144-hour rule taken from the 5 August 2026 email. | `FOLLOWUP_HOURS` |
| D3 | **Crisis wording.** | Empathetic acknowledgement; 995 for immediate danger; Samaritans of Singapore 24-hour hotline 1767; a staff member is alerted. No counselling is attempted. The helplines are sent only when a message contains a crisis or self-harm cue. Other signs of distress alert staff without helplines (see REQ-A04.5). | Uses only national emergency and crisis lines, not advice. | `fixed_replies.crisis_support` |
| D4 | **Second course.** Front Desk Operations funding. | Use the same basic-tier SkillsFuture and UTAP rules, guided funding flow and response format as Course 1. Apply the 10% Q&M-funded discount (S$45) first, then calculate funding against S$405. The discount expires on 15 November 2026 at 11:59 pm Singapore time; from 16 November calculate against S$450. UTAP support is 50% of the unfunded fee, subject to eligibility, remaining annual allowance and claim approval; do not combine it with SkillsFuture. Previously recorded fees are preserved. Other payment arrangements, content, entry and job details still require confirmation. | Amended 1 November 2026: the project owner explicitly confirmed SkillsFuture and UTAP eligibility for Course 2 and requested Course 1's funding rules, in addition to the time-limited Q&M discount. | Course 2 `skillsfuture`, `utap` and `fee` in `course_information.json`; `fd.*` entries; shared funding calculator |
| D5 | **Language.** | Understand any language, but always reply in simple English. The first time a participant writes in a non-Latin script (e.g. Chinese or Tamil), add one line saying the course is taught and assessed in English. Languages written in Latin letters, such as Malay, get English replies without the note. | Matches the minimum-qualification rule, and avoids unreviewed translations of approved wording. | `fixed_replies.language_note` |
| D6 | **Memory retention.** | Delete a participant memory profile once it has not been updated for `RETENTION_DAYS` (default 365). Conversation summaries are deleted with their conversation by the existing retention job. "Forget me" deletes both immediately. | Uses the project's existing PDPA retention window rather than adding a second policy. | `RETENTION_DAYS` |

## Glossary

| Term | Meaning |
|---|---|
| Approved content | Knowledge-base entries derived from the source register, plus live catalogue facts |
| Escalation | A staff case in the operations portal (`staff_escalations`) |
| Memory profile | Whitelisted facts remembered about one participant across conversations |
| Fact guard | Deterministic check that a reply contains no unapproved Q&M facts |
