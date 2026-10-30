# Enrollment System Overview

## Overview

The Enrollment Agent converts free-text participant messages into a typed Pydantic result. It cannot confirm an enrollment or write financial data. Deterministic Python then validates the fields, updates the enrollment draft, allocates an intake, exports the Master Invoice workbook, generates the invoice PDF, and queues email delivery.

> **Control boundary:** AI is used only for language understanding and extraction. Validation, persistence, financial calculations, document generation, and SMTP delivery contain no LLM or CrewAI decision-making.

## Key Files & Responsibilities

| File | Responsibility |
|---|---|
| `backend/agents/enrollment_agent/__init__.py` | Defines the CrewAI Enrollment Agent, including its role, goal, instructions, tools, and safety settings. |
| `backend/services/crewai_service.py` | Builds the extraction `Task`, runs a sequential `Crew`, calls `kickoff()`, and returns `EnrollmentExtraction`. |
| `backend/tools/` | Provides five typed, read-only tools to the Enrollment Agent. |
| `backend/services/enrollment/enrollment_service.py` | Owns the enrollment state machine, deterministic post-extraction validation, corrections, confirmation, and persistence. |
| `backend/services/enrollment/lead_handoff_service.py` | Validates and idempotently merges future enquiry-lead data into the conversation's existing lead and enrollment draft. |
| `backend/services/enrollment/validation.py` | Validates NRIC/FIN checksum, DOB and age, email, mobile, intake, payment method, and monetary values. |
| `backend/data/course_information.json` | Structured source for supported courses, fees, payment rules, intake definitions, FAQs, and reply templates. |
| `backend/models/__init__.py` | Defines `Conversation`, `Message`, and `EnrollmentDraft` persistence models. |
| `backend/services/master_invoice_service.py` | Uses `openpyxl` and `filelock` to maintain one multi-enrollment Master Invoice workbook. |
| `backend/services/invoice_template_service.py` | Populates the official Q&M invoice workbook with `openpyxl`; `ExcelPdfConverter` uses Excel COM through `win32com` to export PDF. |
| `backend/services/invoice_email_service.py` | Creates idempotent transactional-outbox records and manages queue claiming, status, retries, and resend rules. |
| `backend/services/delivery_service.py` | Builds MIME email and sends it with `smtplib`, `ssl`, EHLO, STARTTLS, and authenticated SMTP. Gmail uses `smtp.gmail.com:587` when configured as the SMTP host. |

### Five Read-Only Enrollment Tools

| Tool | Purpose |
|---|---|
| `conversation_memory_tool.py` | Reads owner-scoped recent messages and workflow state; masks NRIC/FIN. |
| `course_information_tool.py` | Reads approved course and enrollment information. |
| `enrollment_draft_tool.py` | Reads saved, missing, and invalid draft fields using privacy-safe values. |
| `enrollment_validation_tool.py` | Previews deterministic validation without persisting changes. |
| `intake_availability_tool.py` | Reads database-backed intake dates, status, and capacity. |

These tools provide controlled context only. They do not confirm enrollment, modify financial records, generate invoices, or send email.

## Enrollment flow

The application reads the saved draft before every prompt and asks for only the first missing field. The order is selected course, intake, full name, mobile number, email, date of birth, and finally NRIC/FIN. NRIC/FIN is not part of initial lead capture: it is requested only after the contact and DOB fields, with an explanation that it is required for official registration and invoice preparation. Displayed NRIC/FIN values are always masked.

FAQ questions, pauses, and casual messages do not clear the draft. The answer is followed by the same next missing enrollment question. When every value is present and valid, one complete masked summary is displayed and explicit confirmation is required before submission.

## Shared database state

| Record | Enrollment data |
|---|---|
| `Conversation` | Shared conversation ID, active intent, pending prompt/action state, and owner. |
| `Lead` | One row per conversation; stores lead status and nurturing state. |
| `EnrollmentDraft` | One row per conversation; stores full name, mobile, email, DOB, NRIC/FIN, primary course/intake mirror, validation state, and enrollment status. |
| `EnrollmentCourse` | Canonical selected course and intake rows linked to the draft. |
| `Message` | Ordered conversation history linked by the same conversation ID. |

## Core Features

- **Deterministic field progression:** CrewAI extracts natural-language values, while `EnrollmentService` selects the next field from the current database record.
- **NRIC/FIN checksum:** a weighted sum, identity-prefix offset, and S/T/F/G/M lookup table verify the final checksum letter.
- **Age validation:** DOB must be a valid past date and the calculated age must be within the configured 16–100 range.
- **Duplicate detection:** lead handoff blocks the same phone/course/intake in another conversation, and final enrollment checks phone/course/intake as well as participant identity.
- **Read-only AI tools:** CrewAI can inspect authorised context but cannot write the database or financial workflow.
- **Master Invoice export:** `openpyxl` creates or reopens the multi-enrollment workbook; `filelock` prevents conflicting concurrent writes.
- **Official invoice PDF:** `openpyxl` fills the Q&M workbook and `win32com` automates Excel's PDF export on Windows.
- **Decoupled email outbox:** invoice email work is persisted before delivery, claimed atomically, and processed separately from enrollment confirmation.
- **Gmail SMTP transport:** `DeliveryService` uses `smtplib` and TLS; the configured Gmail endpoint is `smtp.gmail.com:587`.
- **Zero-hallucination financial boundary:** after Pydantic extraction, no LLM/CrewAI code calculates fees, validates balances, writes records, creates Excel/PDF files, or dispatches email.

## Quick Code Verification

| Check | Verified location |
|---|---|
| Checksum algorithm | `backend/services/enrollment/validation.py:250` — `has_valid_identity_checksum()`; constants at lines 15–23 |
| Age calculation and limits | `backend/services/enrollment/validation.py:60` — `calculate_age()`; `validate_field()` starts at line 67 and applies DOB limits around lines 130–139 |
| Required enrollment fields | `backend/services/enrollment/enrollment_service.py` — `ENROLLMENT_FIELDS` and `IDENTITY_FIELDS` |
| Enquiry lead handoff | `backend/services/enrollment/lead_handoff_service.py` — `EnrollmentLeadHandoffService.receive()` |
| Agent definition | `backend/agents/enrollment_agent/__init__.py:6` — `build_enrollment_agent()` |
| Extraction prompt and Pydantic output | `backend/services/crewai_service.py:117` — `extract_enrollment()`; `output_pydantic=EnrollmentExtraction` near line 157 |
| Read-only tool construction | `backend/tools/enrollment/__init__.py:10` — `build_enrollment_tools()` |
| Persistent draft | `backend/models/__init__.py:73` — `EnrollmentDraft` |
| Course rules | `backend/data/course_information.json` — `course`, `additional_courses`, and `enrollment` sections |
| Excel workbook export | `backend/services/master_invoice_service.py:67` — `MasterInvoiceService`; `export_confirmed_enrollment()` at line 93; `_new_workbook()` at line 551 |
| PDF conversion | `backend/services/invoice_template_service.py:51` — `ExcelPdfConverter`; `convert()` at line 54; invoice entry point `InvoiceTemplateService.generate()` at line 120 |
| Email queue | `backend/services/invoice_email_service.py:58` — `InvoiceEmailService.queue_automatic()`; queue processor at line 147 |
| SMTP dispatch | `backend/services/delivery_service.py:188` — actual implementation is `DeliveryService._email()`; the requested `_send_smtp_email()` name does not exist in this repository |
| Queue CLI | `backend/app.py:624` — `process-invoice-emails` |
| Controlled live-test CLI | `backend/app.py:655` — `send-test-invoice-email` |

## Simulating Clifton's future handoff

No enquiry-agent behavior is required for development testing. Create a normal conversation first, then run the development-only seed command (it refuses to run when `PRODUCTION` is enabled):

```powershell
python -m flask --app backend.app seed-enrollment-lead `
  --conversation-id <existing-conversation-id> `
  --full-name "Dhiraj Test" `
  --phone-number 91234567 `
  --course "Basic Certificate in Dental Assisting" `
  --intake "18 November 2026" `
  --lead-status qualified
```

The future enquiry agent should pass those six fields to `EnrollmentLeadHandoffService.receive()` inside the Flask application transaction and commit only after it succeeds. Replaying an identical handoff is safe. A conflicting value for the same conversation, an invalid field, or a duplicate phone/course/intake raises a deterministic error. The enquiry agent must not write `Lead`, `EnrollmentDraft`, or `EnrollmentCourse` directly.

## Offline test commands

```powershell
python -m pytest backend/tests/enrollment_tests/test_enrollment_lead_handoff.py -q
python -m pytest backend/tests/enrollment_tests -q
cd frontend
npm test
```

The backend tests inject `FakeAIService`; no OpenAI request or credit is used.

## Architecture

```text
┌──────────────────────────────┐
│ Participant message          │
└──────────────┬───────────────┘
               │
               v
┌──────────────────────────────┐
│ CrewAI Enrollment Agent      │
│ + five read-only tools       │
│ Natural-language extraction  │
└──────────────┬───────────────┘
               │
               v
┌──────────────────────────────┐
│ Pydantic EnrollmentExtraction│
└──────────────┬───────────────┘
               │
═══════════════╪════════════════  AI STOPS HERE
               │                  PURE DETERMINISTIC PYTHON BELOW
               v
┌──────────────────────────────┐
│ Validation and state machine │
│ checksum • age • payment     │
│ duplicate • intake capacity  │
└──────────────┬───────────────┘
               v
┌──────────────────────────────┐
│ SQLite / PostgreSQL          │
│ draft • invoice • outbox     │
└──────────────┬───────────────┘
               v
┌──────────────────────────────┐
│ openpyxl + filelock          │
│ Master Invoice workbook      │
└──────────────┬───────────────┘
               v
┌──────────────────────────────┐
│ openpyxl + win32com / Excel  │
│ Official Q&M invoice PDF     │
└──────────────┬───────────────┘
               v
┌──────────────────────────────┐
│ Transactional outbox         │
│ smtplib + TLS                │
│ smtp.gmail.com:587           │
└──────────────────────────────┘
```

