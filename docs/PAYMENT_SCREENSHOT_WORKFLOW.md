# Payment screenshot workflow

The customer can attach a PNG, JPEG or WebP screenshot (up to 5 MB) in an invoiced conversation, choose PayNow or SkillsFuture claim, and add an optional note. The file is stored encrypted. It appears in that conversation and in the authenticated **Payments** staff page. The staff page lists pending and completed reviews and exports the database records as an `.xlsx` workbook with separate submission and verified payment sheets. A generated template invoice is linked to the payment ledger when no finance invoice row exists yet.

The payment evidence task uses the existing backend `OPENAI_API_KEY` with `PAYMENT_VISION_MODEL` (default `gpt-4o-mini`). The key stays on the server. Vision extracts only visible facts. Logo detection, a bank success screen, and a SkillsFuture claim never verify settlement. Staff must reconcile the amount, payee and reference with a bank or SkillsFuture settlement record before approving. Screenshots without a visible reference, such as the supplied DBS example, remain pending until staff enter a verified reference. A mismatch with the invoice payee is flagged. The front desk course currently has no approved PayNow UEN in the catalogue; its payee is marked for staff confirmation rather than guessed.

Every upload has a pending review and does not change the balance. Staff can correct extracted fields, approve a verified amount, reject, or request clarification with a note. Approved payments reduce the remaining invoice balance. A unique screenshot evidence ID and transaction reference prevent double counting. Each decision queues at most one customer WhatsApp message and saves a copy in the chat. WhatsApp delivery requires configured Meta credentials and, outside the customer service window, an approved template; until then the outbox retains the message.

`Invoice.skillsfuture_amount` is the planned funding allocation, and `PaymentEvidence.skillsfuture_claim_amount` is text seen on a screenshot. Only an approved `Payment` row counts as settlement. Staff must check the real SkillsFuture claim and receipt of funds outside this app; the app has no settlement feed. The QR code on the invoice is a test placeholder and must be replaced before any real payment instruction.

## Run checks

From the repository root:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_payment_screenshots.py -q
cd frontend
npm run build
```

The SQLite schema sync takes a backup before changing existing payment tables. If deploying to a non-SQLite database with the old `uq_invoice_payment_method` constraint, remove that constraint and add a unique constraint on `payments.evidence_id` in your database migration before accepting partial payments.
