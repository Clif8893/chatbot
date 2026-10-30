"""Module A lead nurturing: quick-reply menu, opt-out and staff hand-off.

Participants write in natural language; there are no slash commands. The phrases
handled here are deterministic and rendered from staff-approved catalogue content,
so a menu choice, opt-out or hand-off never depends on the language model.
"""

import re
from dataclasses import dataclass

from backend.services.privacy import mask_nrics_in_text


WELCOME_MENU_FOLLOWUP = "welcome_menu"
ENROLLMENT_REQUEST = "I would like to enrol"

_STOP_WORDS = {
    "stop",
    "stop reminder",
    "stop reminders",
    "stop messages",
    "stop all",
    "unsubscribe",
    "opt out",
    "optout",
}
# Matched against the normalised message (letters only, so "don't" becomes "don t").
_OPT_OUT = re.compile(
    r"\b(?:stop|no more|don ?t send|do not send)\b(?: \w+){0,4} "
    r"(?:reminders?|messages?|follow ?ups?|texts?)\b"
    r"|\bstop (?:messaging|texting|contacting|reminding) me\b"
    r"|\b(?:don ?t|do not) (?:message|text|contact|remind) me\b"
    r"|\bunsubscribe\b"
)
_MENU_WORDS = {
    "menu",
    "main menu",
    "options",
    "help",
    "start",
    "what can you do",
    "what can you help with",
    "show options",
    "show me the options",
}
_HUMAN_REQUEST = re.compile(
    r"\b(?:talk|speak|chat)\s+(?:to|with)\s+(?:a\s+|an\s+|your\s+|the\s+|some\s*one\s+(?:from\s+)?)?"
    r"(?:human|person|real person|staff|someone|agent|representative|team|admin|customer service)\b"
    r"|^(?:human|real person|live agent|customer service)(?:\s+(?:please|pls))?[.!?]*$"
    r"|\bcan (?:someone|a staff|your staff|your team) (?:call|contact) me\b"
    r"|\bplease (?:call|contact) me\b",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"\b(?:complain|complaint|complaining|refund|refundable|lawyer|legal action|scam|"
    r"cheated|harass\w*|discriminat\w*|dissatisfied|very unhappy|report you)\b",
    re.IGNORECASE,
)
_GENERIC_INFO = re.compile(
    r"\b(?:more info|more information|more details|info on this|details on this|"
    r"interested|tell me more|find out more|know more)\b",
    re.IGNORECASE,
)
_SPECIFIC_TOPIC = re.compile(
    r"\b(?:fee|fees|cost|price|how much|skills\s*future|sfc|utap|union|paynow|pay|payment|"
    r"date|dates|intake|when|schedule|job|work|requirement|qualification|venue|where|"
    r"enrol|enroll|register|sign up)\b",
    re.IGNORECASE,
)


@dataclass
class LeadRouting:
    """Outcome of Module A pre-routing for one participant message."""

    reply: str | None = None
    rewrite: str | None = None
    intent: str = "faq"
    agent_name: str = "lead_nurturing"
    followup: str | None = None


class LeadNurturing:
    def __init__(self, catalogue, operations=None, channel="web"):
        self.catalogue = catalogue
        self.operations = operations
        self.channel = channel

    @property
    def content(self):
        return self.catalogue.lead_nurturing

    # Public entry point -------------------------------------------------

    def route(
        self,
        conversation,
        text,
        *,
        first_contact,
        greeting=None,
        context_course=None,
        allow_welcome=True,
    ):
        message = str(text or "").strip()
        normalized = _normalize(message)

        if normalized in _STOP_WORDS or _OPT_OUT.search(normalized):
            return self._opt_out(conversation)
        if normalized in _MENU_WORDS:
            return self._welcome()
        menu_choice = self._menu_choice(conversation, message, context_course)
        if menu_choice is not None:
            return menu_choice
        if _HUMAN_REQUEST.search(message):
            return self._handoff(conversation, message, "human_request")
        if _SENSITIVE.search(message):
            return self._handoff(conversation, message, "sensitive", _sensitive_subject(message))
        if allow_welcome and first_contact and greeting:
            return self._welcome(greeting)
        if (
            allow_welcome
            and first_contact
            and _GENERIC_INFO.search(message)
            and not _SPECIFIC_TOPIC.search(message)
        ):
            # "More info on the front desk course" describes only that course.
            intro = self.catalogue.approved_faq_reply(
                ["course description"], course=self.catalogue.focus_course(message)
            )
            return LeadRouting(
                reply=f"{intro}\n\n{self.menu_text()}",
                followup=WELCOME_MENU_FOLLOWUP,
            )
        return None

    def welcome_reply(self, greeting=None):
        """First-contact introduction used when the participant's purpose is unclear."""
        return self._welcome(greeting)

    def menu_text(self):
        options = self.content.get("menu_options", [])
        lines = ["You can reply with a number, or ask in your own words:"]
        lines.extend(f"{item['key']}. {item['label']}" for item in options)
        footer = self.content.get("menu_footer")
        if footer:
            lines.extend(["", footer])
        return "\n".join(lines)

    def first_contact_notice(self):
        """Reminder disclosure for channels that receive automatic follow-ups."""
        notice = self.content.get("opt_out_notice")
        if self.channel in {"whatsapp", "email"} and notice:
            return f"\n\n{notice}"
        return ""

    def mark_interested(self, conversation):
        """Qualify a lead after a genuine interest signal without collecting PII."""
        if self.operations is None:
            return None
        lead = self.operations.lead_for(conversation.id)
        if lead.status not in {"enrolled", "declined", "escalated"}:
            lead.status = "qualified"
        return lead

    def escalate_unanswered(self, conversation, message):
        """Flag a question that approved content cannot answer; return the note to append."""
        if not self._escalate(conversation, message, "unanswered_question"):
            return ""
        note = self.content.get("unanswered_handoff_note")
        return f"\n\n{note}" if note else ""

    # Behaviours -----------------------------------------------------------

    def _welcome(self, greeting=None):
        # Mirror the participant's own salutation, e.g. "Good morning! 👋".
        intro = self.content.get("welcome", "How can I help you today?")
        return LeadRouting(
            reply=f"{greeting or 'Hi'}! 👋 {intro}\n\n{self.menu_text()}",
            intent="unclear",
            followup=WELCOME_MENU_FOLLOWUP,
        )

    def menu_option_topics(self, key):
        option = next(
            (item for item in self.content.get("menu_options", []) if str(item.get("key")) == str(key)),
            {},
        )
        return option.get("faq_topics", [])

    def _menu_choice(self, conversation, message, context_course=None):
        if (
            conversation.pending_followup != WELCOME_MENU_FOLLOWUP
            or conversation.pending_action is not None
        ):
            return None
        match = re.fullmatch(r"\s*([1-9])\s*[.)]?\s*", message)
        if not match:
            return None
        option = next(
            (
                item
                for item in self.content.get("menu_options", [])
                if str(item.get("key")) == match.group(1)
            ),
            None,
        )
        if option is None:
            return None
        conversation.pending_followup = None
        if option.get("action") == "enrol":
            return LeadRouting(rewrite=ENROLLMENT_REQUEST)
        if option.get("action") == "staff":
            return self._handoff(conversation, "Requested a member of staff from the menu.", "human_request")
        if option.get("ask_course") and context_course is None:
            # The answer differs by course and none has been discussed yet.
            return LeadRouting(
                reply=self.catalogue.course_choice_prompt(
                    "Which course would you like this information for?"
                ),
                followup=f"course_choice:menu{option['key']}",
            )
        # Menu answers follow the course the conversation is about, if any.
        reply = self.catalogue.approved_faq_reply(
            option.get("faq_topics", []), course=context_course
        )
        return LeadRouting(
            reply=f"{reply}\n\nYou can choose another menu number, or ask in your own words.",
            followup=WELCOME_MENU_FOLLOWUP,
        )

    def _opt_out(self, conversation):
        if self.operations is not None:
            self.operations.stop_followups(conversation.id, "declined")
        conversation.pending_followup = None
        return LeadRouting(
            reply=self.content.get(
                "opt_out_reply", "You won’t receive any more reminders from us."
            ),
            intent="unclear",
        )

    def _handoff(self, conversation, message, category, subject=None):
        if not self._escalate(conversation, message, category):
            return LeadRouting(reply=self.catalogue.staff_confirmation_message)
        template = self.content.get("sensitive_handoff_reply")
        if subject and template:
            # Name the matter being handed over, e.g. "refunds and cancellations".
            return LeadRouting(reply=template.format(subject=subject), intent="unclear")
        return LeadRouting(
            reply=self.content.get(
                "human_handoff_reply",
                "I’ve passed your message to our team, and a staff member will get back to you.",
            ),
            intent="unclear",
        )

    def _escalate(self, conversation, message, category):
        if self.operations is None:
            return False
        reason = mask_nrics_in_text(str(message or ""))[:1000] or category
        self.operations.escalate(conversation.id, reason, category)
        return True


def _sensitive_subject(message):
    """What a sensitive message is about, to frame the hand-off reply."""
    lowered = str(message or "").casefold()
    if re.search(r"\brefund", lowered):
        return "refunds and cancellations"
    if re.search(r"\b(?:lawyer|legal action)\b", lowered):
        return "this legal matter"
    if re.search(r"\b(?:complain\w*|dissatisfied|very unhappy|report you)\b", lowered):
        return "your feedback"
    return "this concern"


def _normalize(message):
    return " ".join(re.sub(r"[^a-z\s]", " ", str(message or "").casefold()).split())


__all__ = ["LeadNurturing", "LeadRouting", "WELCOME_MENU_FOLLOWUP"]
