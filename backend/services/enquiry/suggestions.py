"""Choose the suggestion buttons shown under an enquiry reply (REQ-A06).

Buttons come only from the approved menu in the knowledge base. The model
proposes ids for the participant's likely next step; the application removes
anything already answered for this participant, in this chat or an earlier one,
and anything unsuitable in the moment, then fills from a default order when
nothing relevant is left.
"""

MAX_SUGGESTIONS = 3
STARTER_MINIMUM = 3
STAFF_ID = "staff"
# No buttons while the participant is opting out, forgetting or being handed to
# enrollment, nor when they are distressed, closed for abuse, or waiting for a
# person. A polite decline (e.g. of medical advice) keeps buttons to steer back.
NO_SUGGESTION_TYPES = {"opt_out", "forget_me", "handoff_enrollment"}
NO_SUGGESTION_CATEGORIES = {"distress", "abusive_conduct", "human_request"}


def choose(
    kb,
    requested_ids,
    answered_entry_ids,
    *,
    response_type,
    escalation_category="",
    enrolling=False,
):
    """Labels for up to three buttons, or [] when buttons would be unhelpful."""
    if response_type in NO_SUGGESTION_TYPES or escalation_category in NO_SUGGESTION_CATEGORIES:
        return []
    usable = _usable(kb, answered_entry_ids, enrolling)
    if response_type == "deescalation" and STAFF_ID in kb.suggestions:
        # A frustrated participant is always offered a person (REQ-A04.1).
        requested_ids = [STAFF_ID, *requested_ids]
    chosen = _pick(requested_ids, usable, MAX_SUGGESTIONS)
    if not chosen:
        chosen = _pick(kb.suggestion_order, usable, MAX_SUGGESTIONS)
    return [kb.suggestions[key].label for key in chosen]


def starter_labels(kb, answered_entry_ids, enrolling=False):
    """The opening menu, without topics this participant has already covered."""
    usable = _usable(kb, answered_entry_ids, enrolling)
    chosen = _pick(kb.starter_suggestion_ids, usable, len(kb.starter_suggestion_ids))
    if len(chosen) < STARTER_MINIMUM:
        extra = [key for key in kb.suggestion_order if key not in chosen]
        chosen += _pick(extra, usable, STARTER_MINIMUM - len(chosen))
    return [kb.suggestions[key].label for key in chosen]


def _usable(kb, answered_entry_ids, enrolling):
    answered = set(answered_entry_ids)

    def check(key):
        item = kb.suggestions.get(key)
        if item is None or (enrolling and key == "enrol"):
            return False
        return not (set(item.covers) & answered)

    return check


def _pick(keys, usable, limit):
    chosen = []
    for key in keys:
        if len(chosen) >= limit:
            break
        if key not in chosen and usable(key):
            chosen.append(key)
    return chosen


__all__ = ["MAX_SUGGESTIONS", "choose", "starter_labels"]
