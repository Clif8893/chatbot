"""Course fee estimates, with funding deductions only where rules are approved."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date
import re
from backend.services.reply_parsing import choice_position, reply_decision

COURSE_ID = "dental-assisting-basic"
FUNDING_ENTRIES = {
    COURSE_ID: ("da.utap", "da.payment_modes", "da.mid_career"),
    "dental-clinic-front-desk-operations": ("fd.utap", "fd.payment_modes", "fd.mid_career", "fd.catalogue_facts"),
}
UTAP_DISCLAIMER = "UTAP reimbursement is subject to eligibility, remaining annual entitlement and claim approval. UTAP is reimbursed after completion, not deducted upfront. UTAP cannot be combined with SkillsFuture."
SKILLSFUTURE_DISCLAIMER = "Basic SkillsFuture Credit only; Mid-Career credit is not eligible. UTAP cannot be combined with SkillsFuture."


def estimate(kb, credit=0, ntuc=False, age_40=False, mid_career=False, *, course_id=COURSE_ID):
    if type(ntuc) is not bool or type(age_40) is not bool or type(mid_career) is not bool:
        raise ValueError("Membership, age and credit tier must be yes/no values.")
    if mid_career:
        raise ValueError("Mid-Career SkillsFuture Credit is not eligible; enter basic-tier credit only.")
    try:
        credit = Decimal(str(credit))
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid basic-tier credit amount.") from None
    if not credit.is_finite() or credit < 0 or credit.as_tuple().exponent < -2:
        raise ValueError("Credit must be a nonnegative amount with at most two decimal places.")
    if credit and ntuc:
        raise ValueError("Choose either SkillsFuture Credit or UTAP, not both.")
    required = FUNDING_ENTRIES.get(course_id)
    if not required:
        raise ValueError("Funding rules require staff confirmation.")
    if any(kb.entry(key) is None or kb.entry(key).confirmation_required for key in required):
        raise ValueError("Funding rules require staff confirmation.")
    course = kb.catalogue.course_for(course_id)
    fee_data = course["fee"]
    fee = Decimal(str(fee_data["amount"]))
    if credit > fee:
        discounted = fee < Decimal(str(fee_data.get("original_amount", fee)))
        suffix = " after the Q&M discount" if discounted else ""
        raise ValueError(f"Credit to use cannot exceed the course fee of S${fee:.2f}{suffix}.")
    reimbursement = min(fee * Decimal("0.5"), Decimal(500 if age_40 else 250)) if ntuc else Decimal(0)
    fmt = lambda value: format(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
    discount_active = bool(fee_data.get("qm_discount_percent")) and fee_data.get("qm_discount_active", True)
    return {"course_id": course_id, "course_name": course.get("name", "Dental Assisting (Basic)"),
            "fee": fmt(fee), "original_fee": fmt(fee_data.get("original_amount", fee) if discount_active else fee),
            "discount_amount": fmt(fee_data.get("qm_discount_amount", 0) if discount_active else 0),
            "discount_percent": fee_data.get("qm_discount_percent", 0) if discount_active else 0,
            "discount_expires_on": fee_data.get("qm_discount_expires_on") if discount_active else None,
            "discount_reason": fee_data.get("discount_reason", "") if discount_active else "",
            "skillsfuture_amount": fmt(credit),
            "utap_selected": ntuc,
            "pay_now": fmt(fee - credit), "possible_reimbursement": fmt(reimbursement),
            "effective_cost": fmt(fee - credit - reimbursement),
            "disclaimer": UTAP_DISCLAIMER if ntuc else SKILLSFUTURE_DISCLAIMER if credit else "",
            "kb_entry_ids": list(required)}


def _start_course_estimate(conversation, kb, course):
    if course is None or course == "both":
        conversation.funding_state = {"step": "course"}
        return kb.catalogue.course_choice_prompt(
            "Which course would you like to calculate funding for? Please choose one."
        )
    if course["id"] not in FUNDING_ENTRIES:
        conversation.funding_state = None
        return f"Funding eligibility for the {course['name']} requires staff confirmation."
    conversation.funding_state = {"step": "credit", "course_id": course["id"]}
    return (
        "How much basic-tier SkillsFuture Credit would you like to use? "
        "Enter an amount, or 0 for none."
    )


def funding_result_course(text, catalogue):
    return next((course for course in catalogue.courses
                 if text.startswith(f"{course['name']}\n\nCourse fee: S$")), None)


def pending_funding_prompt(conversation, kb):
    state = conversation.funding_state or {}
    step = state.get("step")
    if step == "course":
        return kb.catalogue.course_choice_prompt("Which course would you like to calculate funding for? Please choose one.")
    if step == "credit":
        fee = kb.catalogue.course_fee_for(state["course_id"])
        return ("How much basic-tier SkillsFuture Credit would you like to use? "
                f"Enter up to S${fee:.2f}, or 0 for none (or cancel).")
    if step == "ntuc":
        return "Are you an NTUC Union member? Reply yes or no (or cancel)."
    if step == "age":
        return "Are you aged 40 or above? Reply yes or no (or cancel)."
    return ""


def chat_estimate(conversation, text, kb, course=None, prior_messages=()):
    state = dict(conversation.funding_state or {})
    if not state:
        if not re.search(r"\b(?:estimate|c\s*alculate)\b.*\b(?:funding|cost|fees?)\b|\bfunding calculator\b", text, re.I):
            last_reply = next((message.content for message in reversed(prior_messages)
                               if message.role == "assistant"), "")
            if funding_result_course(last_reply, kb.catalogue):
                position = choice_position(text)
                if position is not None and 1 <= position <= len(kb.catalogue.courses):
                    return _start_course_estimate(conversation, kb, kb.catalogue.courses[position - 1])
            return None
        return _start_course_estimate(conversation, kb, course)
    if text.strip().casefold() in {"cancel", "exit"}:
        conversation.funding_state = None
        return "Funding calculation cancelled."
    if state["step"] == "course":
        if re.search(r"\?|^\s*(?:what|how|why|when|where|can|could|do|does|is|are|tell|explain)\b", text, re.I):
            return None
        selected = kb.catalogue.picked_course(text)
        if selected is None and choice_position(text) is None:
            return None
        return _start_course_estimate(conversation, kb, selected)
    # Old sessions may have entered the calculator before course selection was
    # required. Resolve those explicitly rather than retaining the old default.
    if state.get("course_id") not in FUNDING_ENTRIES:
        return _start_course_estimate(conversation, kb, course)
    if "mid-career" in text.casefold() or "mid career" in text.casefold():
        return "Mid-Career credit is not eligible.\n\n" + pending_funding_prompt(conversation, kb)
    step = state["step"]
    try:
        if step == "credit":
            amount = re.fullmatch(
                r"\s*(?:S\$|SGD|\$)?\s*([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|[+-]?\.\d+|NaN|[+-]?Infinity)\s*",
                text, re.I,
            )
            if amount is None:
                return None
            credit = amount.group(1).replace(",", "")
            result = estimate(kb, credit, course_id=state["course_id"])
            state["credit"] = result["skillsfuture_amount"]
            if Decimal(credit) > 0:
                conversation.funding_state = None
                return render(result, kb)
            state["step"] = "ntuc"
            conversation.funding_state = state
            return "Are you an NTUC Union member? Reply yes or no (or cancel)."
        answer = reply_decision(text)
        if answer is None:
            return None
        if step == "ntuc" and answer == "yes":
            conversation.funding_state = {**state, "step": "age"}
            return "Are you aged 40 or above? Reply yes or no (or cancel)."
        result = estimate(kb, 0, ntuc=step == "age", age_40=step == "age" and answer == "yes",
                          course_id=state["course_id"])
        conversation.funding_state = None
        return render(result, kb)
    except ValueError as error:
        return str(error) + "\n\n" + pending_funding_prompt(conversation, kb)


def render(result, kb):
    from types import SimpleNamespace
    from backend.services.enquiry.fact_guard import GuardContext, check
    from backend.services.enquiry.facts import amount_fact
    fee_lines = [f"Course fee: S${result['original_fee']}"]
    if result["discount_percent"]:
        expires = date.fromisoformat(result["discount_expires_on"]).strftime("%d %B %Y").lstrip("0")
        fee_lines.append(f"Q&M-funded discount ({result['discount_percent']}%): -S${result['discount_amount']}")
        if result["discount_reason"]:
            fee_lines.append(result["discount_reason"])
        fee_lines.append(f"Ends on {expires} at 11:59 pm.")
    fee_lines.append(f"Basic SkillsFuture Credit used: S${result['skillsfuture_amount']}")
    payment_lines = [f"Pay now: S${result['pay_now']}"]
    if result["utap_selected"]:
        payment_lines.append(f"Possible reimbursement later: S${result['possible_reimbursement']}")
    sections = [
        result["course_name"],
        "\n".join(fee_lines),
        "\n".join(payment_lines),
    ]
    if result["disclaimer"]:
        sections.append("Funding details:\n- " + result["disclaimer"].replace(". ", ".\n- "))
    text = "\n\n".join(sections)
    allowed = kb.fact_allow_list()
    # Only this deterministic tool's computed amounts extend the allow-list.
    for field in ("fee", "skillsfuture_amount", "pay_now", "possible_reimbursement"):
        allowed = allowed.union(amount_fact(result[field]))
    turn = SimpleNamespace(reply=text, response_type="grounded_answer", kb_entry_ids=result["kb_entry_ids"])
    if check(turn, GuardContext(allowed=allowed, kb_ids=kb.entry_ids())):
        raise ValueError("Funding estimate requires staff confirmation.")
    return text
