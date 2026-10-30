"""CrewAI flow for extracting evidence; finance rules stay in FinanceService."""

import os

# Payment evidence can contain personal banking information.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from crewai.flow.flow import Flow, start


class PaymentEvidenceFlow(Flow):
    def __init__(self, vision, image_bytes, mime_type):
        super().__init__()
        object.__setattr__(self, "_payment_vision", vision)
        object.__setattr__(self, "_payment_image", image_bytes)
        object.__setattr__(self, "_payment_mime", mime_type)

    @start()
    def extract_visible_facts(self):
        return self._payment_vision.extract(self._payment_image, self._payment_mime)
