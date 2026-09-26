"""The LLM may only improve wording — never add facts. Verified with a fake provider."""

import json

from conftest import bundle
from vera.composer import Composer


class FakeLLM:
    name = "fake:test"

    def __init__(self, body):
        self.body = body

    def complete_json(self, system, user, max_tokens=700, timeout=None):
        payload = json.loads(user)
        assert "draft" in payload and "facts" in payload
        assert "Do NOT add any number" in system
        return {"body": self.body, "rationale": "tightened wording"}


def test_polish_with_invented_number_is_rejected(data):
    c, m, t, cust = bundle(data, "trg_001_research_digest_dentists")
    fake = FakeLLM("Dr. Meera, JIDA shows 3-month recall helps 57% of your 311 patients. Want the summary? Reply YES.")
    out = Composer(fake).compose(c, m, t, cust)
    assert out.source == "playbook"
    assert "311" not in out.body


def test_polish_with_taboo_is_rejected(data):
    c, m, t, cust = bundle(data, "trg_001_research_digest_dentists")
    fake = FakeLLM("Dr. Meera, guaranteed results: JIDA Oct 2026, p.14 — 38% fewer caries. Reply YES.")
    assert Composer(fake).compose(c, m, t, cust).source == "playbook"


def test_clean_polish_is_accepted(data):
    c, m, t, cust = bundle(data, "trg_001_research_digest_dentists")
    body = ("Dr. Meera, JIDA Oct 2026 (p.14): a 2,100-patient trial found 3-month fluoride recall cut caries "
            "recurrence 38% vs 6-month in high-risk adults — you have 124 of them.\n"
            "Want the 2-min summary + a patient WhatsApp to forward? Reply YES.")
    out = Composer(FakeLLM(body)).compose(c, m, t, cust)
    assert out.source == "llm:fake:test" and out.body == body
    assert out.template_params[2].endswith("Reply YES.")
