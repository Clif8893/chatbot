# Q&M Agentic Course Agent

This branch contains Dhiraj's working Q&M Training enquiry and enrollment prototype. A React/Vite portal calls a Flask API; CrewAI coordinates intent classification, grounded FAQ replies and enrollment extraction, while deterministic Python validates and persists enrollment state.

The repository was integrated on top of the GitLab `main` history. Credentials, participant records, runtime databases, generated invoices and private company attachments are intentionally not included.

## Implemented agents and workflow

- **Intent Classification Agent** routes natural enquiries to FAQ, enrollment or general handling.
- **FAQ/Enquiry Agent** answers from `backend/data/course_information.json` and avoids inventing dates, venues, funding rules or payment details.
- **Enrollment Agent** extracts the nine enrollment fields. Deterministic services then validate NRIC/FIN checksum, age, contact details, intake, payment allocation, duplicates and capacity.
- Conversation and draft state are persisted so signed-in participants can resume safely. Confirmed users can check status and request changes, rescheduling, cancellation or an invoice resend.
- Participant sessions own their conversations. Staff operations and accounting routes use separate role-protected sessions.

The two prototype courses are:

| Course | Fee | Schedule | Payment rules |
|---|---:|---|---|
| 2-Day Basic Certificate in Dental Assisting | S$600 nett | Two consecutive days, 9:30am-5:30pm | PayNow or basic-tier SkillsFuture plus PayNow remainder; Mid-Career SkillsFuture is excluded; UTAP is reimbursement-only |
| 1-Day Dental Infection Control & Sterilisation Workshop | S$350 nett | One day, 9:30am-5:30pm | Full PayNow only; SkillsFuture and UTAP are unavailable |

The included intake dates are fictional development data. They are enabled only by `ENABLE_DEMO_INTAKES=true`, are disabled in production, and are not approved Q&M intake dates.

## Project layout

| Path | Purpose |
|---|---|
| `backend/agents/` | Three CrewAI agent definitions and structured output models |
| `backend/tools/` | Read-only course, memory, draft, validation and intake tools |
| `backend/services/` | Routing, validation, persistence, enrollment management, invoices and delivery |
| `backend/models/` | SQLAlchemy account, conversation, enrollment, finance and operations models |
| `backend/data/course_information.json` | Structured course and approved reply information |
| `backend/tests/` | Mocked backend and regression tests |
| `frontend/src/` | Participant chat and protected staff portal |
| `docs/` | Requirements, templates and privacy-safe reference material |
| `ENROLLMENT_OVERVIEW.md` | Concise enrollment architecture and verification guide |

## Requirements

- Python 3.10 or newer (verified with Python 3.10.11)
- Node.js 20 or newer and npm (verified with Node.js 24.15.0)
- Microsoft Excel on Windows only if Excel-to-PDF invoice conversion is enabled
- An OpenAI API key for live CrewAI replies; tests mock this boundary

## Clone this branch

```powershell
git clone https://gitlab.com/nyp-sg/fyp/2026-p3/qm_agentic_course_agent.git
Set-Location qm_agentic_course_agent
git checkout feature/dhiraj-enrollment-agent
```

## Backend setup

From the project root in PowerShell:

```powershell
python -m venv backend\.venv
.\backend\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
Copy-Item backend\.env.example backend\.env
python -m flask --app backend.app run --debug --port 5000
```

Edit only the ignored `backend/.env`. At minimum, set `OPENAI_API_KEY` for live chatbot calls and replace `FLASK_SECRET_KEY` with a private value. The default SQLite URL creates `backend/data/qm_chatbot.db`; table creation and additive migrations run at application startup.

Important development settings are documented in `backend/.env.example`. Keep email disabled unless an authorised SMTP account and recipient have been configured:

```dotenv
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
DATABASE_URL=sqlite:///data/qm_chatbot.db
FLASK_SECRET_KEY=
EMAIL_DELIVERY_ENABLED=false
SMTP_USERNAME=
SMTP_PASSWORD=
EMAIL_LIVE_TEST_ENABLED=false
EMAIL_TEST_RECIPIENT=
```

No API key, Gmail app password, Flask secret or production account is supplied in Git.

## Frontend setup

In a second PowerShell terminal:

```powershell
Set-Location frontend
npm install
Copy-Item .env.example .env
npm run dev
```

Open `http://127.0.0.1:5173`. The backend runs at `http://127.0.0.1:5000`; `GET /health` is the health check.

Participants create their own local account from the registration page. There is no committed staff password. To make a local-only staff account, generate a password hash without putting the plaintext password in Git:

```powershell
$hash = python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('choose-a-local-password'))"
$account = @{ teststaff = @{ password_hash = $hash; role = 'admin' } } | ConvertTo-Json -Compress
(Get-Content backend\.env) -replace '^STAFF_ACCOUNTS_JSON=.*$', "STAFF_ACCOUNTS_JSON=$account" | Set-Content backend\.env
```

Restart the backend, then sign in at `/staff` with username `teststaff` and the local password chosen above. Use `operations`, `accounting` or `admin` as the role appropriate to the test. The ignored `.env` must never be committed.

## Tests and build

Normal backend tests mock CrewAI/OpenAI and do not make real provider calls:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests -q
Set-Location frontend
npm test
npm run build
```

Branch-preparation verification completed with **22/22 targeted backend tests** and **16/16 frontend tests** passing. The targeted backend set covers both course workflows, status/context resolution, confirmed changes, rescheduling, cancellation and invoice download. Python compilation and the Vite production build also passed. No live OpenAI or SMTP call was made.

## Invoice and email limitations

- Master Invoice workbooks and PDF invoices are generated under `backend/data/private/`; that directory is ignored.
- Excel export uses deterministic database data. PDF conversion relies on Microsoft Excel/COM on Windows.
- `INVOICE_TEMPLATE_APPROVED` defaults to `false`. Customer PDF delivery must remain blocked until Q&M approves the template wording and supplies the private source workbook.
- Invoice numbering uses the configurable `DEMO-INV` development prefix pending Q&M approval.
- Email uses a database-backed outbox. Delivery is disabled by default and requires local SMTP credentials; failures do not undo enrollment or invoice records.
- Company workflow/reply attachments used during development are not in this branch because they contain private contact or business material. Runtime operation uses the checked-in structured course-information file.

## Documentation

- [Enrollment overview](ENROLLMENT_OVERVIEW.md)
- [Active requirements](docs/requirements/Cliftons-Requirements.md)
- [Requirements template](docs/templates/requirements-template.md)
- [Design template](docs/templates/design-template.md)
- [Tasks template](docs/templates/tasks-template.md)
- [Spec-Driven Development reference](docs/source-materials/proposal/SpecDrivenDevelopment.pdf)

Source documents excluded for privacy remain only in the original local project and are not required to start the application.
