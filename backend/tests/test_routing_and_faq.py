"""Intent routing and grounded FAQ behaviour for the approved catalogue."""

from decimal import Decimal

from backend.agents.schemas import FAQAnswer
from backend.app import create_app
from backend.services.chat_service import clean_customer_copy
from backend.services.course_catalogue import CourseCatalogue
from backend.tests.conftest import FakeAIService, authenticated_client, send


def test_approved_catalogue_values_and_intakes_are_loaded():
    catalogue = CourseCatalogue()
    assert catalogue.course_name == "Basic Certificate in Dental Assisting"
    assert catalogue.course_fee == Decimal("600")
    assert [(course["name"], course["fee"]["amount"], course["duration"]) for course in catalogue.courses] == [
        ("Basic Certificate in Dental Assisting", 600, "Two consecutive training days"),
        ("Certificate in Dental Clinic Front Desk Operations", 450, "One training day"),
    ]
    assert all(course["code"] == "" for course in catalogue.courses)
    assert len(catalogue.approved_intakes) == 6
    assert catalogue.course["training_time"] == "9:30am to 5:30pm on each training day"
    assert catalogue.courses[1]["training_time"] is None


def test_faq_routes_to_faq_agent_and_lists_exact_courses(client, conversation_id):
    response = send(client, conversation_id, "What course is available?")
    payload = response.get_json()
    content = payload["message"]["content"]
    assert response.status_code == 200
    assert payload["routing"]["intent"] == "faq"
    assert payload["routing"]["agent"] == "faq"
    assert payload["routing"]["handler"] == "crewai"
    assert "Basic Certificate in Dental Assisting" in content
    assert "Certificate in Dental Clinic Front Desk Operations" in content
    assert "$600 nett" in content and "$450 nett" in content
    assert "Infection Control" not in content


def test_selected_course_fees_use_current_contract(client, conversation_id):
    dental = send(client, conversation_id, "How much is the Basic Certificate in Dental Assisting?").get_json()["message"]["content"]
    front_desk = send(client, conversation_id, "How much is the Front Desk Operations course?").get_json()["message"]["content"]
    # Reply Template wording: "The course fee is $600 nett for a duration of ...".
    assert dental.startswith("The course fee is $600 nett for a duration of 2 consecutive days")
    assert front_desk == "The course fee for the Certificate in Dental Clinic Front Desk Operations is $450 nett for one training day."


def test_generic_fee_question_lists_both_approved_fees(client, conversation_id):
    reply = send(client, conversation_id, "How much is the course?").get_json()["message"]["content"]
    assert "which course" not in reply.casefold()
    assert "$600" in reply and "$450" in reply
    assert "Basic Certificate in Dental Assisting" in reply
    assert "Certificate in Dental Clinic Front Desk Operations" in reply


def test_duration_and_training_time_are_course_scoped(client, conversation_id):
    dental = send(client, conversation_id, "How long is Dental Assisting and what time is it?").get_json()["message"]["content"]
    front_desk = send(client, conversation_id, "What time is the Front Desk Operations course?").get_json()["message"]["content"]
    assert "2 consective days" in dental
    assert "930am to 530pm" in dental
    assert "one training day" in front_desk.casefold()
    assert "training hours have not been confirmed" in front_desk
    assert "930am" not in front_desk


def test_current_dates_come_from_the_catalogue(client, conversation_id, fake_ai):
    payload = send(client, conversation_id, "When is the next class?").get_json()
    reply = payload["message"]["content"]
    for expected in (
        "18–19 November 2026", "28–29 November 2026", "16–17 December 2026",
        "2 December 2026", "9 January 2027", "3 February 2027",
    ):
        assert expected in reply
    assert len(fake_ai.classify_calls) == 1
    assert fake_ai.faq_calls == []
    assert fake_ai.enrollment_calls == []


def test_dental_assisting_policy_facts_use_approved_reply_template(client, conversation_id):
    expectations = (
        ("Can I use SkillsFuture for Dental Assisting?", "The Basic Certificate in Dental Assisting course is only eligible for the \"Basic Tier\""),
        ("Can I use Mid-Career SkillsFuture Credits for Dental Assisting?", "You cannot use Midcareer Skillsfuture Credit"),
        ("How does UTAP work for Dental Assisting?", "50% unfunded course fee support"),
        ("Can I pay by PayNow for Dental Assisting?", "201841969G"),
        ("What is the minimum qualification for Dental Assisting?", "GCE 'N' Levels"),
        ("Will I get a job after the Dental Assisting course?", "job opportunities for suitable candidates"),
        ("What is the Dental Assisting course website?", "qandm.edu.sg/Courses/Dental-Assistant"),
    )
    for prompt, expected in expectations:
        reply = send(client, conversation_id, prompt).get_json()["message"]["content"]
        assert expected in reply, prompt
    venue = send(client, conversation_id, "Where is the Dental Assisting course venue?").get_json()["message"]["content"]
    # The supplied sources conflict on venue, so it still requires staff confirmation.
    assert "confirm" in venue.casefold()
    assert "Clementi Loop" not in venue


def test_skillsfuture_reply_guides_balance_check_without_singpass_access(client, conversation_id):
    reply = send(client, conversation_id, "How do I check my SkillsFuture balance?").get_json()["message"]["content"]
    assert "https://sfc.myskillsfuture.gov.sg/claim" in reply
    assert "You may login into the Skillsfuture Portal to find out the balance" in reply


def test_enrollment_process_question_preserves_strict_sequence(client, conversation_id):
    payload = send(client, conversation_id, "How can I enroll?").get_json()
    reply = payload["message"]["content"]
    assert payload["routing"]["intent"] == "faq"
    assert "Choose one or both courses" in reply
    assert "select one valid intake for each course" in reply
    assert "participant particulars" in reply
    assert "review the summary" in reply
    assert "explicitly confirm" in reply


def test_payment_fields_and_screenshots_are_not_collected(client, conversation_id):
    payment = send(client, conversation_id, "Wat are the paymnt options?").get_json()["message"]["content"]
    screenshot = send(client, conversation_id, "Can I send my payment screenshot by WhatsApp?").get_json()["message"]["content"]
    assert "2) Paynow. UEN: 201841969G" in payment
    assert "not eligible for Midcareer Skillsfuture credits" in payment
    assert "Front Desk Operations have not been confirmed" in payment
    assert "does not collect payment screenshots" in screenshot
    assert "email or WhatsApp" not in screenshot


def test_unknown_faq_is_answered_honestly(client, conversation_id):
    reply = send(client, conversation_id, "Is parking included with the course?").get_json()["message"]["content"]
    assert "team will need to confirm" in reply.casefold()


def test_unclear_message_requests_clarification(client, conversation_id):
    payload = send(client, conversation_id, "Hello there").get_json()
    assert payload["routing"]["intent"] == "unclear"
    assert "learn more about the course" in payload["message"]["content"].casefold()


def test_enrollment_routes_to_agent_without_selecting(client, conversation_id):
    payload = send(client, conversation_id, "I would like to register").get_json()
    assert payload["routing"]["intent"] == "enrollment"
    assert payload["routing"]["agent"] == "enrollment"
    assert payload["enrollment"]["course_id"] is None
    assert "$600 nett" in payload["message"]["content"]
    assert "$450 nett" in payload["message"]["content"]


def test_fabricated_high_risk_facts_are_replaced_with_grounded_copy(tmp_path):
    class FabricatingAIService(FakeAIService):
        def answer_faq(self, *args, **kwargs):
            return FAQAnswer(answer="The fee is S$999 and the next intake is 1 January 2030.", matched_topics=["course fee and value"])

    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'guard.db').as_posix()}"}, ai_service=FabricatingAIService())
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    reply = send(client, conversation_id, "Why is this course so expensive?").get_json()["message"]["content"]
    assert "S$999" not in reply
    assert "1 January 2030" not in reply
    assert "$600 nett" in reply and "$450 nett" in reply


def test_customer_reply_hides_internal_terminology(client, conversation_id):
    reply = send(client, conversation_id, "Tell me about the course").get_json()["message"]["content"].casefold()
    for hidden_term in ("prototype", "fictional", "synthetic", "mock data", "crewai", "openai", "intent classification", "routing", "faq agent"):
        assert hidden_term not in reply


def test_older_saved_copy_is_cleaned_before_display():
    old_reply = "Prototype notice: fictional demonstration information.\n\nThe fictional prototype course duration is 2 days."
    assert clean_customer_copy(old_reply) == "The course duration is 2 days."
