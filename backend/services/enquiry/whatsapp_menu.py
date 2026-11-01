"""Native menu encoding. IDs retain the full approved label across transport limits."""
import base64


def menu_id(label):
    return "qm:" + base64.urlsafe_b64encode(label.encode()).decode().rstrip("=")


def inbound_text(payload):
    interactive = payload.get("interactive") or {}
    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
    identifier = reply.get("id", "")
    if identifier.startswith("qm:"):
        try:
            return base64.b64decode(identifier[3:] + "=" * (-len(identifier[3:]) % 4), altchars=b"-_", validate=True).decode()
        except (ValueError, UnicodeError):
            pass
    return reply.get("title") or payload.get("body") or (payload.get("text") or {}).get("body") or ""


def interactive_payload(body, labels):
    labels = list(dict.fromkeys(label for label in labels if isinstance(label, str) and label.strip()))[:10]
    if not labels or len(body) > 1024:
        return None
    if len(labels) <= 3 and all(len(label) <= 20 for label in labels):
        return {"type": "button", "body": {"text": body}, "action": {"buttons": [
            {"type": "reply", "reply": {"id": menu_id(label), "title": label}} for label in labels]}}
    return {"type": "list", "body": {"text": body}, "action": {"button": "Explore courses", "sections": [{"rows": [
        {"id": menu_id(label), "title": label[:24], **({"description": label[:72]} if len(label) > 24 else {})} for label in labels]}]}}
