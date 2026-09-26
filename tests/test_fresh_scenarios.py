"""The real judge injects facts and triggers the bot has never seen ("the exam is fresh
scenarios"). These tests simulate that: new digest items, shifted metrics, unseen trigger
kinds, surprise customer scopes — and check the bot adapts without inventing anything."""

import copy

from conftest import bundle
from vera.composer import Composer

composer = Composer()
MEERA = "m_001_drmeera_dentist_delhi"


def draft(c, m, t, cust=None):
    out, _ = composer.draft(c, m, t, cust)
    assert out.body and not out.issues, (t.get("kind"), out.issues, out.body)
    return out


def trig(kind, payload, merchant_id=MEERA, **kw):
    return {"id": f"trg_new_{kind}", "scope": kw.get("scope", "merchant"), "kind": kind, "source": "external",
            "merchant_id": merchant_id, "customer_id": kw.get("customer_id"), "payload": payload,
            "urgency": 3, "suppression_key": f"{kind}:{merchant_id}:new", "expires_at": "2026-05-30T00:00:00Z"}


def test_new_digest_item_is_used(data):
    c, m, _, _ = bundle(data, "trg_001_research_digest_dentists")
    c2 = copy.deepcopy(c)
    c2["digest"].append({"id": "d_NEW_sealant", "kind": "research", "title": "Resin sealants cut molar caries 41% in 6-9 year olds",
                         "source": "IJDR Nov 2026, p.3", "trial_n": 860, "patient_segment": "children",
                         "summary": "Randomised trial across 12 schools shows 41% fewer occlusal caries at 24 months with resin sealants."})
    out = draft(c2, m, trig("research_digest", {"category": "dentists", "top_item_id": "d_NEW_sealant"}))
    assert "IJDR Nov 2026, p.3" in out.body and "41%" in out.body and "860" in out.body
    assert "JIDA" not in out.body  # didn't fall back to the old item


def test_metric_shift_changes_the_message(data):
    c, m, t, _ = bundle(data, "trg_004_perf_dip_bharat")
    m2 = copy.deepcopy(m)
    m2["performance"]["delta_7d"]["calls_pct"] = -0.72
    out = draft(c, m2, {**t, "payload": {"placeholder": True}})
    assert "72%" in out.body


def test_heatwave(data):
    for tid in ("trg_010_ipl_match_delhi", "trg_020_summer_demand_shift", "trg_014_seasonal_acquisition_dip_powerhouse"):
        c, m, _, _ = bundle(data, tid)
        out = draft(c, m, trig("weather_heatwave", {"city": m["identity"]["city"], "temp_c": 44}, m["merchant_id"]))
        assert "44°C" in out.body and m["identity"]["city"] in out.body


def test_local_news_event_uses_payload(data):
    c, m, _, _ = bundle(data, "trg_010_ipl_match_delhi")
    out = draft(c, m, trig("local_news_event", {"headline": "Ring Road closed near Sant Nagar till 8pm for metro work"}, m["merchant_id"]))
    assert "Ring Road closed" in out.body and "delivery" in out.body.lower()


def test_trend_movement(data):
    c, m, _, _ = bundle(data, "trg_001_research_digest_dentists")
    out = draft(c, m, trig("category_trend_movement", {"query": "teeth whitening price", "delta_yoy": 0.41}))
    assert "41%" in out.body and "teeth whitening" in out.body.lower()


def test_completely_unknown_kind_surfaces_its_payload(data):
    c, m, _, _ = bundle(data, "trg_001_research_digest_dentists")
    out = draft(c, m, trig("google_policy_update", {"headline": "Google now shows 'accepts insurance' badge on clinic listings"}))
    assert "accepts insurance" in out.body


def test_unknown_kind_with_invented_number_is_blocked_from_the_payload_only(data):
    # numbers that *are* in the injected payload are allowed (they're grounded)
    c, m, _, _ = bundle(data, "trg_001_research_digest_dentists")
    out = draft(c, m, trig("footfall_alert", {"headline": "Lajpat Nagar market footfall", "footfall_delta_pct": -0.35}))
    assert "35%" in out.body


def test_surprise_customer_scope(data):
    c, m, t, _ = bundle(data, "trg_003_recall_due_priya")
    new_cust = {"customer_id": "c_new_ankit", "merchant_id": MEERA,
                "identity": {"name": "Ankit", "language_pref": "english", "age_band": "30-40"},
                "relationship": {"first_visit": "2025-10-01", "last_visit": "2026-04-20", "visits_total": 2, "services_received": ["cleaning"]},
                "state": "lapsed_soft", "preferences": {"preferred_slots": "weekday_evening", "reminder_opt_in": True},
                "consent": {"opted_in_at": "2025-10-01", "scope": ["recall_reminders"]}}
    out = draft(c, m, {**t, "customer_id": "c_new_ankit"}, new_cust)
    assert out.body.startswith("Hi Ankit") and out.send_as == "merchant_on_behalf"
    assert out.lang == "en" and "Priya" not in out.body
