# Tasks Document — Module A: Conversational Enquiry Assistant & Lead Nurturing

<!-- Navigation Metadata -->
<!-- Template: Tasks | Level: Draft for review | Prerequisites: requirements.md, design.md -->

## Document Information

- **Feature Name**: Module A — Conversational Enquiry Assistant & Lead Nurturing (rebuild)
- **Version**: 0.1 (draft)
- **Date**: 30 October 2026
- **Author**: Clifton Chen Yi
- **Requirements**: [requirements.md](requirements.md) · **Design**: [design.md](design.md)

## Implementation Overview

### Implementation Strategy

Build the new module beside the router first, with its own tests. Then cut
routing over and delete the old Module A code. The full test suite must stay
green after every phase, and Module B/C behaviour must not change.

### Development Approach

- Test-first for deterministic components (knowledge base, policy, guard, memory).
- A scripted fake AI service for all automated tests; live model calls are opt-in only.
- One commit per task.

## Implementation Plan

### Phase 1: Foundation

- [x] 1.1 Curate `backend/data/enquiry_knowledge_base.json` from the source register
  - Copy reply-template wording verbatim; add deck-derived notes (job scope, HR interview, Mid-Career reason)
  - Mark venue and any non-DA course content `confirmation_required`
  - _Requirements: REQ-A01, REQ-A07_
- [x] 1.2 Implement `services/enquiry/knowledge_base.py` (and `facts.py`) with tests
  - Every entry has `id`, `topic`, `approved_text` and `source`; ids are unique
  - Live intakes and fees override template text; `confirmation_required` entries are never rendered as facts
  - _Requirements: REQ-A01_
- [x] 1.3 Add `ENQUIRY_MODEL` / `ENQUIRY_API_KEY` to `backend/config.py` and `backend/.env.example`
  - Fall back to `OPENAI_MODEL` / `OPENAI_API_KEY`; test the config
  - _Requirements: NFR Reliability_
- [x] 1.4 Add `MemoryFact` and `EnquiryTurn` to `backend/agents/schemas.py`
  - _Requirements: REQ-A01–A09_

### Phase 2: Core Logic

- [x] 2.1 Implement `message_policy.py` with a table-driven test of the stop, forget, human, crisis, injection and none signals
  - _Requirements: REQ-A04, REQ-A05, REQ-A08.7, REQ-A09_
- [x] 2.2 Implement `fact_guard.py` with table-driven tests
  - Reject an invented fee, date, time or URL, and internal terms
  - Accept the "$100 top-up" when S$500 credit was stated against the S$600 fee
  - Reject Q&M facts in general-knowledge replies
  - _Requirements: REQ-A01.3, REQ-A02.3, REQ-A03.3, REQ-A05.2, REQ-A07.3_
- [x] 2.3 Add `models/memory.py` (`ParticipantMemory`, `ConversationSummary`) plus an additive migration and migration test
  - _Requirements: REQ-A08_
- [x] 2.4 Implement `memory_service.py` with tests
  - Key whitelist; NRIC, email, phone and DOB rejection; list caps
  - Same-subject isolation across two users; forget-me; summary trigger
  - _Requirements: REQ-A08_
- [x] 2.5 Add `agents/enquiry_agent` and `CrewAIService.converse_enquiry`
  - Contract test of the prompt sections and output schema, following `test_crewai_contracts.py`
  - _Requirements: REQ-A01–A05_
- [x] 2.6 Implement `enquiry_service.py` with service tests using a fake AI service
  - One test per response-policy row, asserting the reply type and side effect
  - Guard retry then fallback; provider error fallback
  - _Requirements: REQ-A01–A09_

### Phase 3: Routing Cut-over

- [x] 3.1 Route non-enrollment intents in `ChatService.respond` to `EnquiryService`
  - Keep Module B pre-emption and enrollment branches unchanged
  - _Requirements: REQ-A09.3_
- [x] 3.2 Port still-valid behaviour from old Module A tests into `enquiry_tests`
- [x] 3.3 Delete old Module A code and tests
  - `agents/faq_agent`, `services/lead_nurturing.py`, the FAQ/quick-action/course-focus helpers in `chat_service.py`, `CrewAIService.answer_faq` / `FAQAnswer`
  - Check imports first and keep anything Module B/C uses
- [x] 3.4 Run the full backend suite and fix any Module B/C regressions

### Phase 4: User Interface

- [x] 4.1 Return `quick_replies` and `routing.response_type` from `/api/chat`, with an API test
  - _Requirements: REQ-A06_
- [x] 4.2 Render quick replies and the first-contact privacy line in the chat UI, with component tests
  - _Requirements: REQ-A06.2_

### Phase 5: Follow-ups and Evaluation

- [x] 5.1 Implement `followup_composer.py` and call it from `OperationsService.run_followups`
  - Tests for memory-aware text, template fallback, and no sending after opt-out or escalation
  - _Requirements: REQ-A10_
- [x] 5.2 Write the evaluation set `enquiry_tests/eval/enquiry_cases.jsonl` (78 cases) and its opt-in `live_provider` runner
  - Cover every template FAQ, plus rude, abusive, distress, injection, gibberish, off-topic, unknown-fact and memory-recall cases
  - _Requirements: Success Criteria_
- [x] 5.3 Run the evaluation with an OpenAI model and record the results ([evaluation-results.md](evaluation-results.md): 70/70 with `gpt-4o-mini`, twice)
- [ ] 5.4 Run the evaluation with `anthropic/claude-sonnet-5-5` once an Anthropic key is configured and the `anthropic` package is installed

### Phase 6: Documentation

- [x] 6.1 Update the README Module A section and architecture diagram
- [x] 6.2 Write a content-update guide for staff editing `enquiry_knowledge_base.json` ([content-update-guide.md](content-update-guide.md))
- [x] 6.3 Prepare decisions D1–D6 for Q&M sign-off (sign-off sheet published as a shareable page; sending it to Q&M is the project team's step)
- [x] 6.4 Extend `RetentionService` to delete memory profiles idle beyond `RETENTION_DAYS` (D6)
  - _Requirements: REQ-A08.8_

### Follow-ups

- [ ] 7.1 Staff-portal editor for `enquiry_knowledge_base.json`. The portal's existing course/FAQ editor feeds only the Enrollment Agent's reference information, so Module A wording is currently edited in the file.
- [x] 7.2 Store a credit balance stated in a turn that the shared classifier routes to Module B (deterministic capture on every message)
- [x] 7.4 Mid-enrollment evaluation: rude, general, unknown-fact and fee messages during an open enrollment reach Module A and keep the enrollment resumable (`mid_enrollment` cases)
- [ ] 7.3 Record Q&M's sign-off of D1–D6 in requirements.md and update content for any decision changed

## Requirements Traceability

| Requirement | Tasks |
|---|---|
| REQ-A01 Approved answers | 1.1, 1.2, 2.2, 2.5, 2.6 |
| REQ-A02 Unknown Q&M questions | 2.2, 2.6 |
| REQ-A03 General questions | 2.2, 2.5, 2.6 |
| REQ-A04 Rude / abusive / distress | 2.1, 2.6 |
| REQ-A05 Adversarial / unclear | 2.1, 2.2, 2.6 |
| REQ-A06 First contact | 4.1, 4.2 |
| REQ-A07 SkillsFuture guidance | 1.1, 2.2 |
| REQ-A08 Memory | 2.3, 2.4, 2.6 |
| REQ-A09 Escalation / opt-out / hand-off | 2.1, 2.6, 3.1 |
| REQ-A10 Follow-ups | 5.1 |

## Verification Commands

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests -q
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\enquiry_tests -q
Set-Location frontend; npm test; npm run build
# Opt-in, uses provider credits:
$env:RUN_LIVE_ENQUIRY_EVAL="1"; .\backend\.venv\Scripts\python.exe -m pytest -m live_provider backend\tests\enquiry_tests\test_live_enquiry_evaluation.py -q -s
```
