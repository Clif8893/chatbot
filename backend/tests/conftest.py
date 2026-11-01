"""Shared fixtures with a deterministic substitute for OpenAI/Crew execution."""

import re
from uuid import uuid4

import pytest

from backend.agents.schemas import (
    AIServiceError,
    ConversationDigest,
    EnquiryTurn,
    EnrollmentExtraction,
    EnrollmentFields,
    FollowupMessage,
    IntentClassification,
    MemoryFact,
)
from backend.app import create_app


class FakeAIService:
    def __init__(self):
        self.classify_calls = []
        self.enquiry_calls = []
        self.summary_calls = []
        self.followup_calls = []
        self.enrollment_calls = []

    def classify(
        self,
        message,
        context,
        active_intent,
        draft_status,
        pending_followup=None,
        draft_summary=None,
    ):
        self.classify_calls.append(
            {
                "message": message,
                "context": context,
                "active_intent": active_intent,
                "draft_status": draft_status,
                "pending_followup": pending_followup,
                "draft_summary": draft_summary,
            }
        )
        lowered = message.casefold()
        faq_terms = (
            "fee",
            "fees",
            "cost",
            "much",
            "duration",
            "how long",
            "location",
            "where",
            "whr",
            "venue",
            "held",
            "funding",
            "course",
            "corse",
            "class",
            "classes",
            "certificate",
            "capacity",
            "experience",
            "time",
            "timing",
            "training hours",
            "schedule",
            "date",
            "skillsfuture",
            "skillfuture",
            "skilsfuture",
            "mid-career",
            "mid career",
            "paynow",
            "pay now",
            "payment",
            "paymnt",
            "screenshot",
            "proof of payment",
            "invoice",
            "claim steps",
            "utap",
            "qualification",
            "qualific",
            "qualified",
            "job",
            "position",
            "website",
        )
        enrollment_terms = (
            "enrol",
            "enroll",
            "enrl",
            "register",
            "regster",
            "sign up",
            "sign upp",
            "apply",
        )
        is_question = "?" in message or bool(
            re.match(
                r"^(?:what|wat|when|where|whr|can|could|how|hw|is|do|does|tell)\b",
                lowered,
            )
        )
        asks_how_to_enroll = is_question and any(
            term in lowered for term in enrollment_terms
        )
        direct_enrollment = any(
            term in lowered
            for term in (
                "i want to enrol",
                "i want to enroll",
                "i wana enrl",
                "i would like to register",
                "please register me",
                "register me",
                "sign me up",
                "sign me upp",
                "ready to enrol",
                "ready to enroll",
            )
        )
        enrollment_interest = any(
            phrase in lowered
            for phrase in (
                "works for me",
                "suits me",
                "prefer that date",
                "prefer this date",
                "that date is good",
                "this date is good",
                "reserve a place",
                "hold a place",
                "seriously interested",
                "interested in joining",
                "interested in the",
            )
        )
        starts_enrollment = any(term in lowered for term in enrollment_terms)
        if any(term in lowered for term in ("continue my enrollment", "continue my enrolment", "resume my enrollment", "resume my enrolment", "can we continue", "okay, continue", "ok, continue", "let's continue", "lets continue", "i'm ready", "im ready", "continue from where we stopped", "continue where we stopped")):
            intent = "resume_workflow"
        elif any(term in lowered for term in ("continue later", "ask something else first", "pause my enrollment", "pause my enrolment", "not ready to choose", "not sure which intake")):
            intent = "pause_workflow"
        elif any(term in lowered for term in ("what information do you need", "what information do you still need", "what information have i not provided", "what information have i already provided", "what info do you need", "what do you need from me", "what other details", "anything else", "what's missing", "what s missing", "so now need what", "what details are missing", "what is left", "what else you need", "what do i need to correct", "which fields have i completed", "what details i give already", "show my enrollment progress", "show my enrolment progress", "have i completed registration", "what happens next")):
            intent = "workflow_question"
        elif any(term in lowered for term in ("suitable for beginners", "better for beginners", "which course should i", "which course is right")):
            intent = "course_comparison"
        elif any(term in lowered for term in ("tell me a joke", "weather", "football score", "capital of")):
            intent = "unsupported_general_question"
        elif ("thanks" in lowered and not lowered.strip().startswith("no thanks")) or lowered.strip(" .!?") in {
            "hi", "hello", "hey", "thanks", "thank you", "how are you",
            "how's it going", "good morning", "gud morning", "good afternoon",
            "gud afternoon", "good evening", "gud evening", "morning",
            "afternoon", "evening",
        }:
            intent = "small_talk"
        elif any(term in lowered for term in (
            "cancel", "reschedul", "change my", "change me", "change enrollment",
            "change enrolment", "update my", "discard draft", "withdraw",
            "remove me from", "changed my mind", "move to", "put me in",
            "add the other course", "add another course",
        )):
            intent = "enrollment"
        elif enrollment_interest:
            intent = "enrollment_interest"
        elif direct_enrollment or (starts_enrollment and not asks_how_to_enroll):
            intent = "enrollment"
        elif asks_how_to_enroll or (
            any(term in lowered for term in faq_terms)
            and (is_question or active_intent != "enrollment")
        ):
            intent = "faq"
        elif active_intent == "enrollment":
            intent = "enrollment"
        else:
            intent = "unclear"
        return IntentClassification(
            intent=intent,
            confidence=0.96,
            short_reason="Deterministic test classification.",
        )

    def converse_enquiry(
        self,
        *,
        message,
        conversation,
        profile,
        prior_conversations,
        approved_content,
        policy_hint="",
        retry_feedback=None,
        suggestion_menu="",
    ):
        """Answer from the matching approved entry, like a well-behaved model.

        It proposes no button ids, so the application's fallback order applies.
        """
        self.enquiry_calls.append(
            {
                "message": message,
                "conversation": conversation,
                "profile": profile,
                "prior_conversations": prior_conversations,
                "approved_content": approved_content,
                "policy_hint": policy_hint,
                "retry_feedback": retry_feedback,
                "suggestion_menu": suggestion_menu,
            }
        )
        entries = dict(
            re.findall(
                r"^\[([\w.]+)\][^\n]*\n(.*?)(?=\n\n\[|\n\n## |\Z)",
                approved_content,
                re.M | re.S,
            )
        )
        lowered = message.casefold()

        def has(*terms):
            return any(term in lowered for term in terms)

        def grounded(*entry_ids):
            return EnquiryTurn(
                reply="\n\n".join(entries[entry_id] for entry_id in entry_ids),
                response_type="grounded_answer",
                kb_entry_ids=list(entry_ids),
                memory_updates=[
                    MemoryFact(key="topic_answered", value=entry_id)
                    for entry_id in entry_ids
                ],
            )

        greetings = {
            "hi": "Hi", "hey": "Hi", "hello": "Hello", "how are you": "Hello",
            "good morning": "Good morning", "gud morning": "Good morning",
            "morning": "Good morning", "good afternoon": "Good afternoon",
            "afternoon": "Good afternoon", "good evening": "Good evening",
            "evening": "Good evening",
        }
        greeting = greetings.get(lowered.strip(" .!?"))
        if greeting:
            name = re.search(r"First name on their enrollment: (\S+)", profile)
            salutation = f"{greeting}, {name.group(1)}!" if name else f"{greeting}!"
            return EnquiryTurn(
                reply=f"{salutation} 👋 How can I help you with our courses today?",
                response_type="small_talk",
            )
        if has("complain", "complaint"):
            return EnquiryTurn(
                reply="I'm sorry to hear that. I've passed this to our team to follow up.",
                response_type="deescalation",
                escalation_category="complaint",
            )
        if has("thank"):
            return EnquiryTurn(reply="You're welcome! 😊", response_type="small_talk")
        if has("capital of", "weather", "joke", "football"):
            return EnquiryTurn(
                reply=(
                    "That's a general question, so I'll keep it brief. Is there "
                    "anything about our courses I can help with?"
                ),
                response_type="general_knowledge",
            )
        if has("parking"):
            return EnquiryTurn(
                reply=(
                    "I don't have confirmed information about parking at the "
                    "training venue. I've asked our team to check."
                ),
                response_type="no_confirmed_info",
                unanswered_subject="parking at the training venue",
                escalation_category="unanswered_question",
            )
        enrolling = re.search(r"Course they are enrolling in: (.+?) \(", profile)
        if enrolling and not has("front desk", "assisting", "both", "all courses"):
            # "The course" means the course the participant is enrolling in.
            lowered = f"{lowered} {enrolling.group(1).casefold()}"
        if has("front desk"):
            if has("fee", "cost", "much", "price", "date", "intake", "when", "long", "time", "hour"):
                return grounded("fd.catalogue_facts")
            return EnquiryTurn(
                reply=(
                    "I don't have confirmed details yet about that for the front desk "
                    "course. I can ask our team to confirm them for you."
                ),
                response_type="no_confirmed_info",
                kb_entry_ids=["fd.catalogue_facts"],
                unanswered_subject="the front desk course details",
            )
        names_a_course = has("assisting", "dental assistant", "the da ", "da course")
        if not names_a_course:
            # A question about "the course" without naming one covers every course.
            if has("fee", "cost", "much", "price") and not has("expensive", "overpriced"):
                return grounded("general.courses_offered")
            if has("date", "intake", "when", "schedule", "next class"):
                return grounded("da.intake_dates", "fd.catalogue_facts")
            if has("tell me about the course", "about the courses", "what courses"):
                return grounded("general.courses_offered", "da.introduction")
        rules = (
            (("expensive", "overpriced", "price so high"), "da.fee_concern"),
            (("mid-career", "mid career", "midcareer"), "da.mid_career"),
            (("balance",), "da.skillsfuture_balance"),
            (("skillsfuture", "skillfuture", "skilsfuture", "funding", "sfc"), "da.payment_modes"),
            (("utap", "union"), "da.utap"),
            (("paynow", "pay now", "uen"), "da.paynow"),
            (("payment", "paymnt", "pay"), "da.payment_modes"),
            (("fee", "cost", "much", "price", "duration", "how long", "hours", "time"), "da.fee_and_timing"),
            (("venue", "where", "whr", "location"), "da.venue"),
            (("date", "intake", "when", "schedule", "next class"), "da.intake_dates"),
            (("qualification", "qualific", "requirement", "experience", "beginner", "suitable"), "da.minimum_qualification"),
            (("job", "position", "work", "career"), "da.job_opportunities"),
            (("enrol", "enroll", "register", "sign up", "apply"), "da.enrollment_requirements"),
            (("course", "corse", "class", "about", "info", "certificate", "website"), "da.introduction"),
        )
        for terms, entry_id in rules:
            if has(*terms):
                return grounded(entry_id)
        return EnquiryTurn(
            reply="Sorry, could you tell me a bit more about what you'd like to know?",
            response_type="clarifying_question",
        )

    def compose_followup(self, *, template, profile, conversation, approved_content, final):
        self.followup_calls.append({"template": template, "profile": profile, "final": final})
        return FollowupMessage(message=template)

    def summarise_conversation(self, previous_summary, transcript):
        self.summary_calls.append(transcript)
        return ConversationDigest(summary="The participant asked about the courses.")

    def extract_enrollment(self, message, context, draft_state, tools=None):
        self.enrollment_calls.append(
            {
                "message": message,
                "context": context,
                "draft_state": draft_state,
                "tools": list(tools or []),
            }
        )
        lowered = message.casefold().strip()
        fields = {}

        name_match = re.search(
            r"(?:full\s+name|name)\s*(?:is|:)?\s*([A-Za-z][A-Za-z '\-]+?)(?=\s*(?:;|,|\band\b|\r?\n|$))",
            message,
            re.IGNORECASE,
        )
        if name_match:
            fields["full_name"] = name_match.group(1).strip()
        else:
            leading_name = re.match(
                r"\s*([A-Za-z][A-Za-z '\-]+?),\s*[STFGM]\d{7}[A-Z]\b",
                message,
                re.IGNORECASE,
            )
            if leading_name:
                fields["full_name"] = leading_name.group(1).strip()

        nric_match = re.search(r"\b[STFGM]\d{7}[A-Z]\b", message, re.IGNORECASE)
        if nric_match:
            fields["nric"] = nric_match.group(0)
        elif "nric" in lowered:
            fields["nric"] = message.split()[-1].strip(".,;")

        email_match = re.search(r"\b[^\s,;@]+@[^\s,;@]+\.[^\s,;@]+\b", message)
        if email_match:
            fields["email"] = email_match.group(0)
        elif "email" in lowered:
            labelled_email = re.search(
                r"email(?:\s+address)?\s*(?:is|:)?\s*([^,;\r\n]+)",
                message,
                re.IGNORECASE,
            )
            if labelled_email:
                fields["email"] = labelled_email.group(1).strip()

        dob_match = re.search(
            r"(?:dob|date of birth|born)\s*(?:is|:)?\s*([^,;\r\n]+)",
            message,
            re.IGNORECASE,
        )
        if dob_match:
            fields["date_of_birth"] = dob_match.group(1).strip()

        if "next available intake" in lowered:
            fields["preferred_intake_date"] = "next available intake"
        else:
            intake_match = re.search(
                r"(?:preferred\s+intake|intake|preferred date)\s*(?:is|:)?\s*([^,;\r\n]+)",
                message,
                re.IGNORECASE,
            )
            if intake_match:
                fields["preferred_intake_date"] = intake_match.group(1).strip()

        mobile_match = re.search(
            r"(?:mobile|phone|hp)(?:\s+number)?\s*(?:is|:)?\s*(\+?65[\s-]?)?([89]\d{3}[\s-]?\d{4})",
            message,
            re.IGNORECASE,
        )
        if mobile_match:
            fields["mobile_number"] = "".join(
                part for part in mobile_match.groups() if part
            )
        else:
            labelled_mobile = re.search(
                r"(?:mobile|phone|hp)(?:\s+number)?\s*(?:is|:)?\s*([^,;\r\n]+)",
                message,
                re.IGNORECASE,
            )
            bare_mobile = re.search(r"(?<!\d)([89]\d{7})(?!\d)", message)
            if labelled_mobile:
                fields["mobile_number"] = labelled_mobile.group(1).strip()
            elif bare_mobile:
                fields["mobile_number"] = bare_mobile.group(1)

        if "basic certificate" in lowered and "dental assisting" in lowered:
            fields["course"] = "Basic Certificate in Dental Assisting"
        elif "front desk" in lowered:
            fields["course"] = "Certificate in Dental Clinic Front Desk Operations"

        confirmation = (
            "confirm"
            if lowered in {"yes", "yes, confirm", "confirm", "correct", "all correct"}
            else "reject"
            if lowered in {"no", "not correct"}
            else "none"
        )
        return EnrollmentExtraction(
            extracted_fields=EnrollmentFields(**fields),
            confirmation=confirmation,
            correction_requested=any(
                term in lowered for term in ("change", "correct", "update")
            ),
        )


class FailingAIService(FakeAIService):
    def classify(self, *args, **kwargs):
        raise AIServiceError("provider detail that must not reach the response")


@pytest.fixture()
def fake_ai():
    return FakeAIService()


@pytest.fixture()
def app(tmp_path, fake_ai):
    database_path = (tmp_path / "test.db").as_posix()
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only-secret",
            "OPENAI_API_KEY": "",
            "OPENAI_MODEL": "gpt-5-mini",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path}",
        },
        ai_service=fake_ai,
    )


@pytest.fixture()
def client(app):
    return authenticated_client(app)


@pytest.fixture()
def conversation_id(client):
    return client.post("/api/conversations").get_json()["conversation_id"]


def send(client, conversation_id, message):
    return client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": message},
    )


def authenticated_client(app, email=None, password="test-password-123"):
    """Return a participant client whose mutating requests include its CSRF token."""
    client = app.test_client()
    address = email or f"participant-{uuid4().hex}@example.com"
    response = client.post(
        "/api/auth/register",
        json={
            "full_name": "Test Participant",
            "email": address,
            "password": password,
        },
    )
    assert response.status_code == 201
    csrf_token = response.get_json()["csrf_token"]
    original_open = client.open

    def open_with_csrf(*args, **kwargs):
        method = str(kwargs.get("method", "GET")).upper()
        if method not in {"GET", "HEAD", "OPTIONS"}:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRF-Token", csrf_token)
            kwargs["headers"] = headers
        return original_open(*args, **kwargs)

    client.open = open_with_csrf
    client.test_email = address
    client.test_password = password
    client.test_csrf_token = csrf_token
    return client
