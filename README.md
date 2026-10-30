# Q&M AI-Powered Enquiry & Enrollment System

This repository contains the active requirements baseline and a working
prototype for course enquiries and conversational enrollment. A React/Vite chat
client calls a Flask REST API. Flask controls the workflow, CrewAI coordinates
three OpenAI-backed agents, and SQLAlchemy preserves conversations and enrollment
drafts across reloads and later days.

The central catalogue contains the planned integrated versions of two courses:
Basic Certificate in Dental Assisting (S$600 nett) and Certificate in Dental
Clinic Front Desk Operations (S$450 nett). Their six approved November 2026 to
February 2027 intakes are seeded idempotently into the staff-managed course-date
table. Obsolete demo rows are retained for history but retired from selection.
Automated tests use only synthetic identities.

## Current Phase

The current implementation includes:

- Intent classification into `faq`, `enrollment`, or `unclear`
- Module A lead nurturing in natural language (no slash commands): a numbered
  quick-reply menu on first contact or when the participant asks for "help" or
  the "menu", opt-out by STOP or phrases such as "please stop sending me
  reminders", staff escalation for requests to talk to someone, sensitive
  messages and questions approved content cannot answer, and staff-editable
  3-step lead follow-up templates
- A grounded FAQ/Enquiry Agent using editable company course information
- An Enrollment Agent that collects selected course(s), one intake per course,
  name, NRIC/FIN, date of birth, email, and mobile number
- Deterministic Python validation and explicit confirmation before saving
- An additive enrollment-schema migration that preserves existing records
- Participant registration, login, protected chat, and account-owned conversations
- Role-protected staff operations and finance workspaces
- Course-date management, payment review, private document delivery, and queued integrations
- SQLite locally, with PostgreSQL support through `DATABASE_URL`
- Database-backed history, browser conversation resumption, and a new-conversation action

FAQ replies follow the course the participant means. A question naming one
course by name, short name, list position ("the 2nd course"), duration ("the
1-day course") or fee is answered for that course only. Follow-up questions,
quick-action buttons, the numbered menu and "I'd like to enrol" stay on the
course the conversation is about. "Your course", "courses", "both" or "all", or
a fresh conversation with no course discussed, is answered for both courses. Each FAQ
in `course_information.json` has a general `answer` plus optional per-course
`course_answers`, which staff can edit in the portal.

FAQ answers for the Basic Certificate in Dental Assisting use the wording of
`2 days DA course Reply Template.docx` verbatim (job opportunities, minimum
qualification, Mid-Career SkillsFuture, "why is your course so expensive",
modes of payment, UTAP and the introduction). Replies are tailored only where
the question needs it: a fee or timing question gets the matching template
sentence, the introduction lists current intakes instead of the template's
historical dates and omits the unconfirmed venue, and generic funding or payment
answers add one line noting that Front Desk funding is not yet confirmed. The Front Desk Operations
course has no supplied funding or payment source, so those answers still defer
to staff. Its course description (`overview`) was drafted for the prototype and
must be approved by Q&M before production use. Lead-nurturing wording lives in the `lead_nurturing` block of
`backend/data/course_information.json`.

Leads who message Q&M first by WhatsApp or email are told in the first reply
that up to three reminders may follow and that STOP opts out. Set
`FOLLOWUP_REQUIRE_EXPLICIT_CONSENT=true` to send reminders only to leads with a
recorded opt-in; a recorded withdrawal always blocks them. `FOLLOWUP_HOURS`
defaults to `48,96,144` (REQ-03); the proposal's sequence is `24,72,168`.

External email, WhatsApp and payment-vision actions remain disabled until their
server-side credentials are configured. Payment evidence always requires staff
review unless an approved verification policy is explicitly enabled.

## Main Documentation

- [Active Clifton's requirements](docs/requirements/Cliftons-Requirements.md)
- [Original project proposal](docs/source-materials/proposal/QM_AI_Powered_Enquiry_and_Enrollment_System_V2_4.pdf)
- [Spec-Driven Development PDF](docs/source-materials/proposal/SpecDrivenDevelopment.pdf)
- [Company workflow presentation](docs/source-materials/proposal/Agentive%20Workflow%20for%20Dental%20Assisting%20Course%20Administration.pptx)
- [Company reply information](docs/source-materials/proposal/2%20days%20DA%20course%20Reply%20Template.docx)
- Company workflow email dated 5 August 2026 (referenced by the requirements; not stored in this repository)
- [Requirements template](docs/templates/requirements-template.md)
- [Design template](docs/templates/design-template.md)
- [Tasks template](docs/templates/tasks-template.md)
- [Enrollment Agent implementation overview](docs/enrollment/ENROLLMENT_OVERVIEW.md)
- [Enrollment Agent development workbook](notebooks/enrollment/Enrollment_Agent_Development_Workbook.ipynb)

Older requirements material is retained under `docs/archive/` and is not the
current source of truth.

## Architecture

```text
React/Vite chat
      |
      v
Flask REST API ----> SQLAlchemy (SQLite or PostgreSQL)
      |
      v
Application router
      |----> CrewAI Intent Classification Agent
      |----> CrewAI FAQ/Enquiry Agent ----> company course information
      `----> CrewAI Enrollment Agent ----> deterministic validator/state machine
```

The OpenAI model is configured through `OPENAI_MODEL`, allowing it to be changed
without modifying application code. CrewAI 1.15.21 supplies the OpenAI provider
adapter used by all three agents.

## Folder Guide

| Path | Purpose |
|---|---|
| `frontend/` | React components, Vite configuration, styles, and REST client |
| `frontend/src/components/enrollment/` | Participant-facing enrollment status, invoice, and payment-evidence UI |
| `backend/agents/` | CrewAI agent definitions and Pydantic output contracts |
| `backend/services/` | Shared CrewAI execution, persistence, routing, privacy, finance, and catalogue services |
| `backend/services/enrollment/` | Enrollment state machine, parsing, validation, intake allocation, and participant enrollment management |
| `backend/tools/enrollment/` | Read-only tools scoped to the Enrollment Agent runtime |
| `backend/models/` | SQLAlchemy models |
| `backend/data/` | Editable company course information and ignored local SQLite data/backups |
| `backend/tests/` | Shared model-free automated tests plus one opt-in live smoke test |
| `backend/tests/enrollment_tests/` | Enrollment Agent, course-selection, intake, memory, management, and workflow regression tests |
| `docs/enrollment/` | Enrollment Agent implementation and integration documentation |
| `notebooks/enrollment/` | Offline Enrollment Agent development evidence |
| `docs/` | Current project documentation and archived drafts |

## Backend Setup

From the project root in PowerShell:

```powershell
Set-Location backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
Set-Location ..
python -m flask --app backend.app run --debug --port 5000
```

Fill `OPENAI_API_KEY` only in `backend/.env`. Never put it in a frontend
variable or commit it. Set a private `FLASK_SECRET_KEY` and
`DATA_ENCRYPTION_KEY` outside development. Staff accounts are supplied as JSON
containing Werkzeug password hashes; plaintext staff passwords do not belong in
the file or repository. Optional backend settings are documented in
`backend/.env.example`.

```dotenv
OPENAI_MODEL=
DATABASE_URL=
CONVERSATION_CONTEXT_MESSAGE_LIMIT=20
FLASK_SECRET_KEY=change-me
FLASK_PORT=5000
ENABLE_DEMO_INTAKES=true
MINIMUM_ENROLLMENT_AGE=16
MAXIMUM_ENROLLMENT_AGE=100
ENROLLMENT_TIMEZONE=Asia/Singapore
```

`ENABLE_DEMO_INTAKES` remains available for archived compatibility fixtures, but
the current six planned intakes are approved catalogue rows and do not depend on
that flag. The seed process preserves participant counts and configured capacity.

The configured age range is deterministic development validation because the
supplied company documents do not state a minimum enrollment age. Q&M must
confirm the minimum before production use; it is not presented as course policy
in FAQ replies.

An empty `DATABASE_URL` uses `backend/data/qm_chatbot.db`. Production may use a
PostgreSQL URL such as `postgresql://...`; credentials belong only in the ignored
local/hosting environment.

## Frontend Setup

In a second terminal:

```powershell
Set-Location frontend
npm install
Copy-Item .env.example .env
npm run dev
```

Open `http://127.0.0.1:5173`. In local development, Vite forwards `/api` to the Flask server on port 5000, so the browser uses one origin. For a separate deployed API, set:

```dotenv
VITE_API_BASE_URL=https://your-api-host.example
```

Participants register or sign in at `/`. Staff use `/staff`.

## API Summary

- `POST /api/auth/register`, `/api/auth/login`, and `/api/auth/logout` manage participant sessions.
- `POST /api/conversations` creates a conversation owned by the signed-in participant.
- `GET /api/conversations/<conversation_id>/messages` reloads its masked history.
- `POST /api/chat` accepts `{"conversation_id": "...", "message": "..."}`.
- `/api/staff/*` routes require a configured staff account and enforce role boundaries.
- `GET /health` reports process health without returning configuration or secrets.

The chat response includes `conversation_id`, an assistant `message`, development
`routing` metadata, and non-sensitive enrollment state. NRIC/FIN values are masked
in API-rendered history and confirmation summaries.

Confirmed enrollments are rebuilt into the private
`backend/data/private/Master_Invoice_List.xlsx` export. The directory and all
generated workbooks are Git-ignored and downloads require an accounting/admin
staff session. The `DEMO-INV-YYYY-000001` numbering format is a configurable
development format pending Q&M confirmation; set `MASTER_INVOICE_NUMBER_PREFIX`
in the local environment when an approved prefix is supplied.

Accounting staff can download, regenerate, or retry failed exports from the
Accounts portal. To recover the workbook entirely from validated SQL records:

```powershell
Set-Location "C:\qm_agentic_course_agent_dhiraj"
$env:FLASK_APP="backend.app:app"
python -m flask rebuild-master-invoice
```

This recovery command preserves persisted invoice numbers, creates one row per
eligible enrollment, and does not modify enrollment records.

After the Master Invoice export assigns a number, the invoice-template service
can create the participant PDF from the `Invoice` worksheet attached to the
forwarded company email. Runtime copies and PDFs stay under the Git-ignored
`backend/data/private/invoices/` directory. Accountant/admin staff can view the
generation status, retry or regenerate, and download the PDF; this Step 5 flow
also queues one idempotent invoice email when a newly confirmed enrollment has
a successfully generated PDF. Email failure does not change the enrollment,
intake reservation, invoice number, PDF, or download option. Participants can
view masked delivery status and request a resend; accountant/admin staff can
retry or resend the same invoice.

Copy the blank email settings from `backend/.env.example` into the ignored local
`backend/.env`. Use a Google app password in `SMTP_PASSWORD`, never the normal
Gmail password. Process pending invoice-email jobs with:

```powershell
Set-Location "C:\qm_agentic_course_agent_dhiraj"
.\backend\.venv\Scripts\python.exe -m flask --app backend.app process-invoice-emails
```

The controlled one-message development test remains off by default. After an
authorised test address and app password are configured, explicitly enable it
and run:

```powershell
.\backend\.venv\Scripts\python.exe -m flask --app backend.app send-test-invoice-email
```

Keep `INVOICE_TEMPLATE_APPROVED=false` until Q&M confirms that the workbook's
embedded three-week payment deadline, bank/payment wording, and no-refund terms
are approved for customer use. While false, a confirmed enrollment is recorded
as `needs_review` and no customer PDF is produced. The configurable
`DEMO-INV-YYYY-000001` fallback remains a demonstration rule, not an approved
production invoice-numbering policy.

## Verification

Normal tests mock the AI boundary, so they are deterministic and incur no API use:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests -q
Set-Location frontend
npm run build
```

### Offline Enrollment Agent workbook

`notebooks/enrollment/Enrollment_Agent_Development_Workbook.ipynb` follows the course's
single-agent development sequence using the production Enrollment Agent and its
read-only course-information tool. It uses a scripted `FakeLLM`, blocks external
network access, contains only fictional inputs, and does not start Flask or open
the application database. Its transferable regression coverage is in
`backend/tests/enrollment_tests/test_enrollment_agent_isolated.py`:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\enrollment_tests\test_enrollment_agent_isolated.py -q
```

The live-provider smoke test is skipped unless explicitly enabled:

```powershell
$env:RUN_REAL_OPENAI_TEST="1"
.\backend\.venv\Scripts\python.exe -m pytest -m live_provider backend\tests\test_real_openai.py -q
Remove-Item Env:RUN_REAL_OPENAI_TEST
```

That opt-in command uses the backend API key and may incur provider usage. Do not
run it in routine CI.
