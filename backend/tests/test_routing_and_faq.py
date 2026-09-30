"""Intent routing and grounded FAQ behavior."""

from decimal import Decimal

from backend.agents.schemas import FAQAnswer
from backend.app import create_app
from backend.services.course_catalogue import CourseCatalogue
from backend.services.chat_service import clean_customer_copy
from backend.tests.conftest import FakeAIService, authenticated_client, send


def test_company_course_information_is_loaded_without_invented_intakes():
    catalogue = CourseCatalogue()
    assert catalogue.course_name == "2-Day Basic Certificate in Dental Assisting"
    assert catalogue.course_fee == Decimal("600")
    assert catalogue.course["duration"] == "two consecutive training days"
    assert catalogue.course["training_time"] == "9:30am-5:30pm on each training day"
    assert catalogue.course["venue"]["reply_template_wording"] == "our HQ at Clementi Loop"
    assert catalogue.course["venue"]["confirmation_required"] is True
    assert catalogue.course["paynow"]["uen"] == "201841969G"
    assert catalogue.intake_dates == []
    topics = {item["topic"] for item in catalogue.public_summary()["faqs"]}
    assert {
        "course description",
        "course fee and value",
        "payment options",
        "enrolment process",
        "Mid-Career SkillsFuture",
        "course fee concern",
        "job opportunities",
        "minimum qualification",
        "UTAP",
    } <= topics


def test_faq_routes_to_faq_agent_with_customer_friendly_copy(client, conversation_id):
    response = send(client, conversation_id, "What course is available?")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["routing"]["intent"] == "faq"
    assert payload["routing"]["agent"] == "faq_agent"
    assert "2-Day Basic Certificate" in payload["message"]["content"]
    assert "prototype" not in payload["message"]["content"].casefold()
    assert "fictional" not in payload["message"]["content"].casefold()


def test_course_introduction_keeps_pdf_order_and_current_cautions(
    client, conversation_id
):
    reply = send(client, conversation_id, "Tell me about the course").get_json()[
        "message"
    ]["content"]
    assert reply.startswith("📚 Basic Certificate in Dental Assisting\n\n")
    assert reply.index("S$600 nett") < reply.index("Two consecutive training days")
    assert reply.index("Two consecutive training days") < reply.index("9:30am to 5:30pm")
    assert "exact training venue will be confirmed" in reply
    assert "SkillsFuture Credits may be used" in reply
    assert "July 2026" not in reply
    assert "August 2026" not in reply


def test_course_description_matches_approved_reply_material(client, conversation_id):
    reply = send(client, conversation_id, "What will I learn in this course?").get_json()[
        "message"
    ]["content"]
    assert "dental instruments and materials" in reply
    assert "infection control" in reply
    assert "anticipate a dentist’s needs" in reply


def test_selected_course_fee_reply_uses_current_contract(client, conversation_id):
    payload = send(
        client,
        conversation_id,
        "How much is the 2-Day Basic Certificate in Dental Assisting?",
    ).get_json()
    reply = payload["message"]["content"]
    assert reply == (
        "The 2-Day Basic Certificate in Dental Assisting is S$600 nett."
    )


def test_expensive_course_reply_keeps_all_pdf_paragraphs_and_bullets(
    client, conversation_id
):
    reply = send(client, conversation_id, "Why is your course so expensive?").get_json()[
        "message"
    ]["content"]
    assert "Thank you for sharing your concern" in reply
    assert "cost is an important consideration" in reply
    assert "quality and value" in reply
    assert "📚 The S$600 nett fee for the two-day course includes:" in reply
    assert "- Comprehensive course materials" in reply
    assert "- Instruction from experienced trainers" in reply
    assert "- Access to specialised equipment and facilities" in reply
    assert "- Practical knowledge that supports professional development" in reply
    assert "take it into consideration for future planning" in reply
    assert "please feel free to ask 😊" in reply
    assert reply.count("\n\n") >= 4


def test_duration_and_training_time_are_from_company_information(
    client, conversation_id
):
    payload = send(
        client,
        conversation_id,
        "How long is the 2-Day Basic Certificate in Dental Assisting and what time is it?",
    ).get_json()
    reply = payload["message"]["content"]
    assert "two consecutive training days" in reply.casefold()
    assert "9:30am-5:30pm on each training day" in reply


def test_skillsfuture_basic_tier_is_accepted(client, conversation_id):
    payload = send(client, conversation_id, "Can I use SkillsFuture?").get_json()
    reply = payload["message"]["content"]
    assert "basic-tier SkillsFuture Credits" in reply
    assert "Mid-Career SkillsFuture Credits cannot be used" in reply
    assert "submit your own claim" in reply
    assert "sfc.myskillsfuture.gov.sg/claim" in reply


def test_mid_career_skillsfuture_is_rejected(client, conversation_id):
    payload = send(
        client, conversation_id, "Can I use Mid-Career SkillsFuture Credits?"
    ).get_json()
    reply = payload["message"]["content"]
    assert reply.startswith("Mid-Career SkillsFuture Credits\n\n")
    assert "two tiers" in reply
    assert "cannot be used" in reply
    assert "basic-tier SkillsFuture Credit balance" in reply
    assert "sfc.myskillsfuture.gov.sg/claim" in reply


def test_paynow_information_uses_company_uen(client, conversation_id):
    payload = send(
        client,
        conversation_id,
        "Can I pay by PayNow for the 2-Day Basic Certificate in Dental Assisting?",
    ).get_json()
    reply = payload["message"]["content"]
    assert "PayNow is accepted" in reply
    assert "201841969G" in reply
    assert "SkillsFuture amount" in reply


def test_utap_guidance_explains_reimbursement_rules(client, conversation_id):
    payload = send(client, conversation_id, "How does UTAP work?").get_json()
    reply = payload["message"]["content"]
    assert "pay the course fee upfront" in reply
    assert "attend both training days" in reply
    assert "within six months" in reply
    assert "S$250 per year" in reply
    assert "S$500 per year" in reply
    assert "- Proof of payment" in reply
    assert "- Proof of completion" in reply
    assert "NTUC U Portal" in reply
    assert "cannot be combined with SkillsFuture" in reply
    assert "1. Confirm that your NTUC Union membership is valid" in reply
    assert "2. Enrol in the course" in reply
    assert "3. After completing the course" in reply
    assert "late submissions will not be accepted" in reply


def test_misspelt_utap_question_uses_approved_guidance(client, conversation_id):
    reply = send(client, conversation_id, "How does UTP reimbursment wrk?").get_json()[
        "message"
    ]["content"]
    assert "pay the course fee upfront" in reply
    assert "within six months" in reply
    assert "NTUC U Portal" in reply


def test_minimum_qualification_guidance(client, conversation_id):
    payload = send(
        client, conversation_id, "What is the minimum qualification?"
    ).get_json()
    reply = payload["message"]["content"]
    assert "GCE N Levels are preferred" in reply
    assert "basic English reading and writing" in reply
    assert reply.startswith("✅ Minimum Qualification\n\n")


def test_job_opportunities_are_not_guaranteed(client, conversation_id):
    payload = send(
        client, conversation_id, "Will I get a job after the course?"
    ).get_json()
    reply = payload["message"]["content"]
    assert "Suitable candidates may be considered" in reply
    assert "last day of the class" in reply
    assert "Interview performance" in reply
    assert "employment is not guaranteed" in reply.casefold()
    for factor in (
        "- Workplace location",
        "- Work schedule",
        "- Skills and experience",
        "- Suitability and cultural fit",
        "- Interview performance",
    ):
        assert factor in reply


def test_missing_current_intakes_use_exact_company_fallback(
    client, conversation_id, fake_ai
):
    payload = send(client, conversation_id, "When is the next class?").get_json()
    assert payload["message"]["content"].startswith(
        "Our upcoming course dates are being updated. Please leave your contact "
        "details, and our team will confirm the available dates with you."
    )
    assert fake_ai.classify_calls == []
    assert fake_ai.faq_calls == []
    assert fake_ai.enrollment_calls == []
    assert "July 2026" not in payload["message"]["content"]
    assert "August 2026" not in payload["message"]["content"]


def test_misspelt_intake_question_uses_exact_company_fallback(
    client, conversation_id
):
    payload = send(client, conversation_id, "Wen is the nxt intke?").get_json()
    assert payload["message"]["content"].startswith(
        "Our upcoming course dates are being updated. Please leave your contact "
        "details, and our team will confirm the available dates with you."
    )


def test_venue_uses_selected_course_reply_without_unconfirmed_addresses(
    client, conversation_id
):
    payload = send(
        client,
        conversation_id,
        "Where is the 2-Day Basic Certificate in Dental Assisting course venue?",
    ).get_json()
    reply = payload["message"]["content"]
    assert "exact training venue will be confirmed" in reply
    assert "Clementi Loop" not in reply
    assert "Kitchener Road" not in reply


def test_course_website_uses_supplied_reply_template(client, conversation_id):
    payload = send(client, conversation_id, "What is the course website?").get_json()
    assert (
        "qandm.edu.sg/Courses/Dental-Assistant/4/Basic-Certificate-In-Dental-Assisting"
        in payload["message"]["content"]
    )


def test_enrollment_process_question_routes_to_faq(client, conversation_id):
    payload = send(client, conversation_id, "How can I enroll?").get_json()
    assert payload["routing"]["intent"] == "faq"
    reply = payload["message"]["content"]
    assert "1. Full name as shown on your NRIC" in reply
    assert "2. NRIC or FIN number" in reply
    assert "6. Preferred intake date" in reply
    assert "9. PayNow amount" in reply
    assert "all the details in one message" in reply
    assert "summary" in reply


def test_payment_guidance_uses_only_approved_options(client, conversation_id):
    reply = send(client, conversation_id, "Wat are the paymnt options?").get_json()[
        "message"
    ]["content"]
    assert "PayNow" in reply
    assert "Basic-tier SkillsFuture Credits" in reply
    assert "UTAP reimbursement" in reply
    assert "cannot be combined" in reply
    assert reply.startswith("💳 Payment Methods\n\n")
    assert "1. Basic-tier SkillsFuture Credits" in reply
    assert "2. PayNow" in reply
    assert "3. UTAP reimbursement" in reply


def test_payment_screenshot_guidance_prefers_email(client, conversation_id):
    reply = send(
        client, conversation_id, "Can I send my payment screenshot by WhatsApp?"
    ).get_json()["message"]["content"]
    assert "email or WhatsApp" in reply
    assert "Email is the preferred channel" in reply


def test_unknown_faq_is_answered_honestly(client, conversation_id):
    payload = send(client, conversation_id, "Is parking included with the course?").get_json()
    assert "team will need to confirm" in payload["message"]["content"].casefold()


def test_unclear_message_requests_clarification(client, conversation_id):
    payload = send(client, conversation_id, "Hello there").get_json()
    assert payload["routing"]["intent"] == "unclear"
    assert "learn more about the course" in payload["message"]["content"].casefold()


def test_enrollment_routes_to_enrollment_agent(client, conversation_id):
    payload = send(client, conversation_id, "I would like to register").get_json()
    assert payload["routing"]["intent"] == "enrollment"
    assert payload["routing"]["agent"] == "enrollment_agent"
    assert "s$600 nett" in payload["message"]["content"].casefold()


def test_informal_misspelt_enrollment_request_is_understood(client, conversation_id):
    payload = send(client, conversation_id, "i wana enrl").get_json()
    assert payload["routing"]["intent"] == "enrollment"
    assert "s$600 nett" in payload["message"]["content"].casefold()


def test_misspelt_indirect_fee_question_routes_to_faq(client, conversation_id):
    payload = send(client, conversation_id, "hi, hw much iz da corse?").get_json()
    assert payload["routing"]["intent"] == "faq"
    assert "S$600 nett" in payload["message"]["content"]


def test_customer_first_name_personalises_faq_during_enrollment(
    client, conversation_id
):
    send(client, conversation_id, "Please register me")
    send(client, conversation_id, "My name is Test Student")
    payload = send(client, conversation_id, "How much is the fee?").get_json()
    assert payload["message"]["content"].startswith("Hi Test 👋\n\n")
    assert payload["message"]["content"].count("Test") == 1
    assert (
        "continue with your remaining enrollment details"
        in payload["message"]["content"].casefold()
    )


def test_fee_concern_uses_customer_first_name_once(client, conversation_id):
    send(client, conversation_id, "Please register me")
    send(client, conversation_id, "My name is Test Student")
    reply = send(client, conversation_id, "Why is the course so expensive?").get_json()[
        "message"
    ]["content"]
    assert reply.startswith("Hi Test 👋\n\nThank you for sharing your concern")
    assert reply.count("Test") == 1


def test_fabricated_high_risk_facts_are_replaced_with_staff_confirmation(tmp_path):
    class FabricatingAIService(FakeAIService):
        def answer_faq(self, *args, **kwargs):
            return FAQAnswer(
                answer="The fee is S$999 and the next intake is 1 January 2030.",
                matched_topics=["course fee and value"],
            )

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'guard.db').as_posix()}",
        },
        ai_service=FabricatingAIService(),
    )
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    reply = send(client, conversation_id, "Why is this course so expensive?").get_json()[
        "message"
    ]["content"]
    assert "S$999" not in reply
    assert "1 January 2030" not in reply
    assert "S$600 nett" in reply
    assert "course materials" in reply


def test_customer_reply_hides_internal_terminology(client, conversation_id):
    payload = send(client, conversation_id, "Tell me about the course").get_json()
    reply = payload["message"]["content"].casefold()
    for hidden_term in (
        "prototype",
        "fictional",
        "synthetic",
        "mock data",
        "crewai",
        "openai",
        "intent classification",
        "routing",
        "faq agent",
    ):
        assert hidden_term not in reply


def test_older_saved_copy_is_cleaned_before_display():
    old_reply = (
        "Prototype notice: fictional demonstration information.\n\n"
        "The fictional prototype course duration is 2 days."
    )
    cleaned = clean_customer_copy(old_reply)
    assert cleaned == "The course duration is 2 days."
