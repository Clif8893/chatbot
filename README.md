# Q&M AI-Powered Enquiry & Enrollment System

This repository contains the active requirements baseline and a working
prototype for course enquiries and conversational enrollment. A React/Vite chat
client calls a Flask REST API. Flask controls the workflow, CrewAI runs the
intent classifier, the Module A enquiry agent and the Module B enrollment agent,
and SQLAlchemy preserves conversations, enrollment drafts and participant memory
across reloads and later days.

The central catalogue contains the planned integrated versions of two courses:
Basic Certificate in Dental Assisting (S$600 nett) and Certificate in Dental
Clinic Front Desk Operations (S$450 nett). Their six approved November 2026 to
February 2027 intakes are seeded idempotently into the staff-managed course-date
table. Obsolete demo rows are retained for history but retired from selection.
Automated tests use only synthetic identities.

## Current Phase

The current implementation includes:

- Intent classification shared by both modules
- Module A, a conversational enquiry assistant (see below and
  [docs/module-a](docs/module-a/requirements.md)): approved answers, sensible
  replies to anything else, memory across a participant's chats, staff
  escalation, opt-out and memory-aware follow-up reminders
- An Enrollment Agent that collects selected course(s), one intake per course,
  name, NRIC/FIN, date of birth, email, and mobile number
- Deterministic Python validation and explicit confirmation before saving
- An additive enrollment-schema migration that preserves existing records
- Participant registration, login, protected chat, and account-owned conversations
- Role-protected staff operations and finance workspaces
- Course-date management, payment review, private document delivery, and queued integrations
- SQLite locally, with PostgreSQL support through `DATABASE_URL`
- Database-backed history, browser conversation resumption, and a new-conversation action

### Module A: enquiry assistant

Every message that Module B does not own goes to Module A's `EnquiryService`:
questions, greetings, general questions, unclear messages, rude remarks and
requests for staff. Crisis messages, threats, STOP, "forget me" and requests
for a person are handled by Module A even in the middle of an enrollment. When
a participant clearly wants to enrol, Module A hands over to Module B's
existing enrollment offer.

How replies stay accurate and appropriate:

- **Approved facts only.** Q&M facts come from
  `backend/data/enquiry_knowledge_base.json`. That file holds the reply-template
  wording, with live fees and intake dates filled in from the course catalogue.
  A deterministic fact guard rejects any price, date, time, percentage or link
  that is not approved, and the participant's own figures are allowed, so
  "S$600 − S$500 = S$100 top-up" works. Unknown Q&M details get "I don't have
  confirmed information" plus a staff case.
- **Anything else.** Harmless general questions get a brief answer and a steer
  back to the course. Personal medical, legal, financial or political advice is
  declined. Rudeness is de-escalated, and a third offence or any threat closes
  the chat and alerts staff. Crisis cues receive helpline details. Attempts to
  change or reveal the assistant's instructions are declined.
- **Memory.** A whitelisted, encrypted profile (name, course and intake
  interest, payment preference, stated credit, concerns, constraints) and
  conversation summaries carry across a participant's chats. NRIC, date of
  birth, email and phone are never stored there. The first Module A reply in a
  chat discloses this; "forget me" clears it, and idle profiles are deleted
  after `RETENTION_DAYS`.
- **Follow-ups.** Reminders at `FOLLOWUP_HOURS` (default `0.5,24.5,72.5`: 30
  minutes, then 1 day and 2 days after the previous reminder, from the workflow
  deck) start from approved templates and are
  personalised from memory. They fall back to the template unchanged if
  anything fails the checks. Set `FOLLOWUP_REQUIRE_EXPLICIT_CONSENT=true` to
  send reminders only to leads with a recorded opt-in; STOP always stops them.
  WhatsApp and email enquirers are reminded on their own channel. Web chat
  reminders are posted in the conversation itself (no email or WhatsApp
  setup needed); the open chat page checks for them every minute.
- **Running the reminders.** Start the built-in scheduler alongside the API:

  ```powershell
  .\backend\.venv\Scripts\python.exe -m flask --app backend.app automation-worker --interval 300
  ```

  It runs the inbox, follow-up and delivery jobs every `--interval` seconds
  (keep it well under 30 minutes so the first reminder is on time; `--once`
  runs one pass). Web chat reminders work straight away. Until email
  (`EMAIL_DELIVERY_ENABLED` and `SMTP_*`) or WhatsApp (`WHATSAPP_*`) is
  configured it only queues those messages, because a failed send is not
  retried. An external scheduler such as cron or n8n can
  instead call `POST /api/automation/run` with
  `Authorization: Bearer <AUTOMATION_API_TOKEN>`.

Content decisions made where the source materials conflict or are silent (venue
not stated, Front Desk details deferred to staff, crisis wording, language,
memory retention) are recorded as D1–D6 in
[docs/module-a/requirements.md](docs/module-a/requirements.md) and await Q&M
sign-off. Staff edit enquiry wording as described in the
[content-update guide](docs/module-a/content-update-guide.md). The portal's
existing course/FAQ content editor still feeds the Enrollment Agent's reference
information, but not Module A's replies.

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
- Module A specification: [requirements](docs/module-a/requirements.md), [design](docs/module-a/design.md), [tasks](docs/module-a/tasks.md)
- [Module A content-update guide](docs/module-a/content-update-guide.md)
- [Module A live evaluation results](docs/module-a/evaluation-results.md)
- [Enrollment Agent implementation overview](docs/enrollment/ENROLLMENT_OVERVIEW.md)
- [Enrollment Agent development workbook](notebooks/enrollment/Enrollment_Agent_Development_Workbook.ipynb)

Older requirements material is retained under `docs/archive/` and is not the
current source of truth.

## Architecture

```text
React/Vite chat                     WhatsApp/email inbound (integrations)
      |                                        |
      v                                        v
Flask REST API --------> SQLAlchemy (SQLite or PostgreSQL)
      |
      v
Application router (ChatService)
      |----> crisis / threat / STOP / forget me / staff request ---------+
      |----> Module B pre-emption (pending actions, choices, invoices)   |
      |----> CrewAI Intent Classification Agent                          |
      |          |----> enrollment ----> CrewAI Enrollment Agent         |
      |          |                         --> deterministic validator   |
      |          `----> everything else ------------------------------+  |
      |                                                               v  v
      `--------------------------------------------- Module A EnquiryService
                 pre-checks -> memory -> CrewAI Enquiry Agent -> fact guard
                 -> staff cases, consent, memory updates, hand-off to Module B
```

`OPENAI_MODEL` sets the classifier and enrollment model. Module A uses
`ENQUIRY_MODEL`/`ENQUIRY_API_KEY` when set (for example
`anthropic/claude-sonnet-5-5`, which needs the `anthropic` package), and
otherwise the same OpenAI settings. CrewAI 1.15.21 supplies the provider
adapters.

## Folder Guide

| Path | Purpose |
|---|---|
| `frontend/` | React components, Vite configuration, styles, and REST client |
| `frontend/src/components/enrollment/` | Participant-facing enrollment status, invoice, and payment-evidence UI |
| `backend/agents/` | CrewAI agent definitions and Pydantic output contracts |
| `backend/services/` | Shared CrewAI execution, persistence, routing, privacy, finance, and catalogue services |
| `backend/services/enquiry/` | Module A: knowledge base, fact extraction and guard, message pre-checks, memory, enquiry service, follow-up composer |
| `backend/services/enrollment/` | Enrollment state machine, parsing, validation, intake allocation, and participant enrollment management |
| `backend/tools/enrollment/` | Read-only tools scoped to the Enrollment Agent runtime |
| `backend/models/` | SQLAlchemy models |
| `backend/data/` | Editable company course information, Module A's `enquiry_knowledge_base.json`, and ignored local SQLite data/backups |
| `backend/tests/` | Shared model-free automated tests plus opt-in live tests |
| `backend/tests/enquiry_tests/` | Module A tests and the evaluation set (`eval/enquiry_cases.jsonl`) |
| `backend/tests/enrollment_tests/` | Enrollment Agent, course-selection, intake, memory, management, and workflow regression tests |
| `docs/module-a/` | Module A specification, content-update guide and evaluation results |
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
# Optional Module A model; blank uses OPENAI_MODEL and OPENAI_API_KEY.
ENQUIRY_MODEL=
ENQUIRY_API_KEY=
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
npm test
npm run build
```

Module A's own suite runs on its own in a few minutes:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\enquiry_tests -q
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

### Module A live evaluation

78 scripted conversations check Module A against the configured model. They
cover FAQ accuracy, unknown facts, general questions, rudeness, threats,
distress, prompt injection, memory, hand-off and language. Each turn is scored
deterministically, and category thresholds come from the Module A success
criteria. The run is opt-in because it makes roughly 150–200 provider calls:

```powershell
$env:RUN_LIVE_ENQUIRY_EVAL="1"
.\backend\.venv\Scripts\python.exe -m pytest -m live_provider backend\tests\enquiry_tests\test_live_enquiry_evaluation.py -q -s
Remove-Item Env:RUN_LIVE_ENQUIRY_EVAL
```

Set `EVAL_REPORT_PATH` to keep the JSON report and `EVAL_CASES` to run
selected case ids. Results so far are recorded in
[docs/module-a/evaluation-results.md](docs/module-a/evaluation-results.md).
Rerun after changing the knowledge base, prompts or model.
