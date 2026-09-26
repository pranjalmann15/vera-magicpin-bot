from conftest import bundle
from conversation_handlers import ConversationState, respond
from vera.conversation import ConversationManager, ConvState, classify, MerchantState

AUTO = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]


def fresh(kind="research_digest", offer="the study summary", on_accept="Done — summary ready.\n\nNext: queued."):
    mgr = ConversationManager()
    st = ConvState("c1", "m1", kind=kind, offer=offer, on_accept=on_accept, owner="Dr. Meera")
    mgr.open(st, "opener")
    return mgr


def test_classifier_basics():
    ms = MerchantState()
    assert classify(AUTO, ms) == "auto_reply"
    assert classify("Stop messaging me. This is useless spam.", ms) == "opt_out"
    assert classify("This is useless, why are you bothering me", ms) == "hostile"
    assert classify("Ok lets do it. Whats next?", ms) == "commit"
    assert classify("haan kar do", ms) == "commit"
    assert classify("Mujhe magicpin judrna hai", ms) == "commit"
    assert classify("Can you also help me file my GST?", ms) == "off_topic:gst"
    assert classify("busy right now, later", ms) == "defer:14400"
    assert classify("How much will it cost?", ms) == "question"
    assert classify("2", ms, is_customer=True) == "slot_pick"


def test_auto_reply_same_conversation_send_wait_end():
    mgr = fresh()
    r1 = mgr.handle("c1", "m1", None, "merchant", AUTO, 2)
    r2 = mgr.handle("c1", "m1", None, "merchant", AUTO, 3)
    r3 = mgr.handle("c1", "m1", None, "merchant", AUTO, 4)
    assert r1["action"] == "send" and "auto-reply" in r1["body"].lower()
    assert r2["action"] == "wait" and r2["wait_seconds"] >= 3600
    assert r3["action"] == "end"


def test_auto_reply_detected_across_conversations():
    # the local judge sends each auto-reply on a fresh conversation_id for the same merchant
    mgr = ConversationManager()
    acts = [mgr.handle(f"conv_auto_{i}", "m1", None, "merchant", "Thank you for contacting us! Our team will respond shortly.", i + 1)["action"]
            for i in range(1, 5)]
    assert acts[:3] == ["send", "wait", "end"]


def test_verbatim_repeat_is_auto_reply_even_without_known_phrasing():
    mgr = fresh()
    canned = "Namaste, Sharma Medicals mein aapka swagat hai, jaldi baat karenge"
    first = mgr.handle("c1", "m1", None, "merchant", canned, 2)
    second = mgr.handle("c1", "m1", None, "merchant", canned, 3)
    assert first["action"] == "send"
    assert second["action"] in ("send", "wait")
    assert "auto" in second.get("rationale", "").lower() or second["action"] == "wait"


def test_intent_transition_goes_straight_to_action():
    mgr = fresh()
    r = mgr.handle("c1", "m1", None, "merchant", "Ok, let's do it. What's next?", 3)
    assert r["action"] == "send"
    low = r["body"].lower()
    assert low.startswith("done")
    assert not any(q in low for q in QUALIFYING)


def test_opt_out_ends_and_blocks_merchant():
    mgr = fresh()
    assert mgr.handle("c1", "m1", None, "merchant", "Not interested. Stop messaging me.", 2)["action"] == "end"
    assert mgr.is_blocked("m1")


def test_hostile_then_gst_stays_polite_and_on_mission():
    mgr = fresh()
    r1 = mgr.handle("c1", "m1", None, "merchant", "Why are you bothering me. This is useless.", 2)
    assert r1["action"] == "send" and ("sorry" in r1["body"].lower())
    r2 = mgr.handle("c1", "m1", None, "merchant", "can you help me file my GST?", 3)
    assert r2["action"] == "send"
    assert "CA" in r2["body"] and "study summary" in r2["body"]


def test_defer_waits():
    mgr = fresh()
    r = mgr.handle("c1", "m1", None, "merchant", "Busy now, message me tomorrow", 2)
    assert r["action"] == "wait" and r["wait_seconds"] == 86400


def test_language_switch_mid_conversation():
    mgr = fresh()
    r = mgr.handle("c1", "m1", None, "merchant", "haan theek hai, kar do", 2)
    assert r["action"] == "send" and r["body"].startswith("Done")  # on_accept text is pre-rendered
    r2 = mgr.handle("c1", "m1", None, "merchant", "kitna time lagega?", 3)
    assert "ghant" in r2["body"]  # answered in Hinglish


def test_deliverable_follows_merchant_language(data):
    # Meera's opener is Hinglish (Delhi, hi); she replies in English → deliverable in English
    st = ConversationState.start(*bundle(data, "trg_022_cde_webinar_dentists"))
    r = respond(st, "Ok lets do it, please register me for this one")
    assert r["body"].startswith("Done —") and "Ho gaya" not in r["body"]
    st2 = ConversationState.start(*bundle(data, "trg_022_cde_webinar_dentists"))
    r2 = respond(st2, "haan kar do")
    assert r2["body"].startswith("Ho gaya")


def test_no_verbatim_repeat_in_a_conversation():
    mgr = fresh()
    bodies = [mgr.handle("c1", "m1", None, "merchant", "hmm what is this about?", i).get("body") for i in (2, 3, 4)]
    sent = [b for b in bodies if b]
    assert len(sent) == len(set(sent))


def test_customer_slot_booking_flow(data):
    st = ConversationState.start(*bundle(data, "trg_003_recall_due_priya"))
    r = respond(st, "2")
    assert r["action"] == "send" and "Fri 6 Nov, 5pm" in r["body"] and "Booked" in r["body"]


def test_customer_yes_with_two_slots_asks_which(data):
    st = ConversationState.start(*bundle(data, "trg_003_recall_due_priya"))
    r = respond(st, "yes")
    assert r["cta"] == "multi_choice_slot" and "Thu 5 Nov" in r["body"]


def test_curious_ask_answer_becomes_deliverable(data):
    st = ConversationState.start(*bundle(data, "trg_008_curious_ask_studio11"))
    r = respond(st, "Mostly balayage this week")
    assert r["action"] == "send" and "balayage" in r["body"].lower()


def test_thanks_after_action_ends_gracefully(data):
    st = ConversationState.start(*bundle(data, "trg_022_cde_webinar_dentists"))
    assert respond(st, "yes please register")["action"] == "send"
    assert respond(st, "thanks")["action"] == "end"
