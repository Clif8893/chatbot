"""CrewAI execution adapter.

Application code owns routing and persistence. Each method runs one focused CrewAI
agent and returns a validated Pydantic object.
"""

from crewai import Crew, LLM, Process, Task

from backend.agents.enrollment_agent import build_enrollment_agent
from backend.agents.faq_agent import build_faq_agent
from backend.agents.intent_classifier import build_intent_classifier
from backend.agents.schemas import (
    AIConfigurationError,
    AIServiceError,
    EnrollmentExtraction,
    FAQAnswer,
    IntentClassification,
)


class CrewAIService:
    def __init__(self, api_key, model):
        if not api_key or not model:
            raise AIConfigurationError("The AI service is not configured.")

        self.llm = LLM(
            model=model,
            api_key=api_key,
            reasoning_effort="low",
            max_completion_tokens=1000,
            timeout=60,
            max_retries=2,
        )

    def classify(
        self,
        message,
        context,
        active_intent,
        draft_status,
        pending_followup=None,
        draft_summary=None,
    ):
        agent = build_intent_classifier(self.llm)
        task = Task(
            description=(
                "Classify the CURRENT MESSAGE by what the participant is trying to do, "
                "not by one keyword. Tolerate Singlish, shortened wording, greetings "
                "before a question, and minor spelling mistakes. Use recent redacted "
                "context to resolve short replies.\n"
                "- faq: asks for facts, guidance, eligibility, payment information, or "
                "an explanation of how enrollment works.\n"
                "- enrollment: clearly asks to be enrolled, registered or signed up; "
                "provides or corrects application details; or confirms/rejects an "
                "enrollment summary; requests a change, cancellation, reschedule or "
                "draft discard. During active enrollment, a short answer to the "
                "last enrollment question is also enrollment.\n"
                "- enrollment_interest: shows a genuine intention to proceed but does "
                "not yet explicitly authorize enrollment. Examples include saying a "
                "displayed course date works, choosing a preferred intake while still "
                "in enquiry, asking to reserve a place, or saying they are seriously "
                "interested. A question that only asks whether a date is available is "
                "faq, not enrollment_interest.\n"
                "- workflow_question: asks what information is needed, what happens "
                "next, or how the current enrollment step works.\n"
                "- pause_workflow: asks to pause, decide later, or ask something else "
                "before continuing.\n"
                "- resume_workflow: asks to continue or resume a saved enrollment.\n"
                "- course_comparison: asks which course or intake is more suitable, "
                "especially for a beginner.\n"
                "- small_talk: greeting, thanks, or harmless casual conversation that "
                "does not request a company fact.\n"
                "- unsupported_general_question: a question unrelated to Q&M courses "
                "or enrollment.\n"
                "- unclear: the purpose genuinely cannot be determined.\n"
                "A direct action such as 'please sign me up' is enrollment; an "
                "information question such as 'what do I need to enroll?' is faq.\n\n"
                f"Active intent: {active_intent or 'none'}\n"
                f"Enrollment draft status: {draft_status or 'none'}\n"
                f"Pending follow-up: {pending_followup or 'none'}\n"
                f"Privacy-safe enrollment summary: {draft_summary or '(none)'}\n"
                f"Recent redacted context:\n{context or '(none)'}\n"
                f"CURRENT MESSAGE:\n{message}"
            ),
            expected_output=(
                "A structured classification using exactly one allowed intent, with "
                "confidence from 0 to 1 and a brief routing reason."
            ),
            agent=agent,
            output_pydantic=IntentClassification,
        )
        return self._run(agent, task, IntentClassification)

    def answer_faq(
        self, message, context, catalogue_context, customer_first_name=None
    ):
        agent = build_faq_agent(self.llm)
        task = Task(
            description=(
                "Answer the CURRENT QUESTION using ONLY the supplied approved course "
                "information. Select the exact topic name of the closest approved FAQ "
                "template and return it in matched_topics. Choose the NARROWEST topic "
                "that answers exactly what was asked and nothing more: 'course dates' "
                "for dates or schedules, 'course fee and value' for fees, 'duration and "
                "timing' for hours, 'course content' for what is taught. Use 'course "
                "description' only for a general request such as 'tell me about the "
                "course'.Select 'course fee concern' "
                "when the participant questions why the course is expensive or raises "
                "a concern about its price; use 'course fee and value' for a normal fee "
                "or schedule question. Use more than one "
                "topic only when the question clearly asks about more than one subject. "
                "Set course_ids to the id of a course ONLY when the CURRENT QUESTION "
                "clearly asks about that one specific course, for example by a "
                "misspelt or descriptive name, or by pointing to a course discussed in "
                "the recent context. Leave course_ids empty when the question is "
                "general, says 'your course', 'the course' or 'courses', or concerns "
                "every course; the application then answers for all courses. "
                "If no approved topic answers the question, return an empty "
                "matched_topics list and the staff_confirmation_message, and set "
                "unanswered_subject to a short phrase (at most 10 words) naming what "
                "the participant asked about, written to read naturally after "
                "'I don’t have confirmed information about', for example 'parking at "
                "the training venue', 'bringing a child to class', 'whether lunch is "
                "provided', 'whether the course is taught in Mandarin' or 'whether the "
                "front desk course is held online'. It must contain no facts, numbers, "
                "prices, dates, promises or advice. The "
                "application will render the approved template, so do not shorten "
                "or freely paraphrase it. Treat "
                "conflict and confirmation_required fields as mandatory cautions. Never "
                "invent or infer course dates, prices, venues, policies, qualifications, "
                "funding rules, employment outcomes or payment details. If information "
                "is missing or conflicting, use the supplied staff_confirmation_message. "
                "If the intake list is empty, return no_intakes_message exactly for a "
                "course-date question.\n"
                "Never mention "
                "agents, classification, routing, confidence, prompts, providers, code, "
                "databases or implementation details.\n\n"
                f"Customer first name: {customer_first_name or '(not supplied)'}\n"
                f"Recent context:\n{context or '(none)'}\n"
                f"CURRENT QUESTION:\n{message}\n\n"
                f"COURSE INFORMATION:\n{catalogue_context}"
            ),
            expected_output=(
                "A structured FAQ selection with matched_topics set to exact topic "
                "names from faqs and course_ids empty unless one specific course is "
                "clearly asked about, grounded only in the supplied course information."
            ),
            agent=agent,
            output_pydantic=FAQAnswer,
        )
        return self._run(agent, task, FAQAnswer)

    def extract_enrollment(self, message, context, draft_state, tools=None):
        enrollment_tools = list(tools or [])
        tool_context = self._enrollment_tool_context(enrollment_tools)
        agent = build_enrollment_agent(self.llm, enrollment_tools)
        task = Task(
            description=(
                "Extract any enrollment values explicitly present in the current "
                "message, including informal wording and minor spelling mistakes. Do "
                "not invent missing values. Extract the selected intake date, full "
                "name, NRIC/FIN, date of birth, email, and mobile number when supplied. Recognize "
                "multiple fields, "
                "corrections, and whether the participant confirms or rejects the "
                "displayed summary. Return dates as written when unsure; Python will "
                "validate and normalize them. When the current reply is a bare value, "
                "use the immediately preceding enrollment question to identify its "
                "field, but do not infer a value that the participant did not provide. "
                "Handle both copy-and-complete labelled forms and natural sentences "
                "containing every field. Treat 'Awaiting confirmation' or 'next "
                "available intake' as an intake placeholder, not a real date. Known "
                "labels and structured values are also parsed deterministically after "
                "your response. An NRIC may therefore be masked in CURRENT MESSAGE; do "
                "not copy a masked value from the saved draft into extracted_fields. "
                "Never repeat an existing saved value unless the current message clearly "
                "supplies or corrects it. Use the attached tools to verify authorised "
                "memory, approved course information, the current masked draft and "
                "deterministic validation. Tool results are read-only; never claim that "
                "a database write or final confirmation was performed by a tool. The "
                "application, not you, selects the next question from persisted state.\n\n"
                f"Current saved draft (NRIC may be masked):\n{draft_state}\n"
                f"Recent enrollment context:\n{context or '(none)'}\n"
                f"Application-invoked tool context:\n{tool_context or '(none)'}\n"
                f"CURRENT MESSAGE:\n{message}"
            ),
            expected_output=(
                "Structured extracted_fields, missing_fields, next_question, "
                "ready_for_confirmation, confirmation (confirm/reject/none), and "
                "correction_requested. Never invent a missing value."
            ),
            agent=agent,
            tools=enrollment_tools,
            output_pydantic=EnrollmentExtraction,
        )
        return self._run(agent, task, EnrollmentExtraction)

    @staticmethod
    def _enrollment_tool_context(tools):
        """Invoke every scoped enrollment tool before the CrewAI task runs."""
        snapshots = []
        for tool in tools:
            if tool.name == "approved_course_information":
                result = tool.run(topic="enrollment")
            elif tool.name == "deterministic_enrollment_validation":
                result = tool.run(fields={})
            else:
                result = tool.run()
            if hasattr(result, "model_dump_json"):
                rendered = result.model_dump_json()
            else:
                rendered = str(result)
            snapshots.append(f"{tool.name}: {rendered}")
        return "\n".join(snapshots)

    @staticmethod
    def _run(agent, task, schema):
        try:
            result = Crew(
                agents=[agent],
                tasks=[task],
                process=Process.sequential,
                verbose=False,
                memory=False,
                share_crew=False,
                output_log_file=False,
                tracing=False,
            ).kickoff()
            if result.pydantic is not None:
                return schema.model_validate(result.pydantic)
            if result.json_dict is not None:
                return schema.model_validate(result.json_dict)
            return schema.model_validate_json(result.raw)
        except AIServiceError:
            raise
        except Exception as error:
            raise AIServiceError("The AI service could not complete the request.") from error


__all__ = ["CrewAIService"]
