"""Multi-turn reply handling.

Classification is rule-based and deterministic (fast, auditable, no LLM needed for
the cases that matter most: auto-replies, opt-outs, commitments). The LLM, when
configured, is only used to phrase answers to open questions — and its output is
validated like everything else.

Routing table (first match wins):
  auto_reply   canned WA-Business text, or the same text seen before from this merchant
               1st → one short owner-flag nudge, 2nd → wait 24h, 3rd → end
  opt_out      "stop", "not interested", "band karo"            → end, merchant suppressed
  hostile      abuse / "useless" without an explicit stop       → one apology + opt-out path; again → end
  off_topic    GST / tax / loans / legal …                      → decline in one line, redirect to the offer
  commit       "yes", "let's do it", "go ahead", "haan", "join" → action mode: send the deliverable, never re-qualify
  slot_pick    "1", "2", "Thu" (customer booking)               → confirm that slot
  defer        "later", "busy", "kal"                           → wait (sized to what they said)
  decline      "no", "nahi"                                     → end politely (no body)
  question     "?", how/what/kitna…                             → answer from context, re-offer
  info         anything else                                    → acknowledge specifically, move to the next step
"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .context import detect_english, detect_hinglish
from .textutil import normalize_msg

# ---------------------------------------------------------------------------- patterns

AUTO_REPLY = [
    r"thank(s| you) for (contacting|reaching|your (message|enquiry|inquiry)|messaging|writing)",
    r"(will|shall) (get back|respond|revert|reply|contact you)",
    r"our (team|executive|representative) will",
    r"(we are|we're) (currently )?(closed|unavailable|away|out of office)",
    r"business hours", r"working hours", r"out of (the )?office",
    r"automated (assistant|response|message|reply)", r"\bauto[- ]?reply\b", r"this is an automated",
    r"(i am|i'm) an? (automated|virtual) assistant",
    r"jaankari ke liye", r"team tak pahuncha", r"hum jald( hi)? (aapse )?(sampark|contact)",
    r"aapka (sandesh|message) (mil gaya|prapt)", r"we (have )?received your (message|query|request)",
]
OPT_OUT = [
    r"\bstop\b", r"\bunsubscribe\b", r"don'?t (message|text|contact|send|disturb)", r"do not (message|text|contact|send|disturb)",
    r"not interested", r"no interest", r"remove (me|my number)", r"leave me alone", r"never (message|contact)",
    r"band karo", r"mat bhejo", r"message mat", r"nahi chahiye", r"interest nahi", r"pareshan mat",
]
HOSTILE = [
    r"\bidiot\b", r"\bstupid\b", r"\buseless\b", r"\bspam\b", r"\bnonsense\b", r"\bbakwas\b", r"\bpagal\b",
    r"shut up", r"\bf+u+c+k", r"\bwtf\b", r"\bbloody\b", r"waste of (my )?time", r"\bfraud\b", r"\bscam\b",
    r"why are you (bothering|disturbing|messaging)", r"\bannoying\b", r"\bharass", r"\bchor\b", r"\bbekaar\b",
]
OFF_TOPIC = {
    "gst": r"\bgst\b|\bitr\b|income tax|\btax(es)? (filing|return)|file my tax",
    "loan": r"\bloan\b|\bcredit card\b|\bemi\b|\binsurance\b",
    "legal": r"\blawyer\b|\blegal\b|\bcourt\b|\bpolice\b|\bfir\b",
    "accounts": r"\baccounting\b|\bbookkeeping\b|\bbalance sheet\b|\bca\b ",
    "personal": r"\bvisa\b|\bpassport\b|\belectricity bill\b|\brecharge\b",
}
COMMIT = [
    r"\byes\b", r"\byeah\b", r"\byep\b", r"\byup\b", r"\bsure\b", r"\bok(ay)?\b", r"\bokk+\b", r"\bdone\b",
    r"go ahead", r"let'?s do it", r"lets do it", r"\bdo it\b", r"\bproceed\b", r"\bconfirm(ed)?\b", r"please do",
    r"send (it|me|the|over)", r"sounds good", r"\bperfect\b", r"\bagreed?\b", r"\bbook it\b", r"\bpublish\b", r"\bgo live\b",
    r"\bhaan\b", r"\bhan ji\b", r"\bji haan\b", r"\btheek hai\b", r"\bthik hai\b", r"\bchalo\b", r"\bkar do\b", r"\bkardo\b",
    r"\bbhej do\b", r"\bbhejo\b", r"\bchalega\b", r"\bbilkul\b", r"\bzaroor\b", r"\bstart\b", r"\bwhat'?s next\b", r"\bwhats next\b",
    r"\brenew\b", r"\bactivate\b", r"👍", r"✅",
]
JOIN = [r"\bjoin\b", r"\bjudna\b", r"\bjudrna\b", r"\bjudna hai\b", r"\bsign ?up\b", r"\bregister me\b", r"\bonboard"]
DEFER = {
    86400 * 7: [r"next week", r"agle hafte", r"after (a|one) week"],
    86400: [r"\btomorrow\b", r"\bkal\b", r"after diwali", r"\bnext day\b"],
    14400: [r"\blater\b", r"\bbusy\b", r"not now", r"\bbaad (mein|me)\b", r"abhi nahi", r"in a meeting", r"call (me )?later",
            r"\bwait\b", r"give me (some )?time", r"thodi der"],
}
DECLINE = [r"^\s*no\b", r"\bnope\b", r"\bnahi\b", r"\bnahin\b", r"\bna\b", r"no thanks", r"not now,? thanks", r"\bpass\b", r"maybe later"]
THANKS = [r"^\s*(thanks|thank you|thx|ty|shukriya|dhanyavaad)[\s!.🙏]*$", r"^\s*(great|nice|cool|awesome|good)[\s!.]*$"]
QUESTION_WORDS = r"^(what|how|why|when|where|which|who|can|could|will|would|is|are|does|do|kya|kaise|kab|kitna|kitne|kaun|kyun|kahan)\b"


@dataclass
class ConvState:
    conversation_id: str
    merchant_id: Optional[str]
    customer_id: Optional[str] = None
    trigger_id: Optional[str] = None
    kind: str = ""
    send_as: str = "vera"
    lang: str = "en"
    offer: str = ""
    on_accept: str = ""
    slots: list = field(default_factory=list)
    explain: str = ""
    owner: str = ""
    alt: dict = field(default_factory=dict)   # lang -> {"on_accept", "offer"}
    stage: str = "pitch"          # pitch | action | done | ended
    sent: list = field(default_factory=list)
    turns: list = field(default_factory=list)
    auto_count: int = 0
    hostile_count: int = 0
    unanswered: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class MerchantState:
    opted_out: bool = False
    auto_msgs: Counter = field(default_factory=Counter)
    auto_total: int = 0
    wait_until: float = 0.0
    hostile: bool = False


def _any(patterns, text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def classify(message: str, merchant: MerchantState, is_customer: bool = False) -> str:
    raw = message or ""
    low = raw.lower().strip()
    norm = normalize_msg(raw)
    if not norm and not re.search(r"[👍✅🙏]", raw):
        return "empty"
    if _any(AUTO_REPLY, low) or (len(norm) > 25 and merchant.auto_msgs.get(norm, 0) >= 1):
        return "auto_reply"
    if _any(OPT_OUT, low):
        return "opt_out"
    if _any(HOSTILE, low):
        return "hostile"
    for topic, pat in OFF_TOPIC.items():
        if re.search(pat, low):
            return f"off_topic:{topic}"
    if is_customer and re.fullmatch(r"\s*(option\s*)?[12]\s*[.!]?\s*", low):
        return "slot_pick"
    if is_customer and re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", low) and len(norm.split()) <= 6:
        return "slot_pick"
    if _any(JOIN, low):
        return "commit"
    for secs, pats in DEFER.items():
        if _any(pats, low) and not _any([r"\byes\b", r"let'?s do it", r"\bhaan\b"], low):
            return f"defer:{secs}"
    if _any(COMMIT, low):
        return "commit_question" if ("?" in low and not re.search(r"what'?s next|whats next|next\?", low)) else "commit"
    if _any(THANKS, low):
        return "thanks"
    if _any(DECLINE, low) and len(norm.split()) <= 6:
        return "decline"
    if "?" in low or re.search(QUESTION_WORDS, low):
        return "question"
    return "info"


class ConversationManager:
    def __init__(self, llm=None) -> None:
        self.convs: dict[str, ConvState] = {}
        self.merchants: dict[str, MerchantState] = defaultdict(MerchantState)
        self.llm = llm
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ lifecycle
    def open(self, state: ConvState, first_body: str) -> None:
        with self._lock:
            state.sent.append(first_body)
            state.turns.append({"from": "bot", "body": first_body, "ts": time.time()})
            self.convs[state.conversation_id] = state

    def merchant(self, merchant_id: Optional[str]) -> MerchantState:
        return self.merchants[merchant_id or "_unknown"]

    def is_blocked(self, merchant_id: str, now_ts: Optional[float] = None) -> bool:
        m = self.merchants.get(merchant_id)
        if not m:
            return False
        return m.opted_out or m.hostile or (m.wait_until and (now_ts or time.time()) < m.wait_until)

    def clear(self) -> None:
        with self._lock:
            self.convs.clear()
            self.merchants.clear()

    # ------------------------------------------------------------------ main entry
    def handle(self, conversation_id: str, merchant_id: Optional[str], customer_id: Optional[str],
               from_role: str, message: str, turn_number: int, fallback_state=None) -> dict:
        with self._lock:
            st = self.convs.get(conversation_id)
            if st is None:
                st = fallback_state(conversation_id, merchant_id, customer_id) if fallback_state else \
                    ConvState(conversation_id, merchant_id, customer_id)
                self.convs[conversation_id] = st
            ms = self.merchant(st.merchant_id or merchant_id)
            is_customer = from_role == "customer" or bool(st.customer_id)
            st.turns.append({"from": from_role, "body": message, "ts": time.time()})
            st.unanswered = 0
            self._update_language(st, message)
            self._localize(st)

            label = classify(message, ms, is_customer)
            norm = normalize_msg(message)
            if label == "auto_reply" or len(norm) > 25:
                ms.auto_msgs[norm] += 1

            if st.stage == "ended" and label not in ("question", "commit", "commit_question", "off_topic:gst",
                                                      "off_topic:loan", "off_topic:legal", "off_topic:accounts", "off_topic:personal"):
                return self._end(st, "Conversation already closed; not re-engaging on a non-substantive message.")
            if ms.opted_out and label != "commit":
                return self._end(st, "Merchant previously opted out; staying silent.")

            handler = {
                "auto_reply": self._auto_reply, "opt_out": self._opt_out, "hostile": self._hostile,
                "commit": self._commit, "commit_question": self._commit, "slot_pick": self._slot_pick,
                "thanks": self._thanks, "decline": self._decline, "question": self._question,
                "info": self._info, "empty": self._empty,
            }.get(label.split(":")[0])
            if label.startswith("off_topic"):
                return self._off_topic(st, message, label.split(":")[1])
            if label.startswith("defer"):
                return self._defer(st, int(label.split(":")[1]))
            return handler(st, message)

    # ------------------------------------------------------------------ handlers
    def _auto_reply(self, st: ConvState, message: str) -> dict:
        ms = self.merchant(st.merchant_id)
        st.auto_count += 1
        ms.auto_total += 1
        n = max(st.auto_count, ms.auto_msgs.get(normalize_msg(message), 0), 1)
        if n == 1:
            who = st.owner or ""
            offer = st.offer or self._t(st, "the update I mentioned", "jo update maine bataya")
            body = self._t(st,
                           f"Looks like an auto-reply — no problem. {who + ', when' if who else 'When'} you see this, just reply YES and I'll send {offer}.",
                           f"Lagta hai yeh auto-reply hai — koi baat nahi. {who + ', jab' if who else 'Jab'} aap dekhein, bas YES likhiye, main {offer} bhej dungi.")
            return self._send(st, body, "binary_yes_no",
                              "Detected WhatsApp Business auto-reply (canned phrasing). One short owner-flag nudge; will not repeat.")
        if n == 2:
            ms.wait_until = time.time() + 86400
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Same auto-reply again — the owner isn't at the phone. Backing off 24h instead of burning turns."}
        return self._end(st, f"Auto-reply {n}x in a row with no human reply; closing to avoid spamming the merchant's inbox.")

    def _opt_out(self, st: ConvState, message: str) -> dict:
        ms = self.merchant(st.merchant_id)
        ms.opted_out = True
        return self._end(st, "Merchant explicitly opted out. Closing and suppressing all further proactive messages to this merchant.")

    def _hostile(self, st: ConvState, message: str) -> dict:
        ms = self.merchant(st.merchant_id)
        st.hostile_count += 1
        if st.hostile_count >= 2 or ms.hostile:
            ms.hostile = True
            return self._end(st, "Repeated frustration; closing without further messages.")
        ms.hostile = True  # no new proactive sends; merchant can still pull
        body = self._t(st,
                       "Sorry — that wasn't useful, and I won't push it. If you'd rather I never message again, reply STOP; if something on your listing ever needs fixing, just ask.",
                       "Maaf kijiye — yeh kaam ka nahi tha, main zor nahi dungi. Kabhi message nahi chahiye toh STOP likhiye; listing mein kuch theek karna ho toh bas pooch lijiye.")
        return self._send(st, body, "none", "Merchant frustrated but did not explicitly opt out: one apology, a clear opt-out path, proactive sends paused.")

    def _off_topic(self, st: ConvState, message: str, topic: str) -> dict:
        who = {"gst": ("GST/tax filing is best handled by your CA", "GST/tax filing ke liye aapke CA sahi rahenge"),
               "loan": ("loans and insurance are outside what I can help with", "loan/insurance mere scope ke bahar hai"),
               "legal": ("legal questions need a lawyer, not me", "legal cheezon ke liye lawyer sahi rahenge"),
               "accounts": ("accounting is best left to your CA", "accounts ke liye CA sahi rahenge"),
               "personal": ("that's outside what I can help with", "yeh mere scope ke bahar hai")}[topic]
        back = st.offer and st.stage != "done"
        redirect_en = f" What I can do today: {st.offer}. Reply YES and it's done." if back else \
            " What I can help with: your Google listing, offers, posts and customer messages."
        redirect_hi = f" Jo main aaj kar sakti hoon: {st.offer}. YES likhiye, ho jayega." if back else \
            " Main Google listing, offers, posts aur customer messages mein madad kar sakti hoon."
        body = self._t(st, f"Honestly, {who[0]}.{redirect_en}", f"Seedhi baat — {who[1]}.{redirect_hi}")
        if st.stage == "ended":
            st.stage = "pitch"
        return self._send(st, body, "binary_yes_no" if back else "open_ended",
                          f"Out-of-scope request ({topic}) declined in one line without lecturing; redirected to the open offer.")

    def _commit(self, st: ConvState, message: str) -> dict:
        ms = self.merchant(st.merchant_id)
        ms.hostile = False
        ms.opted_out = False  # an explicit yes after a STOP is the merchant opting back in
        if st.stage == "ended":
            st.stage = "pitch"
        if st.customer_id and st.slots and st.stage == "pitch":
            if len(st.slots) == 1:
                return self._book(st, 0)
            options = " / ".join(f"{i + 1} for {s}" for i, s in enumerate(st.slots[:3]))
            body = self._t(st, f"Great! Which one works — {options}?", f"Badhiya! Kaunsa chalega — {options}?")
            return self._send(st, body, "multi_choice_slot", "Customer said yes but two slots were offered; one-line pick instead of guessing.")
        if st.stage in ("action", "done"):
            st.stage = "done"
            body = self._t(st, "Confirmed ✅ It's in motion. I'll report back here with the numbers — nothing else needed from you.",
                           "Confirmed ✅ Kaam chalu hai. Numbers ke saath yahin update dungi — aapko kuch aur nahi karna.")
            return self._send(st, body, "none", "Merchant re-confirmed after action; closing the loop with a report-back promise.")
        st.stage = "action"
        body = st.on_accept or self._default_action(st)
        if "?" in message and "next" not in message.lower():
            ans = self._answer(st, message)
            if ans:
                body = ans + "\n\n" + body
        return self._send(st, body, "binary_confirm_cancel" if "CONFIRM" in body else "none",
                          "Explicit commitment detected — switched from pitch to action immediately: delivering the promised artifact, no further qualifying questions.")

    def _slot_pick(self, st: ConvState, message: str) -> dict:
        low = message.lower()
        idx = 1 if re.search(r"\b2\b|second|dusra", low) else 0
        for i, s in enumerate(st.slots):
            day = s[:3].lower()
            if re.search(rf"\b{day}", low):
                idx = i
        if not st.slots:
            body = self._t(st, "Noted ✅ We'll confirm that time shortly.", "Noted ✅ Yeh time jaldi confirm karte hain.")
            return self._send(st, body, "none", "Customer proposed a time; acknowledged without inventing availability.")
        return self._book(st, min(idx, len(st.slots) - 1))

    def _book(self, st: ConvState, idx: int) -> dict:
        slot = st.slots[idx]
        st.stage = "done"
        body = self._t(st, f"Booked ✅ {slot}. We'll send a reminder the day before — reply here if anything changes.",
                       f"Booked ✅ {slot}. Ek din pehle reminder bhej denge — kuch badle toh yahin bata dijiye.")
        return self._send(st, body, "none", f"Customer chose a slot; confirmed '{slot}' exactly as offered.")

    def _thanks(self, st: ConvState, message: str) -> dict:
        if st.stage in ("action", "done"):
            st.stage = "done"
            return self._end(st, "Merchant acknowledged after the work was delivered; conversation complete.")
        body = self._t(st, f"Anytime. Whenever you're ready, reply YES and I'll send {st.offer or 'it'}.",
                       f"Zaroor. Jab ready hon, YES likhiye — {st.offer or 'bhej dungi'}.")
        return self._send(st, body, "binary_yes_no", "Polite acknowledgement without commitment; leaving the single CTA open.")

    def _decline(self, st: ConvState, message: str) -> dict:
        return self._end(st, "Merchant declined; exiting gracefully rather than re-pitching.")

    def _defer(self, st: ConvState, seconds: int) -> dict:
        self.merchant(st.merchant_id).wait_until = time.time() + seconds
        human = {604800: "a week", 86400: "a day", 14400: "4 hours"}.get(seconds, f"{seconds // 3600}h")
        return {"action": "wait", "wait_seconds": seconds,
                "rationale": f"Merchant asked for time; backing off {human} before following up."}

    def _question(self, st: ConvState, message: str) -> dict:
        ans = self._answer(st, message) or self._t(
            st, f"Good question. Short version: {st.explain or 'I handle the setup end to end — you just approve.'}",
            f"Achha sawaal. Short mein: {st.explain or 'setup main poora sambhal lungi — aapko bas approve karna hai.'}")
        tail = self._t(st, f" Want me to go ahead with {st.offer}? Reply YES.", f" {st.offer} ke saath aage badhoon? YES likhiye.") if st.offer and st.stage == "pitch" else ""
        return self._send(st, ans + tail, "binary_yes_no" if tail else "open_ended",
                          "Answered the merchant's question from the known context, then restated the single open CTA.")

    def _info(self, st: ConvState, message: str) -> dict:
        if st.kind in ("curious_ask_due", "scheduled_recurring") and st.stage == "pitch":
            topic = self._extract_topic(message)
            st.stage = "action"
            post = f"\"{topic.title()} at our studio this week — book on WhatsApp for a slot that suits you.\"" if topic else ""
            body = self._t(st,
                           f"Got it — {topic} it is. Here's the Google post draft:\n{post}\n\nAnd a WhatsApp reply for price enquiries: \"Thanks for asking! {topic.capitalize()} is available this week — want me to block a slot for you?\"\n\nPosting it tomorrow 10am unless you edit it.",
                           f"Samajh gayi — {topic}. Google post draft:\n{post}\n\nPrice enquiry ke liye WhatsApp reply: \"Poochne ke liye shukriya! {topic.capitalize()} is hafte available hai — slot block kar doon?\"\n\nKal subah 10am post kar dungi, jab tak aap edit na karein.") if topic else \
                self._t(st, "Got it, thanks — drafting the post and the reply template from that now; you'll have both here within the hour.",
                        "Samajh gayi — isi se post aur reply template bana rahi hoon, ek ghante mein yahin mil jayega.")
            return self._send(st, body, "none", "Merchant answered the curious-ask; turned their answer straight into the promised deliverables (reciprocity).")
        ans = self._llm_reply(st, message)
        if ans:
            return self._send(st, ans, "open_ended", "Contextual reply composed by LLM and validated.")
        body = self._t(st, f"Noted, thanks. Next step from my side: {st.offer or 'I will send a short plan'}. Reply YES and I'll get it done.",
                       f"Noted, shukriya. Mera next step: {st.offer or 'ek short plan bhejna'}. YES likhiye, kar deti hoon.")
        return self._send(st, body, "binary_yes_no", "Acknowledged the merchant's input and moved to the single next step.")

    def _empty(self, st: ConvState, message: str) -> dict:
        return {"action": "wait", "wait_seconds": 3600, "rationale": "Empty/unreadable reply; waiting for a real message."}

    # ------------------------------------------------------------------ helpers
    def _answer(self, st: ConvState, message: str) -> Optional[str]:
        low = message.lower()
        if re.search(r"how long|kitna time|kitne time|kab tak|when will|kitne din|how soon|kitni der", low):
            return self._t(st, "Drafts reach you here within the hour; anything on Google shows up after its usual 24-48h review.",
                           "Drafts ek ghante mein yahin; Google pe changes 24-48 ghante mein dikhte hain.")
        if re.search(r"price|cost|kitna|kitne|charge|fee|how much|paisa|rate", low):
            prices = re.findall(r"₹\s?[\d,]+", " ".join([st.explain, st.on_accept] + st.sent))
            if prices:
                return self._t(st, f"On price: the figures I'm working with are {', '.join(dict.fromkeys(prices))} — nothing extra from my side.",
                               f"Price ki baat: jo numbers main use kar rahi hoon woh {', '.join(dict.fromkeys(prices))} hain — meri taraf se kuch extra nahi.")
            return self._t(st, "There's no extra charge from me for this — it's part of your magicpin setup.",
                           "Iske liye meri taraf se koi extra charge nahi — yeh aapke magicpin setup ka hissa hai.")
        if re.search(r"who are you|kaun ho|what is vera|are you a bot|are you human", low):
            return self._t(st, "I'm magicpin's assistant — I look after your Google listing, offers and customer messages.",
                           "Main magicpin ki assistant hoon — Google listing, offers aur customer messages sambhalti hoon.")
        return self._llm_reply(st, message)

    def _llm_reply(self, st: ConvState, message: str) -> Optional[str]:
        if not self.llm:
            return None
        history = "\n".join(f"{t['from']}: {t['body']}" for t in st.turns[-6:])
        system = ("You are Vera, magicpin's WhatsApp assistant for Indian merchants. Reply in at most 3 short sentences. "
                  "Use only facts present in the conversation. Never invent numbers, names or prices. Do not re-introduce yourself. "
                  f"Language: {'Hindi-English code-mix (Roman script)' if st.lang != 'en' else 'English'}. "
                  "End with at most one question. Return JSON {\"body\": \"...\"}.")
        out = self.llm.complete_json(system, f"Conversation so far:\n{history}\n\nOpen offer: {st.offer}\nContext: {st.explain}", max_tokens=250, timeout=8)
        if not out or not isinstance(out.get("body"), str):
            return None
        body = out["body"].strip()
        known = " ".join(t["body"] for t in st.turns) + st.explain + st.on_accept
        known_nums = set(re.findall(r"\d[\d,]*", known))
        if any(n not in known_nums and int(n.replace(",", "") or 0) > 3 for n in re.findall(r"\d[\d,]*", body)):
            return None
        if re.search(r"\b(i am|i'm) vera\b|vera here", body, re.I):
            return None
        return body

    def _default_action(self, st: ConvState) -> str:
        return self._t(st,
                       "Done — starting now. Here's the plan: 1) I refresh your Google listing (hours, description, photos), 2) put your strongest offer at the top, 3) draft this week's post for your approval. First draft reaches you here within the hour — nothing else needed from you.",
                       "Ho gaya — abhi shuru kar rahi hoon. Plan: 1) Google listing refresh (hours, description, photos), 2) sabse strong offer top pe, 3) is hafte ka post approval ke liye. Pehla draft ek ghante mein yahin — aapko kuch aur nahi karna.")

    @staticmethod
    def _extract_topic(message: str) -> str:
        low = message.lower().strip().rstrip(".!")
        low = re.sub(r"^(mostly|mainly|probably|i think|it'?s|its|the|definitely|zyada(tar)?|sabse zyada)\s+", "", low)
        low = re.sub(r"\s+(hai|tha|was|this week|is hafte)$", "", low)
        words = low.split()
        return " ".join(words[:4]) if words and len(words) <= 6 else ""

    def _update_language(self, st: ConvState, message: str) -> None:
        if detect_hinglish(message):
            st.lang = "hinglish"
        elif detect_english(message):
            st.lang = "en"

    @staticmethod
    def _localize(st: ConvState) -> None:
        hindiish = ("hinglish", "hi")
        mat = st.alt.get(st.lang) or next((st.alt[l] for l in hindiish if st.lang in hindiish and l in st.alt), None)
        if mat:
            st.offer = mat.get("offer") or st.offer
            st.on_accept = mat.get("on_accept") or st.on_accept

    def _t(self, st: ConvState, en: str, hi: str) -> str:
        return hi if st.lang in ("hinglish", "hi") else en

    def _send(self, st: ConvState, body: str, cta: str, rationale: str) -> dict:
        body = body.strip()
        if body in st.sent:  # anti-repetition: never send the same text twice in a conversation
            body = self._t(st, "Just checking in on this — ", "Bas yaad dila rahi hoon — ") + body[0].lower() + body[1:]
            if body in st.sent:
                return {"action": "wait", "wait_seconds": 14400, "rationale": "Would have repeated a previous message; waiting instead."}
        st.sent.append(body)
        st.turns.append({"from": "bot", "body": body, "ts": time.time()})
        return {"action": "send", "body": body, "cta": cta, "rationale": rationale}

    def _end(self, st: ConvState, rationale: str) -> dict:
        st.stage = "ended"
        return {"action": "end", "rationale": rationale}
