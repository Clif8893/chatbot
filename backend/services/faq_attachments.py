"""Deterministic FAQ attachment selection; assets never enter AI prompts."""

import json
import re
from difflib import get_close_matches
from pathlib import Path


ATTACHMENTS_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "faq_attachments.json"
)


class FAQAttachmentCatalogue:
    def __init__(self, path=ATTACHMENTS_PATH):
        self.path = Path(path)
        self.attachments = json.loads(self.path.read_text(encoding="utf-8"))

    def for_reply(self, message, matched_topics):
        """Return metadata only for SkillsFuture tier FAQ replies."""
        item = self.attachments["skillsfuture_credit_guide"]
        if item.get("image_status") not in {"approved", "client_approved"}:
            return None
        topics = {str(topic).casefold() for topic in matched_topics or []}
        configured_topics = {
            topic.casefold() for topic in item.get("trigger_topics", [])
        }
        # The current participant message must explicitly be about SkillsFuture.
        # A topic inferred from older context must never attach an FAQ image.
        if topics & configured_topics and _is_skillsfuture_tier_question(message):
            return {
                "image_url": item["image_url"],
                "image_alt": item["image_alt"],
                "image_status": item["image_status"],
            }
        return None


def _is_skillsfuture_tier_question(message):
    lowered = str(message or "").casefold()
    words = re.findall(r"[a-z]+", lowered)
    skillsfuture_like = any(
        get_close_matches("skillsfuture", [word], n=1, cutoff=0.68)
        for word in words
    )
    mid_career = (
        "mid-career" in lowered
        or "mid career" in lowered
        or (
            any(word.startswith("mid") for word in words)
            and any(
                get_close_matches("career", [word], n=1, cutoff=0.72)
                for word in words
            )
        )
    )
    tier_language = any(
        term in lowered
        for term in ("tier", "credit", "credits", "top-up", "top up")
    )
    return skillsfuture_like or (mid_career and tier_language)


__all__ = ["ATTACHMENTS_PATH", "FAQAttachmentCatalogue"]
