"""Shared fixtures with a deterministic substitute for OpenAI/Crew execution."""

import json
import re
from uuid import uuid4

import pytest

from backend.agents.schemas import (
    AIServiceError,
    EnrollmentExtraction,
    EnrollmentFields,
    FAQAnswer,
    IntentClassification,
)
from backend.app import create_app


class FakeAIService:
    def __init__(self):
        self.classify_calls = []
        self.faq_calls = []
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

    def answer_faq(
        self, message, context, catalogue_context, customer_first_name=None
    ):
        self.faq_calls.append(
            {
                "message": message,
                "context": context,
                "catalogue_context": catalogue_context,
                "customer_first_name": customer_first_name,
            }
        )
        catalogue = json.loads(catalogue_context)
        course = catalogue["course"]
        lowered = message.casefold()
        faq_answers = {item["topic"]: item["answer"] for item in catalogue["faqs"]}
        topics = []
        asks_fee = any(term in lowered for term in ("fee", "fees", "cost", "much", "expensive"))
        asks_duration = any(term in lowered for term in ("duration", "how long", "time"))
        raises_fee_concern = any(
            term in lowered
            for term in ("expensive", "overpriced", "price so high", "cost so much")
        )
        if raises_fee_concern:
            topics = ["course fee concern"]
            answer = faq_answers["course fee concern"]
        elif "mid-career" in lowered or "mid career" in lowered:
            topics = ["Mid-Career SkillsFuture"]
            answer = faq_answers["Mid-Career SkillsFuture"]
        elif any(term in lowered for term in ("skillsfuture", "skillfuture", "skilsfuture", "funding")):
            topics = ["SkillsFuture"]
            answer = faq_answers["SkillsFuture"]
        elif "utap" in lowered or "union" in lowered:
            topics = ["UTAP"]
            answer = faq_answers["UTAP"]
        elif "paynow" in lowered or "pay now" in lowered or "uen" in lowered:
            topics = ["PayNow"]
            answer = faq_answers["PayNow"]
        elif any(term in lowered for term in ("payment option", "payment mode", "paymnt")):
            topics = ["payment options"]
            answer = faq_answers["payment options"]
        elif asks_fee and asks_duration:
            topics = ["course fee and value", "duration and timing"]
            answer = faq_answers["course fee and value"] + " " + faq_answers["duration and timing"]
        elif asks_fee:
            topics = ["course fee and value"]
            answer = faq_answers["course fee and value"]
        elif asks_duration:
            topics = ["duration and timing"]
            answer = faq_answers["duration and timing"]
        elif "location" in lowered or "where" in lowered or "venue" in lowered:
            topics = ["venue"]
            answer = faq_answers["venue"]
        elif any(
            term in lowered
            for term in ("date", "intake", "when", "next class", "next course")
        ):
            topics = ["course dates"]
            answer = course["no_intakes_message"]
        elif "qualification" in lowered or "qualified" in lowered or "requirement" in lowered:
            topics = ["minimum qualification"]
            answer = faq_answers["minimum qualification"]
        elif "job" in lowered or "position" in lowered or "work" in lowered:
            topics = ["job opportunities"]
            answer = faq_answers["job opportunities"]
        elif "website" in lowered or "more details" in lowered:
            topics = ["course website"]
            answer = faq_answers["course website"]
        elif "invoice" in lowered or "claim steps" in lowered:
            topics = ["invoice and claim guidance"]
            answer = faq_answers["invoice and claim guidance"]
        elif "screenshot" in lowered or "proof of payment" in lowered:
            topics = ["payment screenshot channel"]
            answer = faq_answers["payment screenshot channel"]
        elif "enrol" in lowered or "enroll" in lowered or "register" in lowered:
            topics = ["enrolment process"]
            answer = faq_answers["enrolment process"]
        elif "parking" in lowered:
            answer = catalogue["reply_style"]["staff_confirmation_message"]
            return FAQAnswer(answer=answer, matched_topics=[], unanswered_subject="parking at the training venue")
        else:
            topics = ["course description"]
            answer = faq_answers["course description"]
        if customer_first_name:
            answer = f"Hi {customer_first_name}. {answer}"
        return FAQAnswer(answer=answer, matched_topics=topics)

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
