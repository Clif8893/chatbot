# Design Document — Module A: Conversational Enquiry Assistant & Lead Nurturing

<!-- Navigation Metadata -->
<!-- Template: Design | Level: Draft for review | Prerequisites: requirements.md -->

## Document Information

- **Feature Name**: Module A — Conversational Enquiry Assistant & Lead Nurturing (rebuild)
- **Version**: 0.1 (draft)
- **Date**: 30 October 2026
- **Author**: Clifton Chen Yi
- **Requirements**: [requirements.md](requirements.md)

## Overview

Module A is rebuilt as a self-contained enquiry service that sits behind the
shared intent router. Each turn makes one structured model call. Deterministic
code surrounds that call: pre-checks before it, fact checking after it, and
memory, escalation and lead-state updates as side effects. The model writes the
conversational reply. Python decides what may be stated as fact and what is
remembered.

### Design Goals

- Every Q&M fact in a reply is traceable to approved content (REQ-A01, A02).
- Every input receives an appropriate reply, including off-topic, rude, distressed and adversarial ones (REQ-A03–A05).
- Personalisation from current and past chats, with strict per-participant isolation (REQ-A08).
- Testable without a model provider.

### Key Design Decisions

| Decision | Reason |
|---|---|
| Flask + CrewAI, same as Modules B/C | One stack and one test approach; the proposal's n8n is not used in this repository |
| Model set by `ENQUIRY_MODEL` | Allows Claude (as in the proposal) or OpenAI without code changes |
| Whole knowledge base in every prompt | The approved content is small (~3k tokens). No retrieval errors and no vector store to operate. |
| One structured output (`EnquiryTurn`) per turn | The reply, its type, citations, tone, escalation and memory updates come from the same reasoning step |
| Deterministic fact guard | Model instructions alone cannot guarantee no invented prices or dates |
| Typed memory keys only | Prevents memory being used to inject instructions or store personal identifiers |
| Replace, not extend, the old Module A code | The old logic is interleaved with routing in `chat_service.py` and is hard to test in isolation |

## Architecture

### System Context

```
Participant (web chat; WhatsApp/email inbound)
        │
        ▼
POST /api/chat ──► ChatService.respond  (router)
                     ├─ crisis / STOP / forget me / staff request
                     │      ──► EnquiryService, in any state           [Module A]
                     ├─ Module B pre-emption: management actions, corrections,
                     │  saved-detail lookups, displayed course/intake choices,
                     │  invoice requests, offer/interest state machine  [unchanged]
                     ├─ CrewAIService.classify (shared intent router)  [kept]
                     ├─ enrollment_interest ──► _interest_reply         [Module B entry]
                     ├─ enrollment, progress, pause, resume, and workflow
                     │  questions with saved state ──► Module B         [unchanged]
                     └─ everything else ──► EnquiryService.handle       [Module A]
                            └─ handoff_enrollment ──► _interest_reply
```

Classification guards (added after live evaluation showed the shared classifier over-using `enrollment_interest`):
- **Questions, greetings and stated credit answered first:** a question, a greeting or a stated SkillsFuture balance that the classifier labels `enrollment_interest` is answered by Module A first. Module A still hands off on a clear wish to enrol.
- **Pause needs a pause cue:** `pause_workflow` is handled by Module B only when the message contains a pause cue ("later", "not now", "not sure", "take a break"…). Frustration such as "why so many questions" goes to Module A, which de-escalates and adds "continue where you left off".
- **Deterministic memory capture:** a stated SkillsFuture balance (`memory_service.stated_credit`) is saved to the participant's profile on every message, whichever module answers it.
- **Course-name shortcut:** "register for <course>" counts as a course selection only when it is not a question.
- **Pause:** `pause_workflow` is handled by Module B only when an enrollment is open.
- **Hand-off:** Module A hands off only with an explicit `handoff_enrollment` reply and no escalation, so a staff request is never replaced by the enrollment offer.

Router rules at the Module A / Module B boundary (`ChatService._enquiry_reply`):
- **Open draft:** if a draft is open, the enquiry reply ends with Module B's "continue where you left off" prompt, and pending enrollment state is kept.
- **Enrollment offer:** an approved reply that offers enrollment ("interested in enrolling, let me know") sets the enrollment-offer follow-up, so "yes" or "no thanks" is understood by Module B.
- **Date listings:** a reply citing one course's date listing (`lists_intakes` entries) records those intake options, so a bare "18 November" next is treated as interest in that intake.
- **First name:** the participant's first name from their draft is passed to Module A for personalisation. It is never stored in memory.
- **Button ids:** the web button `quick_action` ids are still accepted by the API, but routing uses only the message text.

### High-Level Architecture

```
EnquiryService.handle(conversation, user_message, prior_messages)
  1. MessagePolicy.precheck(text)            → PolicySignal (stop / forget / human /
                                                crisis / injection / none)
       stop, forget  → fixed reply + side effect, no model call
  2. MemoryService.build_context(conversation)
       → current window (NRIC-masked) + conversation summary
         + participant profile + last 3 prior-conversation summaries
  3. KnowledgeBase.render(course_focus)      → approved entries + live fees/intakes
  4. CrewAIService.converse_enquiry(...)     → EnquiryTurn
  5. FactGuard.check(turn, allow_list)       → ok | violations
       violations → one retry with feedback → still failing → safe fallback + escalate
  6. Apply effects: memory updates, abuse strikes, escalation, consent,
     lead state (record_inbound / awaiting_response), Module B hand-off
  → EnquiryReply(text, response_type, quick_replies, escalation_category,
                 handoff_enrollment, ...)   — the router persists it
                 (agent_name="enquiry_agent") and commits
```

### Technology Stack

| Layer | Choice |
|---|---|
| Agent runtime | CrewAI 1.15.21 (`Agent`, `Task`, `output_pydantic`) |
| Model | `ENQUIRY_MODEL` (falls back to `OPENAI_MODEL`); e.g. `anthropic/claude-sonnet-5-5` |
| Persistence | SQLAlchemy (SQLite locally, PostgreSQL via `DATABASE_URL`) |
| Encryption | `EncryptedText` (`backend/services/security.py`) |
| Tests | pytest with a scripted fake AI service; opt-in `live_provider` evaluation |

## Components and Interfaces

### Component 1: KnowledgeBase (`backend/services/enquiry/knowledge_base.py`)

- **Input:** `backend/data/enquiry_knowledge_base.json` plus the live `CourseCatalogue` (fees, durations, intakes from the staff-managed `course_dates` table).
- **Interface:**
  - `entries`, `entry(id)`, `entry_ids()`, `entries_for(course_id)`
  - `resolve(entry) -> str`
  - `render(course_id=None) -> str`
  - `fact_allow_list() -> FactSet`
  - `fixed_reply(name, **values)`, `quick_replies`
- **Placeholders:**
  - Course entries: `{course_name}`, `{course_fee}`, `{duration}`, `{training_time}`, `{website}`, `{paynow_uen}`, `{claim_page}`, `{intake_list}`
  - General entries: `{course_list}`
  - They are filled from the catalogue on every call, so approved wording never carries a stale fee or date.
  - A value staff have not filled in renders as "to be confirmed by our team", and is never guessed or treated as an error.
- **`lists_intakes`:** marks entries that show one course's selectable dates. `intake_listing_course(entry_ids)` drives the bare-date hand-off to Module B.
- **Load-time validation:** the loader raises `KnowledgeBaseError` for any of these:
  - duplicate ids
  - a missing field or source
  - an unknown or retired course
  - an unsupported placeholder
  - `lists_intakes` on an entry without a course intake list
  - a missing fixed reply
  - an over-long quick reply
- **Rules:**
  - Entries with `confirmation_required: true` are rendered with a "NOT CONFIRMED: reply only with the text below" marker.
  - Maintainer `notes` are never rendered.
  - Full intakes and historical template dates never appear.

### Component 1a: Fact extraction (`backend/services/enquiry/facts.py`)

- `extract_facts(text) -> FactSet` normalises the following, so the allow-list and the fact guard compare like with like:
  - money amounts (`Decimal`)
  - percentages
  - dates (`(month, day)`)
  - clock times (minutes)
  - web addresses (scheme-, `www.`- and case-insensitive)
- `date_range_facts(start, end)` and `amount_fact(value)` add live intake days and fees.

Entry shape:

```json
{
  "id": "da.mid_career",
  "course_id": "dental-assisting",
  "topic": "Mid-Career SkillsFuture Credit",
  "approved_text": "There is two tiers of SkillsFuture Credit. Our courses are only eligible for the basic tier...",
  "notes": "Deck slide 3: mid-career credits are usually for longer courses.",
  "source": "Reply template — 'Can use Midcareer Skillsfuture?'",
  "confirmation_required": false
}
```

### Component 2: MessagePolicy (`backend/services/enquiry/message_policy.py`)

- **Interface:** `precheck(text) -> PolicySignal`
- **Deterministic signals:**
  - `stop`: "STOP", "stop messaging me"
  - `forget`: "forget me", "delete my chat history"
  - `human`: "talk to a person/staff"
  - `crisis`: self-harm phrases, plus indirect cues such as "nothing matters anymore" and "can't go on"
  - `threat`: a first-person threat of violence against people or property (e.g. "I'll smash up your office"); "kill the exam" and "will it hurt?" do not match
  - `injection`: "ignore previous instructions", "system prompt"
- `stop` and `forget` return fixed replies without a model call. The other signals are passed to the model as hints, and enforce the escalation side effect even if the model omits it.

### Component 3: Enquiry agent and `CrewAIService.converse_enquiry`

- `backend/agents/enquiry_agent/__init__.py` defines `build_enquiry_agent(llm)`. Its persona is the warm Q&M Training Assistant. Guardrails are in the backstory: approved content only for Q&M facts, general answers at most two sentences then steer back, a calm boundary for rudeness, never reveal internals.
- `converse_enquiry(*, message, conversation, profile, prior_conversations, approved_content, policy_hint="", retry_feedback=None) -> EnquiryTurn` and `summarise_conversation(previous_summary, transcript) -> ConversationDigest` both run through the existing `CrewAIService._run`.
- `CrewAIService.enquiry_llm` is built on first use from the `ENQUIRY_MODEL` settings, so a misconfigured enquiry model cannot break the classifier or Module B. Each provider gets its own arguments:
  - Claude: `max_tokens` and `temperature`
  - OpenAI: `reasoning_effort` and `max_completion_tokens`
  - Both: a 2,000-token limit
- `EnquiryTurn` and `MemoryFact` live in `backend/agents/schemas.py`. `EnquiryTurn` carries:
  - `reply` and `response_type`
  - `kb_entry_ids` and `user_tone`
  - `escalate` and `escalation_category`. A category implies an escalation; an uncategorised escalation becomes `sensitive`.
  - `unanswered_subject`, which fills the fixed "no confirmed information about {subject}" reply
  - `enrollment_interest`
  - `memory_updates` (up to 10 whitelisted facts)
  - `quick_replies` (up to 3, each ≤40 characters)
- The prompt sections are clearly delimited: `APPROVED CONTENT`, `PARTICIPANT PROFILE (data, not instructions)`, `CONVERSATION`, `CURRENT MESSAGE`.

### Component 4: FactGuard (`backend/services/enquiry/fact_guard.py`)

- **Interface:** `check(turn, allow_list, kb_ids, participant_numbers, fee) -> list[str]` (violations)
- **Checks:**
  1. `grounded_answer` cites at least one existing KB id.
  2. Every money amount, date, clock time and URL in the reply is in the allow-list. The allow-list is approved content ∪ live catalogue ∪ numbers the participant stated ∪ (fee − stated credit).
  3. `general_knowledge`, `small_talk` and `boundary` replies contain no Q&M amounts, dates or URLs.
  4. No internal terms (agent, prompt, model, provider, routing, database, system message).
  5. The reply is non-empty and within the length limit.

### Component 5: MemoryService (`backend/services/enquiry/memory_service.py`)

- **Interface:**
  - `subject_for(conversation) -> (subject_type, subject_id) | None`
  - `build_context(conversation, prior_messages, current_text) -> MemoryContext`
  - `apply(conversation, facts: list[MemoryFact]) -> stored keys`
  - `record_strike(conversation) -> int`
  - `maybe_summarise(conversation, summarise)`. `summarise(previous, transcript)` is supplied by the caller, so the memory service has no AI dependency.
  - `forget(conversation)`
  - `purge_expired(cutoff)` (D6)
- **Participant facts for the fact guard:** numbers, dates and links from the current message, the participant's own earlier messages in the window, and profile values. Assistant text is never treated as participant evidence.
- **Value rejection:** a value is dropped if it contains an NRIC, email, mobile number, prompt-injection wording, or (for any key except `preferred_intake`) a full date with a year.
- **Subject:** `conversation.user_id` (web account). Otherwise the `ContactMapping.identity_digest` (WhatsApp/email). Otherwise none, and only the current chat is used.
- **Write rules:**
  - Keys must be in the `MemoryFact` literal.
  - Values pass through `mask_nrics_in_text` and are rejected if they look like an NRIC, email, phone number or date of birth.
  - `concern`, `constraint` and `topic_answered` are lists of the latest 5. Other keys are single values, with the latest one winning.
- **Summaries:** when a conversation has more than the context window plus 10 unsummarised messages, a summarise call updates `ConversationSummary`. Summaries of the last 3 other conversations for the same subject are included.

### Component 6: EnquiryService (`backend/services/enquiry/enquiry_service.py`)

`handle(conversation, user_text, prior_messages, first_contact=False) -> EnquiryReply`

`EnquiryReply` has these fields:
- `text`, `response_type` and `quick_replies`
- `escalation_category` and `handoff_enrollment`
- `used_fallback` and `kb_entry_ids`

The router persists both messages and commits. That keeps `ConversationRepository.add_message` bookkeeping in one place. On hand-off, the router passes the turn to Module B.

Additional rules:
- A crisis signal gets the fixed crisis-support reply even when the model call fails.
- A human-contact signal is escalated even if the model omits it.
- Buttons (`quick_replies`) come from `suggestions.choose`, described in Component 6a.
- The first Module A reply in a conversation carries the privacy notice once; this is checked against stored messages, not the recent window. A first-contact reply also gets the full menu.
- The language note (D5) is added once per conversation, for messages mostly in a non-Latin script.

It orchestrates the steps above and owns the side effects:

| Turn outcome | Side effect (existing service) |
|---|---|
| `escalate` / policy `human` / `crisis` / guard double-fail | `OperationsService.escalate(conversation_id, reason, category)` |
| Strike count reaches 3 | `escalate(..., "abusive_conduct")` (also stops follow-ups) |
| Policy `stop` | `OperationsService.set_consent(conversation_id, False, source="chat")` |
| Every participant message | `OperationsService.record_inbound` |
| Reply sent and lead not enrolled | `OperationsService.awaiting_response` |
| `enrollment_interest` | Set `conversation.active_intent = "enrollment"` and use the Module B lead hand-off (`services/enrollment/lead_handoff_service.py`) |

### Component 6a: Suggestion buttons (`backend/services/enquiry/suggestions.py`)

Live use showed free-text buttons from the model were unhelpful: vague ("What else can I ask?") or repeating topics already covered. Buttons now work as follows:

- **Approved menu:** buttons come only from `suggestions` in the knowledge base. Each item has an `id`, a `label` (≤40 characters, sent as the participant's message when tapped) and `covers`, the approved entries it leads to.
- **The model picks:** it returns up to five candidate ids in `EnquiryTurn.suggestions`, chosen as the participant's likely next step. The prompt shows the menu and gives the rules.
- **The application filters**, in `choose()`:
  - it drops unknown ids and duplicates
  - it drops any item whose covered entries include one already answered for this participant, in this chat or an earlier one, or cited in the current reply
  - it drops "enrol" while the participant is already enrolling
  - it always puts "Talk to staff" first on a `deescalation` reply, so a frustrated participant is offered a person (REQ-A04.1)
  - it shows three at most
  - if nothing is left, it fills from `suggestion_order`
- **When no buttons are shown:**
  - while Module B is collecting details (a continuation prompt is present)
  - for `opt_out`, `forget_me` and `handoff_enrollment` replies
  - for distress, abuse closes and staff requests
- **Answered topics are recorded by the application:** the ids of entries cited in each reply go into the profile as `topic_answered`, up to 50. The model's own free-text topic notes are discarded. The profile shows these to the model as topic names, not ids.
- **Opening menu:** `starter_labels()` gives the opening menu returned by `POST /api/conversations`, and by `GET …/messages` for an empty chat. It leaves out topics this participant covered in earlier chats, topped up to at least three from `suggestion_order`.

### Component 7: FollowupComposer (`backend/services/enquiry/followup_composer.py`)

- `compose(conversation, step, total_steps) -> str` starts from the approved template for that step (`followup_templates` in the knowledge base). It personalises the template through `CrewAIService.compose_followup`, using the memory profile and the last 10 messages.
- **Template fallback:** the template is sent unchanged when:
  - there is nothing to personalise (no memory and no messages), in which case no model call is made
  - the model is unconfigured or fails
  - the text fails the fact guard
  - the text drops "STOP"
  - the text exceeds 600 characters
- **Template rules:** templates must contain "STOP", carry no placeholders, and state no facts. They replaced the old numbered-menu reminders in `course_information.json`.
- **Scheduler:** `OperationsService.run_followups` keeps its scheduling, consent, opt-out and escalation rules. It asks `followup_body(step, conversation_id)` for the text, which delegates to the composer that `create_app` attaches.

## Data Models

### Entity 1: ParticipantMemory (`participant_memories`)

| Column | Type | Notes |
|---|---|---|
| id | String(36) PK | |
| subject_type | String(16) | `user` or `contact` |
| subject_id | String(64) | user id or contact identity digest; unique with subject_type |
| facts | EncryptedText (JSON) | `{key: [{value, conversation_id, recorded_at}]}` |
| created_at / updated_at | DateTime(tz) | `updated_at` drives retention (D6) |

### Entity 2: ConversationMemory (`conversation_memories`)

| Column | Type | Notes |
|---|---|---|
| conversation_id | String(36) PK, FK conversations | |
| summary | EncryptedText, nullable | NRIC, email and mobile redacted; ≤ 1,200 characters |
| through_sequence | Integer | last message sequence the summary covers |
| forgotten_through_sequence | Integer | messages up to here were forgotten on request and are never used as context again |
| abuse_strikes | Integer | rude/abusive turns in this conversation (REQ-A04.3) |
| updated_at | DateTime(tz) | |

Abuse strikes are counted per conversation rather than per participant, so one
bad day does not follow someone forever.

Both tables are new, so `db.create_all()` creates them on existing databases
without touching other data; no ALTER migration is needed. `RetentionService`
deletes `conversation_memories` rows before their conversation.

### Data Flow

```
message → precheck → build_context (messages, summary, profile, prior summaries)
        → KB render → model → EnquiryTurn → FactGuard → reply
                                                 └─► memory.apply / escalate / lead state
```

## API Design

No new endpoints. Additions to existing responses:

| Endpoint | Addition |
|---|---|
| `POST /api/chat` | `quick_replies` on every response, empty for Module B turns. `routing.response_type` on Module A turns. |
| `POST /api/conversations` | `quick_replies`: the starter menu |
| `GET /api/conversations/<id>/messages` | `quick_replies`: the starter menu for an empty conversation, otherwise empty (earlier turns' suggestions are not stored) |

```json
{
  "message": "…",
  "quick_replies": ["Upcoming dates", "Talk to staff"],
  "routing": { "intent": "faq", "agent": "enquiry", "rule": "enquiry_grounded_answer", "response_type": "grounded_answer" }
}
```

`routing` remains development metadata. KB ids, tone and memory are never returned to the client.

### Web client

- **Quick replies:** `ChatbotPage` shows the latest response's `quick_replies` through `QuickReplies`. Choosing one sends its label as an ordinary message, so the static buttons and `quick_action` ids are gone.
- **During enrollment:** Module B replies carry no suggestions, so the buttons disappear naturally; no separate "enrollment started" check is needed.
- **Privacy line:** it is part of the reply text and renders as normal text. It is not styled specially, because that would tie the UI to staff-editable wording.
- **Old clients:** the API still accepts `quick_action` ids from older clients and ignores them.

## Response Policy

| Input | `response_type` | Reply behaviour | Side effect |
|---|---|---|---|
| Covered course question | `grounded_answer` | Approved content tailored to the question | — |
| Q&M question not covered | `no_confirmed_info` | "I don't have confirmed information about …" plus an offer to check | escalate `unanswered_question` |
| Harmless general question | `general_knowledge` | ≤2 sentences of general info, then steer back | — |
| Out-of-bounds general question | `boundary` | Polite decline and redirect | — |
| Frustration / mild rudeness | `deescalation` | Acknowledge, answer the underlying point, offer a human | strike if rude |
| Insult / profanity | `boundary` | One courteous boundary line, offer help | strike |
| Third strike / threat | `boundary` | Neutral close (a detected threat always closes) | escalate `abusive_conduct` |
| Crisis cue (deterministic) | `deescalation` | Empathy plus approved helpline wording | escalate `distress` |
| Model-judged distress, no crisis cue | `deescalation` | Kind reply, no helplines | escalate `distress` for staff review |
| Injection / prompt request | `boundary` | Decline, continue helping; if guarded twice, the fixed `decline_instructions` reply | — |
| Unclear | `clarifying_question` | One question plus quick replies | — |
| Greeting / thanks | `small_talk` | Brief and warm; returning participants are welcomed back | — |
| Intent to enroll | `handoff_enrollment` | Confirm, then Module B continues | hand-off |

## Security Considerations

### Authentication

Unchanged. Web participants are signed in, and the conversation's owner is enforced by the existing API.

### Authorization

Memory lookups are keyed only by the current conversation's subject. No query reads another subject's memory or summaries. A dedicated test covers this.

### Data Protection

- Memory facts and summaries are encrypted at rest.
- NRIC values are masked in all model context.
- Personal identifiers are rejected from memory.
- "Forget me" deletes the profile and summaries. Transcripts follow the existing retention policy (`retention_service.py`).

### Prompt-injection resistance

- Profile and summaries are rendered inside a block labelled as data.
- Memory keys are typed, and values are truncated to 200 characters.
- The fact guard blocks unapproved facts even if the model is manipulated.

## Error Handling

| Failure | Behaviour |
|---|---|
| Model/provider error (`AIServiceError`) | Safe fallback reply (offer staff help); mark the user message as an error; escalate `unanswered_question` once |
| Fact guard fails twice | Safe `no_confirmed_info` reply plus escalation |
| Memory write rejected | Drop the fact silently and log the category only (no value) |
| Summarise call fails | Keep the previous summary; retry on a later turn |

## Testing Strategy

- **Unit** (`backend/tests/enquiry_tests/`, no network):
  - knowledge base loading and rendering
  - message policy table
  - fact guard table
  - memory whitelist, PII rejection, isolation and forget
  - follow-up composer fallback
- **Service:** `EnquiryService` with a scripted fake AI service returning fixed `EnquiryTurn` objects, covering each response-policy row and its side effects.
- **Integration:** `/api/chat` through the test client for first contact, returning participant, enrollment hand-off, and Module B regressions.
- **Evaluation (opt-in `live_provider`):** 78 multi-turn cases in `enquiry_tests/eval/enquiry_cases.jsonl` across 14 categories, each traced to REQ-A01 to A09.
  - `eval/runner.py` plays each case through `/api/chat` and scores every turn on: the answering module, the response type, required and forbidden phrases, new staff cases, and the word limit.
  - Category thresholds follow the Success Criteria: FAQ ≥95%; safety categories 100%; memory ≥80%, with every miss reported.
  - `test_eval_cases.py` always runs offline. It validates the case file against `requirements.md`, tests the scoring, and plays a few cases with the fake model.
  - `test_live_enquiry_evaluation.py` runs only with `RUN_LIVE_ENQUIRY_EVAL=1`. `EVAL_CASES` selects case ids, and `EVAL_REPORT_PATH` keeps the JSON report.
