import re

import pytest

from conftest import bundle
from vera.composer import Composer
from vera.context import Ctx
from vera.validator import validate

QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]
composer = Composer()


def all_trigger_ids(data):
    return list(data["triggers"])


def test_every_trigger_composes_cleanly(data):
    bodies = {}
    for tid in all_trigger_ids(data):
        c, m, t, cust = bundle(data, tid)
        out, ctx = composer.draft(c, m, t, cust)
        assert out.body.strip(), tid
        assert not out.issues, (tid, out.issues, out.body)
        assert out.send_as == ("merchant_on_behalf" if cust else "vera"), tid
        assert out.cta in {"binary_yes_no", "open_ended", "multi_choice_slot", "binary_confirm_cancel", "none"}
        assert out.suppression_key == t["suppression_key"]
        assert len(out.template_params) == 3
        bodies.setdefault(out.body, []).append(tid)
    dupes = {b: ids for b, ids in bodies.items() if len(ids) > 1}
    # identical inputs may legitimately produce identical bodies (same merchant+kind placeholder triggers)
    for b, ids in dupes.items():
        kinds = {data["triggers"][i]["kind"] for i in ids}
        merchants = {data["triggers"][i]["merchant_id"] for i in ids}
        assert len(kinds) == 1 and len(merchants) == 1, ids


def test_on_accept_is_action_not_qualification(data):
    for tid in all_trigger_ids(data):
        c, m, t, cust = bundle(data, tid)
        out, _ = composer.draft(c, m, t, cust)
        if not out.on_accept:
            continue
        low = out.on_accept.lower()
        assert not any(q in low for q in QUALIFYING), (tid, out.on_accept)


def test_on_accept_is_grounded(data):
    for tid in all_trigger_ids(data):
        c, m, t, cust = bundle(data, tid)
        out, ctx = composer.draft(c, m, t, cust)
        if out.on_accept:
            issues = [i for i in validate(out.on_accept, ctx, cta="none", is_reply=True, max_len=2000) if i.startswith("ungrounded")]
            assert not issues, (tid, issues, out.on_accept)


def test_deterministic(data):
    c, m, t, cust = bundle(data, "trg_001_research_digest_dentists")
    a = Composer().compose(c, m, t, cust)
    b = Composer().compose(c, m, t, cust)
    assert a.body == b.body


def test_research_digest_uses_cohort_and_citation(data):
    out, _ = composer.draft(*bundle(data, "trg_001_research_digest_dentists"))
    assert "JIDA Oct 2026, p.14" in out.body
    assert "2,100" in out.body and "38%" in out.body and "124" in out.body
    assert out.body.startswith("Dr. Meera")


def test_recall_slot_weekdays_come_from_iso_not_label(data):
    # payload labels say "Wed 5 Nov"/"Thu 6 Nov", but 2026-11-05 is a Thursday
    out, _ = composer.draft(*bundle(data, "trg_003_recall_due_priya"))
    assert "Thu 5 Nov, 6pm" in out.body and "Fri 6 Nov, 5pm" in out.body
    assert "Wed 5 Nov" not in out.body
    assert out.send_as == "merchant_on_behalf" and out.cta == "multi_choice_slot"
    assert "₹299" in out.body
    assert out.lang == "hinglish"


def test_competitor_placeholder_never_invents_a_name(data):
    tid = next(t for t, v in data["triggers"].items() if v["kind"] == "competitor_opened" and v["payload"].get("placeholder"))
    out, _ = composer.draft(*bundle(data, tid))
    assert "Smile Studio" not in out.body
    assert "a new" in out.body


def test_competitor_real_advises_not_to_price_match(data):
    out, _ = composer.draft(*bundle(data, "trg_023_competitor_opened_dentist"))
    assert "Smile Studio" in out.body and "₹199" in out.body and "1.3 km" in out.body
    assert re.search(r"match|Price match", out.body)


def test_ipl_saturday_judgement(data):
    out, _ = composer.draft(*bundle(data, "trg_010_ipl_match_delhi"))
    assert "delivery" in out.body.lower() and "12%" in out.body
    assert "Tue-Thu" in out.body  # knows its BOGO doesn't run today


def test_refill_cross_checks_recall(data):
    out, _ = composer.draft(*bundle(data, "trg_019_chronic_refill_grandfather"))
    assert all(mol in out.body for mol in ("metformin", "atorvastatin", "telmisartan"))
    assert "recall" in out.body.lower()
    assert out.lang == "hi" and "CONFIRM" in out.body


def test_perf_dip_without_decline_is_honest(data):
    out, _ = composer.draft(*bundle(data, "trg_031_perf_dip_m_023_sushma_salon_p"))
    assert "+8%" in out.body and "39" in out.body  # numbers holding; plan lapsed 39 days


def test_no_taboo_words_anywhere(data):
    for tid in all_trigger_ids(data):
        c, m, t, cust = bundle(data, tid)
        out, _ = composer.draft(c, m, t, cust)
        low = out.body.lower()
        for taboo in ["guaranteed", "miracle", "best in city", "100% safe"]:
            assert taboo not in low, (tid, taboo)


def test_customer_without_consent_is_detected(data):
    c, m, t, _ = bundle(data, "trg_021_unverified_gbp_sunrise")
    walk_in = data["customers"]["c_015_anonymous_for_m010"]
    assert Ctx(c, m, {**t, "scope": "customer", "kind": "recall_due"}, walk_in).consent_ok() is False


@pytest.mark.parametrize("tid", ["trg_001_research_digest_dentists", "trg_018_supply_atorvastatin_recall"])
def test_validator_rejects_invented_numbers(data, tid):
    c, m, t, cust = bundle(data, tid)
    ctx = Ctx(c, m, t, cust)
    assert any(i.startswith("ungrounded") for i in validate("Dr. X, 47 of your patients saw 83% gains. Reply YES.", ctx))
