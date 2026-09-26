import json

import pytest
from fastapi.testclient import TestClient

from conftest import EXPANDED


@pytest.fixture()
def client():
    from vera import app as app_module
    app_module.engine.teardown()
    return TestClient(app_module.app)


def push(client, scope, cid, payload, version=1):
    return client.post("/v1/context", json={"scope": scope, "context_id": cid, "version": version,
                                            "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})


def load_all(client, data):
    for slug, c in data["categories"].items():
        assert push(client, "category", slug, c).status_code == 200
    for mid, m in data["merchants"].items():
        assert push(client, "merchant", mid, m).status_code == 200
    for cid, c in data["customers"].items():
        assert push(client, "customer", cid, c).status_code == 200


def test_health_and_metadata(client):
    h = client.get("/v1/healthz").json()
    assert h["status"] == "ok" and h["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    md = client.get("/v1/metadata").json()
    for k in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"):
        assert k in md


def test_context_versioning(client, data):
    m = data["merchants"]["m_001_drmeera_dentist_delhi"]
    assert push(client, "merchant", m["merchant_id"], m, 1).json()["accepted"] is True
    dup = push(client, "merchant", m["merchant_id"], m, 1)
    assert dup.status_code == 200 and dup.json().get("idempotent") is True
    changed = {**m, "performance": {**m["performance"], "views": 2580}}
    assert push(client, "merchant", m["merchant_id"], changed, 1).status_code == 409
    assert push(client, "merchant", m["merchant_id"], changed, 2).json()["accepted"] is True
    stale = push(client, "merchant", m["merchant_id"], m, 1)
    assert stale.status_code == 409 and stale.json() == {"accepted": False, "reason": "stale_version", "current_version": 2}
    bad = client.post("/v1/context", json={"scope": "planet", "context_id": "x", "version": 1, "payload": {}})
    assert bad.status_code == 400 and bad.json()["reason"] == "invalid_scope"
    assert client.post("/v1/context", json={"scope": "merchant"}).status_code == 400


def test_warmup_counts(client, data):
    load_all(client, data)
    assert client.get("/v1/healthz").json()["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}


def test_full_tick_flow(client, data):
    load_all(client, data)
    tids = list(data["triggers"])
    for tid in tids:
        push(client, "trigger", tid, data["triggers"][tid])
    seen_keys, all_actions = set(), []
    for _ in range(8):  # triggers stay active across ticks, like the real harness
        r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": tids})
        assert r.status_code == 200
        acts = r.json()["actions"]
        assert len(acts) <= 20
        merchant_facing = [a["merchant_id"] for a in acts if not a["customer_id"]]
        assert len(merchant_facing) == len(set(merchant_facing)), "one merchant-facing message per merchant per tick"
        for a in acts:
            for k in ("conversation_id", "merchant_id", "send_as", "trigger_id", "template_name", "template_params",
                      "body", "cta", "suppression_key", "rationale"):
                assert a.get(k) not in (None, ""), k
            assert a["suppression_key"] not in seen_keys
            seen_keys.add(a["suppression_key"])
        all_actions += acts
    assert len(all_actions) >= 90  # everything with consent eventually goes out, no duplicates
    # an empty tick is valid
    assert client.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": []}).json() == {"actions": []}


def test_tick_skips_no_consent_customer(client, data):
    load_all(client, data)
    t = {**data["triggers"]["trg_003_recall_due_priya"], "id": "trg_x", "customer_id": "c_015_anonymous_for_m010",
         "merchant_id": "m_010_sunrisepharm_pharmacy_lucknow", "suppression_key": "x"}
    push(client, "trigger", "trg_x", t)
    assert client.post("/v1/tick", json={"available_triggers": ["trg_x"]}).json() == {"actions": []}


def test_mojibake_payload_is_repaired(client, data):
    cat = json.loads(json.dumps(data["categories"]["restaurants"], ensure_ascii=False).encode("utf-8").decode("cp1252", errors="ignore"))
    m = data["merchants"]["m_005_pizzajunction_restaurant_delhi"]
    push(client, "category", "restaurants", cat)
    push(client, "merchant", m["merchant_id"], m)
    push(client, "trigger", "trg_010_ipl_match_delhi", data["triggers"]["trg_010_ipl_match_delhi"])
    acts = client.post("/v1/tick", json={"available_triggers": ["trg_010_ipl_match_delhi"]}).json()["actions"]
    assert acts and "â" not in acts[0]["body"] and "₹399" in acts[0]["body"]


def test_reply_contract_and_replay_scenarios(client, data):
    load_all(client, data)
    push(client, "trigger", "trg_022_cde_webinar_dentists", data["triggers"]["trg_022_cde_webinar_dentists"])
    act = client.post("/v1/tick", json={"available_triggers": ["trg_022_cde_webinar_dentists"]}).json()["actions"][0]
    conv = act["conversation_id"]

    def say(msg, turn, cid=conv, mid="m_001_drmeera_dentist_delhi"):
        return client.post("/v1/reply", json={"conversation_id": cid, "merchant_id": mid, "from_role": "merchant",
                                              "message": msg, "received_at": "2026-04-26T10:42:00Z", "turn_number": turn}).json()

    r = say("Yes please register me", 2)
    assert r["action"] == "send" and r["body"] and r["rationale"]
    # unknown conversation + commitment → action with real content for that merchant
    r = say("Ok lets do it. Whats next?", 2, cid="conv_intent_1")
    assert r["action"] == "send" and not any(q in r["body"].lower() for q in ("would you", "do you", "can you tell", "what if", "how about"))
    # hostile → end
    assert say("Stop messaging me. This is useless spam.", 2, cid="conv_hostile")["action"] == "end"
