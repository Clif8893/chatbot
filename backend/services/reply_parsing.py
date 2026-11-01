"""Shared interpretation of short replies to a displayed list or a yes/no prompt.

Every workflow step that shows numbered options or asks for confirmation uses
these helpers, so a reply understood at one step is understood at all of them.
"""

import re


_POSITION_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
# Words that may surround a list choice without changing it, e.g.
# "ok I'll take the 2nd one pls" or "no. 2 lah".
_POSITION_FILLER = {
    "the", "one", "option", "intake", "choice", "number", "no", "num", "nr",
    "i", "id", "ill", "im", "d", "ll", "will", "want", "like", "would", "choose", "pick",
    "prefer", "take", "select", "go", "going", "with", "for", "to", "please",
    "pls", "plz", "ok", "okay", "la", "lah", "leh", "lor", "ah", "ya", "yes",
    "can", "it", "that", "this", "me", "my", "s", "is", "just", "then",
}
_POSITION_PREFIX = {"option", "number", "no", "intake", "choice"}


def choice_position(value):
    """Return the 1-based list position when the whole reply picks one item.

    Accepts "2", "2nd", "second", "option 2", "no. 2", "#2", "the 2nd one",
    "ok 2" or "2 la". Any other word (a month, "course", "available", a second
    number) means the reply is not a bare position, so "2nd December" or
    "28-29" are left for date matching.
    """
    text = str(value or "").casefold().replace("’", "'").replace("'", "")
    tokens = re.findall(r"[a-z]+|\d+(?:st|nd|rd|th)?", text)
    if not tokens or len(tokens) > 8:
        return None
    positions = [token for token in tokens if token not in _POSITION_FILLER]
    if not positions:
        # "one" is filler in "the second one" but a choice in "option one".
        if tokens[-1] == "one" and (len(tokens) == 1 or tokens[-2] in _POSITION_PREFIX):
            return 1
        return None
    if len(positions) != 1:
        return None
    token = positions[0]
    if token in _POSITION_WORDS:
        return _POSITION_WORDS[token]
    match = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", token)
    return int(match.group(1)) if match else None


_ORDINAL = (
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"1st|2nd|3rd|4th|5th|6th|7th|8th|9th|10th"
)
_LIST_NOUN = r"(?:one|option|choice|course|intake|date|class|programme|program|session)"
# After an ordinal these make it a date, a count or a sequence, not a list item:
# "the 1st of December", "2nd day", "first time", "the 28th Nov".
_NOT_A_LIST_ITEM = (
    r"(?:of|day|days|week|weeks|time|times|attempt|year|years|month|months|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w*"
)
_POSITION_IN_SENTENCE = re.compile(
    rf"\b(?:option|number|no\.?|choice|course|intake)\s*(?P<numbered>\d{{1,2}})\b"
    rf"|#\s*(?P<hashed>\d{{1,2}})\b"
    # "the 1st", "the second one", "2nd option": an ordinal word needs "the"
    # or a list noun, so "first, how much is it?" is not a choice.
    rf"|\bthe\s+(?P<lead>{_ORDINAL})\b(?!\s+{_NOT_A_LIST_ITEM}\b)"
    rf"|\b(?P<noun>{_ORDINAL})\s+{_LIST_NOUN}\b"
    rf"|\b(?P<suffixed>\d{{1,2}}(?:st|nd|rd|th))\b(?!\s+{_NOT_A_LIST_ITEM}\b)"
)


def referenced_position(value):
    """Return the list position a reply points at, even inside a sentence.

    Accepts everything ``choice_position`` does, plus replies such as
    "I am interested in the 1st", "the second one works for me" or
    "can I go with option 2". Returns None when the reply points at more than
    one position or at a date ("the 1st of December").
    """
    whole = choice_position(value)
    if whole is not None:
        return whole
    text = " ".join(str(value or "").casefold().replace("’", "'").split())
    found = set()
    for match in _POSITION_IN_SENTENCE.finditer(text):
        token = next(group for group in match.groups() if group)
        if token in _POSITION_WORDS:
            found.add(_POSITION_WORDS[token])
        else:
            found.add(int(re.match(r"\d+", token).group()))
    return found.pop() if len(found) == 1 else None


_AFFIRMATIVE = {
    "yes", "y", "ya", "yah", "yas", "yeah", "yea", "yep", "yup", "ok", "okay",
    "okie", "okey", "k", "kk", "sure", "confirm", "confirmed", "correct",
    "proceed", "submit", "alright", "absolutely", "definitely", "can", "agree",
    "agreed", "good", "fine", "right", "ahead", "👍",
}
_NEGATIVE = {
    "no", "n", "nope", "nah", "not", "dont", "never", "wrong", "incorrect",
    "wait", "later", "stop", "👎",
}
# Negations that turn an affirmative word around ("not correct").
_NEGATORS = {"not", "dont", "never", "no"}
_DECISION_FILLER = {
    "please", "pls", "plz", "lah", "la", "leh", "lor", "ah", "oh", "thanks",
    "thank", "you", "ty", "i", "it", "its", "that", "thats", "this", "is",
    "are", "all", "everything", "looks", "look", "sounds", "go", "do", "just",
    "so", "then", "the", "details", "info", "of", "course", "maybe", "yet",
    "now", "very", "much", "really", "im", "is", "mind", "thx",
}


def reply_decision(value, actions=()):
    """Classify a short reply to a yes/no prompt as "yes", "no" or None.

    ``actions`` are extra words that may accompany the answer at that step,
    such as "cancel" in "yes cancel it". Any other word, or any digit, means
    the reply is not a plain decision and is left to the normal routing.
    """
    text = str(value or "").casefold().replace("’", "'").replace("'", "")
    if re.search(r"\d|\?", text):
        return None
    tokens = re.findall(r"[a-z]+|👍|👎", text)
    if not tokens or len(tokens) > 8:
        return None
    allowed = _AFFIRMATIVE | _NEGATIVE | _DECISION_FILLER | set(actions)
    if any(token not in allowed for token in tokens):
        return None
    affirmative = any(token in _AFFIRMATIVE for token in tokens)
    negative = any(token in _NEGATIVE for token in tokens)
    if negative and (not affirmative or any(token in _NEGATORS for token in tokens)):
        return "no"
    if affirmative and not negative:
        return "yes"
    return None
