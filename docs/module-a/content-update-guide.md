# Updating the Enquiry Assistant's Wording

This guide is for whoever maintains what the Q&M enquiry assistant (Module A) is
allowed to say. Everything the assistant states as Q&M fact comes from one file:

`backend/data/enquiry_knowledge_base.json`

The assistant rephrases this wording to suit each question, but it cannot add
Q&M facts that are not here. A built-in check blocks any price, date, time,
percentage or web link that the file and the live course data do not contain.
Changes to this file are picked up on subsequent requests. Replies already sent
in a chat are history and are not rewritten.

## Before you start

- **Live facts are not edited here.** Course fees, durations and training hours
  come from the course catalogue. Intake dates come from the course dates staff
  manage in the staff portal. Changing a date in the portal changes what the
  assistant says straight away.
- **The portal's course/FAQ editor does not change these replies.** It still
  feeds the enrollment assistant's reference information. Edit this file for
  enquiry wording.
- **Keep a copy** of the file before editing, so you can restore it.
- **Only add wording Q&M has approved.** If sources disagree (as the venue did),
  leave the topic as "to be confirmed" rather than choosing one.

## What is in the file

### `entries`: approved answers

Each entry is one topic the assistant can answer:

```json
{
  "id": "da.mid_career",
  "course_id": "dental-assisting-basic",
  "topic": "Mid-Career SkillsFuture Credit",
  "approved_text": "There are two tiers of SkillsFuture Credit. ...",
  "source": "reply_template — Can use Midcareer Skillsfuture?",
  "confirmation_required": false
}
```

| Field | What it means |
|---|---|
| `id` | A unique name. Start with `da.` (Dental Assisting), `fd.` (Front Desk) or `general.`. Never reuse or rename an id without a developer, because tests refer to some of them. |
| `course_id` | Which course the entry is about: `dental-assisting-basic`, `dental-clinic-front-desk-operations`, or `general` for both. |
| `topic` | A short label the assistant uses to pick the right entry. Make it describe what the entry answers. |
| `approved_text` | The approved wording. Write it as you would reply on WhatsApp. |
| `source` | Where the wording came from (a document, slide or decision). Required. |
| `confirmation_required` | `true` means "not confirmed". The assistant may say only this text and nothing more on the topic, so use it for topics such as the venue. |
| `notes` | Optional notes for maintainers. Never shown to participants. |
| `lists_intakes` | `true` only on entries that list one course's dates. After such a reply, a participant answering with just a date ("18 November") is treated as interest in that intake. |

### Live values in `{braces}`

Write these exactly as shown and the app fills them in from live data:

| Placeholder | Filled with |
|---|---|
| `{course_name}` | The course's full name |
| `{course_fee}` | The fee, e.g. "S$600 nett" |
| `{duration}` | e.g. "Two consecutive training days" |
| `{training_time}` | e.g. "9:30am to 5:30pm on each training day" |
| `{intake_list}` | The course's open intake dates |
| `{website}` | The course web page |
| `{paynow_uen}` | The PayNow UEN |
| `{claim_page}` | The SkillsFuture claim page link |
| `{course_list}` | Every course with its fee and duration (in `general` entries only) |

If the catalogue has no value yet, the reply says "to be confirmed by our team"
instead of guessing. Do not type fees or dates into `approved_text`; use the
placeholders so they never go out of date.

### `fixed_replies`: set responses

Fixed replies are sent word for word in specific situations:

| Name | When it is used |
|---|---|
| `first_contact_privacy` | Added to the assistant's first reply in each chat |
| `language_note` | Added once when someone writes in Chinese, Tamil or another non-Latin script |
| `unanswered` | When the assistant cannot answer a Q&M question. Must keep `{subject}`. |
| `human_request` | When someone asks for a person |
| `crisis_support` | When a message suggests a risk of self-harm (approved under decision D3) |
| `decline_instructions` | When someone tries to make the assistant reveal or change how it works |
| `abuse_close` | After a threat, or a third rude or abusive message |
| `opt_out` | When someone replies STOP |
| `forget_me` | When someone asks to be forgotten |
| `fallback` | When the assistant cannot answer safely; staff are alerted |

You may change the wording, but every name must stay, and only `unanswered` may
use a placeholder.

### `followup_templates`: reminders

The three reminders are sent to leads who stop replying, at the intervals set by
`FOLLOWUP_HOURS`. The assistant may personalise them (for example by mentioning
the course the person asked about), but sends them unchanged if anything looks
wrong. Each template must:

- tell the participant how to reply STOP
- contain no fees, dates or links (the assistant adds approved facts when relevant)
- contain no placeholders

### `suggestions`: the buttons under replies

The assistant can only offer buttons from this menu. It never writes its own.

```json
{"id": "utap", "label": "NTUC UTAP support", "covers": ["da.utap"]}
```

| Field | What it means |
|---|---|
| `id` | A unique short name |
| `label` | The button text (40 characters or fewer). Tapping it sends this text as the participant's message, so write something specific the assistant can answer, such as "Upcoming dates", never something vague like "What else can I ask?" |
| `covers` | The entry ids the button leads to. Once any of them has been answered for the participant, in this chat or an earlier one, the button is no longer offered. Leave it empty for buttons that never run out, such as "Talk to staff". |

Two lists use these ids:

- `starter_suggestions`: the opening menu of a new chat. Topics the participant already covered are left out.
- `suggestion_order`: used when the assistant picks no suitable button.

Every id named in either list must exist in `suggestions`.

## Checking your change

The app checks this file every time it starts and refuses to start if
something is wrong. A broken edit never reaches participants. The error names
the problem, for example:

- `Duplicate entry id: da.utap`
- `Entry da.fees uses unsupported placeholders: ['discount']`
- `Missing fixed replies: ['crisis_support']`
- `Follow-up template 2 must tell the participant how to reply STOP.`

After editing, ask a developer to run the checks below, or run them yourself:

```powershell
# Quick check of the file and Module A behaviour (no AI calls, a few minutes)
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\enquiry_tests -q

# Full live check against the real AI model (uses API credits)
$env:RUN_LIVE_ENQUIRY_EVAL="1"
.\backend\.venv\Scripts\python.exe -m pytest -m live_provider backend\tests\enquiry_tests\test_live_enquiry_evaluation.py -q -s
Remove-Item Env:RUN_LIVE_ENQUIRY_EVAL
```

Run the live check after any change to wording that answers common questions.
It confirms that the assistant still gives the right facts and handles difficult
messages properly.

## Common changes

| You want to… | Do this |
|---|---|
| Correct a fact (e.g. a new UTAP limit) | Edit that entry's `approved_text` and update its `source` |
| Confirm the venue | Put the venue in `da.venue`'s `approved_text`, set `confirmation_required` to `false`, and record the decision in `requirements.md` (D1) |
| Add a new FAQ | Add an entry with a new `id`, a clear `topic` and a `source` |
| Change a fee, duration or intake date | Change it in the course catalogue or the staff portal, not here |
| Approve Front Desk course details | Add `fd.` entries for the newly approved topics and remove that topic from the "Not yet confirmed" sentence in `fd.catalogue_facts` (D4) |
| Change reminder timing | Change `FOLLOWUP_HOURS` in the server settings, not the templates |
