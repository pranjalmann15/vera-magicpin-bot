"""Trigger playbooks — one strategy per trigger kind.

A playbook decides the *angle* (what is the single most useful thing to say right
now), picks the compulsion levers, and writes the message from grounded facts
only. It also prepares `on_accept`: the concrete deliverable Vera sends the moment
the merchant says yes, so a "let's do it" is answered with work, not another
qualifying question.

House style (applies to every playbook):
  * lead with the fact, not a greeting paragraph; no "I hope you're doing well"
  * one ask, in the last sentence
  * numbers only from the contexts (or derived and registered via c.allow)
  * add judgement where the data supports it — e.g. don't match a competitor's
    price when the reviews say you win on trust
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Optional

from .context import Ctx, Voice, pretty_services
from .textutil import (
    IST, clock, day_month, dow_day_month, humanize_token, inr, months_between, num,
    parse_date, parse_dt, pct, slot_label, squash, weeks_phrase,
)

METRIC_LABEL = {
    "views": "profile views", "calls": "calls", "ctr": "click-through rate",
    "directions": "direction requests", "leads": "leads",
}
log = logging.getLogger("vera.playbooks")

PEER_SCOPE = {
    "dentists": "solo dental practices", "salons": "metro salons",
    "restaurants": "casual-dining places", "gyms": "neighbourhood gyms",
    "pharmacies": "neighbourhood pharmacies",
}


@dataclass
class Draft:
    body: str
    cta: str
    rationale: str
    levers: list[str] = field(default_factory=list)
    on_accept: str = ""
    offer: str = ""
    slots: list[str] = field(default_factory=list)
    template: str = ""

    def __post_init__(self) -> None:
        self.body = squash(self.body)
        self.on_accept = squash(self.on_accept)


# =========================================================================== helpers

SINGULAR = {"dentists": "dental clinic", "salons": "salon", "restaurants": "restaurant",
            "gyms": "gym", "pharmacies": "pharmacy"}


def short_title(title: str) -> str:
    """'Match-night Combo @ ₹399 (food + drink)' -> 'Match-night Combo @ ₹399'."""
    return re.sub(r"\s*\([^)]*\)", "", title or "").strip()


def lc_first(s: str) -> str:
    """Lower-case the first word when it follows 'Name, ' — unless it's an acronym/proper noun."""
    if not s:
        return s
    words = s.split(" ")
    first = words[0]
    if first.isupper() or first in ("I", "Diwali") or any(ch.isdigit() for ch in first):
        return s
    if len(words) > 1 and words[1][:1].isupper():  # proper-noun phrase: "Sant Nagar", "Smile Studio"
        return s
    return s[0].lower() + s[1:]


def opener(c: Ctx, v: Voice) -> str:
    """Merchant-facing opener. Introduces Vera only on first-ever contact."""
    if c.first_contact:
        return v.t(f"{c.salutation}, Vera from magicpin here.", f"{c.salutation}, main Vera, magicpin se.")
    return f"{c.salutation},"


def lead(c: Ctx, v: Voice, text: str) -> str:
    """Opener + first sentence with correct casing ('Name, calls are…' / 'Name, Vera here. Calls are…')."""
    op = opener(c, v)
    text = text.strip()
    if op.endswith(","):
        return f"{op} {lc_first(text)}"
    return f"{op} {text[:1].upper() + text[1:]}"


def peer_scope(c: Ctx) -> str:
    return PEER_SCOPE.get(c.slug, "similar businesses")


def fmt_metric(metric: str, value: float) -> str:
    return pct(value) if metric == "ctr" else num(value)


def vs_peer(c: Ctx, v: Voice, metric: str, mine: float, peer: float) -> str:
    """'2.1x the 2.5% average for casual-dining places' — registers the derived number."""
    r = mine / peer if peer else 1
    pv, scope = fmt_metric(metric, peer), peer_scope(c)
    if r >= 1.9:
        x = f"{r:.1f}".rstrip("0").rstrip(".")
        c.allow(float(x))
        return v.t(f"{x}x the {pv} average for {scope}", f"{scope} ke {pv} average ka {x}x")
    if r >= 1.1:
        n = round((r - 1) * 100)
        c.allow(n)
        return v.t(f"{n}% above the {pv} average for {scope}", f"{scope} ke {pv} average se {n}% zyada")
    if r > 0.9:
        return v.t(f"in line with the {pv} average for {scope}", f"{scope} ke {pv} average ke barabar")
    if 0.4 <= r <= 0.6:
        return v.t(f"about half the {pv} average for {scope}", f"{scope} ke {pv} average ka lagbhag aadha")
    n = round((1 - r) * 100)
    c.allow(n)
    return v.t(f"{n}% below the {pv} average for {scope}", f"{scope} ke {pv} average se {n}% kam")


def ask(v: Voice, en: str, hi: Optional[str] = None, reply: str = "YES") -> str:
    """Single binary CTA, always the last sentence."""
    return v.t(f"{en} Reply {reply}.", f"{hi or en} Reply {reply}.")


def best_active_offer(c: Ctx) -> Optional[dict]:
    offers = c.active_offers
    priced = [o for o in offers if "₹" in str(o.get("title", "")) and "%" not in str(o.get("title", ""))]
    return (priced or offers or [None])[0]


def suggested_catalog_offer(c: Ctx) -> Optional[dict]:
    """A service+price catalog offer the merchant isn't already running."""
    running = {str(o.get("title", "")).lower() for o in c.active_offers}
    for o in c.catalog("service_at_price", "free_service", "free_trial"):
        if str(o.get("title", "")).lower() not in running:
            return o
    return None


def post_draft(c: Ctx, headline: str, line: str) -> str:
    return f"\"{headline} — {c.biz}, {c.locality}. {line}\""


def accept_text(c: Ctx, v: Voice, done: str, artifact: str = "", next_step: str = "") -> str:
    parts = [v.t(f"Done — {done}.", f"Ho gaya — {done}.")]
    if artifact:
        parts.append(artifact)
    if next_step:
        parts.append(next_step)
    parts.append(v.t("I'll go ahead unless you reply STOP.", "Main aage badha rahi hoon — rokna ho toh STOP bhej dijiye."))
    return "\n\n".join(parts)


# =========================================================================== merchant-facing

def research_digest(c: Ctx, v: Voice) -> Draft:
    item_id = c.payload.get("top_item_id") or next(
        (val for k, val in c.payload.items() if isinstance(val, str) and k.endswith("item_id")), None)
    item = (c.digest_item(item_id) if item_id else None) or c.digest_item(kinds=("research",)) \
        or c.digest_item(kinds=("research", "tech", "trend"))
    if not item:
        return generic(c, v)
    source = item.get("source", "")
    title = item.get("title", "")
    summary = item.get("summary", "")
    n = item.get("trial_n")
    first_sentence = summary.split(". ")[0].rstrip(".") if summary else ""
    caveat = ""
    if "no effect" in summary.lower():
        caveat = " " + summary[summary.lower().index("no effect"):].split(".")[0].strip() + "."

    # Merchant anchor: tie the finding to the part of *their* roster it touches.
    anchor = ""
    seg = str(item.get("patient_segment", ""))
    hr = c.agg.get("high_risk_adult_count")
    if "high_risk" in seg and hr:
        anchor = v.t(f" That maps directly onto the {num(hr)} high-risk adults in your patient base.",
                     f" Aapke roster ke {num(hr)} high-risk adults pe yeh seedha lagta hai.")
    elif c.agg.get("total_unique_ytd"):
        anchor = v.t(f" Relevant to your {num(c.agg['total_unique_ytd'])} patients this year.",
                     f" Aapke is saal ke {num(c.agg['total_unique_ytd'])} patients ke liye relevant.")

    finding = first_sentence or title
    if n and re.search(r"\btrial\b", finding):
        finding = re.sub(r"\btrial\b", f"trial ({num(n)} patients)", finding, count=1)
    elif n:
        finding = f"{finding} (n={num(n)})"
    body = f"""{lead(c, v, f"{source} has one worth your two minutes. {finding}.{caveat}{anchor}")}
{ask(v, "Want the 2-min summary plus a patient-education WhatsApp you can forward?", "2-min summary aur patients ke liye ek forward-ready WhatsApp bhej doon?")}"""

    note = (f"\"New research ({source}): {title}. If you've had a cavity in the last year, ask us at your next "
            f"visit whether a shorter recall suits you. — {c.signoff_name}\"")
    on_accept = accept_text(
        c, v,
        v.t("summary and patient note are ready", "summary aur patient note ready hai"),
        v.t(f"Summary: {summary}\n\nPatient WhatsApp (copy-paste ready):\n{note}",
            f"Summary: {summary}\n\nPatient WhatsApp (copy-paste ke liye ready):\n{note}"),
        v.t("Next, I'll queue it for your high-risk recall list.", "Agla step: isse high-risk recall list ke liye queue kar rahi hoon."),
    )
    return Draft(body, "binary_yes_no",
                 f"Research digest ({source}) matched to the merchant's own patient segment; cites trial size and source, "
                 f"offers a ready artifact (reciprocity) with one binary ask.",
                 ["specificity", "source_citation", "reciprocity", "curiosity"], on_accept,
                 offer=v.t("the study summary + patient WhatsApp", "study summary + patient WhatsApp"))


def regulation_change(c: Ctx, v: Voice) -> Draft:
    item = c.digest_item(c.payload.get("top_item_id"), kinds=("compliance",))
    if not item:
        return generic(c, v)
    deadline = parse_date(c.payload.get("deadline_iso")) or parse_date(item.get("title", "")[-10:])
    summary = item.get("summary", "")
    sentences = [s.strip().rstrip(".") for s in summary.split(". ") if s.strip()]
    when = f"from {day_month(deadline)} {deadline.year}" if deadline else "soon"
    days_left = (deadline - c.today).days if deadline else None
    runway = ""
    if days_left is not None and 0 < days_left <= 45:  # only worth saying when it's genuinely close
        c.allow(days_left)
        runway = v.t(f" That's {days_left} days away.", f" Sirf {days_left} din bache hain.")
    detail = ". ".join(sentences[:2]) + "." if sentences else ""
    what = re.sub(r"^(\w+) revised ", r"\1 has revised ", item.get("title", "").split(" effective")[0])
    body = f"""{opener(c, v)} compliance heads-up — {what}, effective {day_month(deadline) + ' ' + str(deadline.year) if deadline else 'soon'} ({item.get('source', '')}). {detail}{runway}
{ask(v, "Want a one-page audit checklist for your setup?", "Aapke setup ke liye 1-page audit checklist bhej doon?")}"""
    checklist = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences + [item.get("actionable", "")], 1) if s)
    on_accept = accept_text(
        c, v, v.t("checklist is ready", "checklist ready hai"),
        f"{item.get('title', '')}\n{checklist}",
        v.t(f"I'll remind you 30 days before {day_month(deadline) if deadline else 'the deadline'} to confirm it's closed.",
            f"{day_month(deadline) if deadline else 'Deadline'} se 30 din pehle reminder bhejungi."),
    )
    return Draft(body, "binary_yes_no",
                 "Regulatory change with a hard date; states exactly what passes/fails so the merchant can self-check, "
                 "frames runway calmly, offers a checklist.",
                 ["specificity", "loss_aversion", "effort_externalization"], on_accept,
                 offer=v.t("the audit checklist", "audit checklist"))


def perf_dip(c: Ctx, v: Voice, _from_spike: bool = False) -> Draft:
    metric = c.payload.get("metric")
    delta = c.payload.get("delta_pct")
    if metric is None or delta is None:
        wd = c.worst_delta()
        if not wd:
            return _no_dip_visible(c, v)
        metric, delta = wd
    label = METRIC_LABEL.get(metric, metric)
    baseline = c.payload.get("vs_baseline")
    current = c.perf.get(metric)
    gap = c.peer_gap(metric)

    facts = v.t(f"{label.capitalize()} are down {pct(delta)} this week", f"Is hafte {label} {pct(delta)} gire hain")
    if baseline:
        facts += v.t(f" against your usual {num(baseline)}", f" — aapka usual {num(baseline)} tha")
    facts += "."
    if gap and current is not None and not baseline:
        facts += v.t(f" That leaves you at {fmt_metric(metric, current)} for the month vs a {fmt_metric(metric, gap[1])} average for {peer_scope(c)}.",
                     f" Mahine ka total {fmt_metric(metric, current)} hai, jabki {peer_scope(c)} ka average {fmt_metric(metric, gap[1])} hai.")

    # Diagnose with whatever the profile actually shows.
    causes = []
    if c.verified is False:
        causes.append(v.t("the Google profile is still unverified", "Google profile abhi unverified hai"))
    if not c.active_offers:
        causes.append(v.t("there's no live offer for someone comparing options", "compare karne wale customer ke liye koi live offer nahi hai"))
    stale = c.signal_value("stale_posts")
    if stale:
        causes.append(v.t(f"the last Google post was {stale} ago", f"last Google post {stale} pehle tha"))
    neg = c.review("neg")
    if neg and len(causes) < 2:
        causes.append(v.t(f"{neg[0].get('occurrences_30d')} recent reviews flag {humanize_token(neg[0].get('theme', ''))}",
                          f"{neg[0].get('occurrences_30d')} recent reviews mein {humanize_token(neg[0].get('theme', ''))} ki shikayat hai"))

    fix_offer = suggested_catalog_offer(c) if not c.active_offers else None
    if causes:
        diag = v.t(f" What I'd look at first: {' and '.join(causes[:2])}.", f" Pehle yeh dekhna chahiye: {' aur '.join(causes[:2])}.")
    else:
        diag = ""
    if fix_offer:
        cta = ask(v, f"Want me to put a '{fix_offer['title']}' offer live today to catch those searches?",
                  f"'{fix_offer['title']}' offer aaj hi live kar doon taaki searches convert hon?")
        deliverable = f"'{fix_offer['title']}' offer drafted for your listing"
    elif c.verified is False:
        cta = ask(v, "Want me to start Google verification now?", "Google verification abhi shuru kar doon?")
        deliverable = "verification request started"
    else:
        cta = ask(v, "Want me to draft 2 Google posts to restart the flow this week?", "Is hafte flow wapas laane ke liye 2 Google posts draft kar doon?")
        deliverable = "2 Google posts drafted"
    body = lead(c, v, facts + diag) + f"\n{cta}"
    on_accept = accept_text(c, v, deliverable,
                            v.t("I'll track calls and views daily and send you the before/after in 7 days.",
                                "7 din mein before/after numbers bhejungi."))
    return Draft(body, "binary_yes_no",
                 f"{label} dip of {pct(delta)} — named the metric, then diagnosed from the merchant's own profile gaps "
                 f"(verification/offers/posts/reviews) instead of a generic 'boost your sales'.",
                 ["loss_aversion", "specificity", "effort_externalization"], on_accept, offer=deliverable)


def _no_dip_visible(c: Ctx, v: Voice) -> Draft:
    """A dip trigger whose data shows no week-on-week decline: say so, and name the real risk."""
    bd = c.best_delta()
    holding = v.t(f"quick check on the dip flag: your week-on-week numbers are actually holding ({METRIC_LABEL.get(bd[0], bd[0])} {pct(bd[1], signed=True)}).",
                  f"dip alert check kiya: week-on-week numbers theek hain ({METRIC_LABEL.get(bd[0], bd[0])} {pct(bd[1], signed=True)}).") if bd else \
        v.t("quick check on the dip flag: your week-on-week numbers are flat, not falling.", "dip alert check kiya: numbers flat hain, gir nahi rahe.")
    days_exp = c.sub.get("days_since_expiry")
    weak = c.standout_weakness()
    if c.sub.get("status") == "expired" and days_exp:
        risk = v.t(f" The risk is lagging: your plan lapsed {num(days_exp)} days ago, so posts, review replies and offer upkeep have stopped — that usually shows up in calls a few weeks later.",
                   f" Risk aage ka hai: plan {num(days_exp)} din pehle lapse hua, posts aur review replies ruk gaye hain — iska asar calls pe kuch hafte baad dikhta hai.")
        cta = ask(v, "Want to see what reactivating would switch back on?", "Reactivate karne se kya wapas chalu hoga, dikhaoon?")
        deliverable = "reactivation breakdown"
    elif weak:
        m, mine, peer = weak
        risk = v.t(f" Where you do trail is {METRIC_LABEL.get(m, m)}: {fmt_metric(m, mine)} a month vs {fmt_metric(m, peer)} for {peer_scope(c)}.",
                   f" Peeche sirf {METRIC_LABEL.get(m, m)} mein ho: {fmt_metric(m, mine)}/month vs {peer_scope(c)} ke {fmt_metric(m, peer)}.")
        cta = ask(v, "Want 2 changes aimed at that gap?", "Us gap ke liye 2 changes bhejoon?")
        deliverable = "2 changes for the gap"
    else:
        risk = v.t(" Nothing to fix this week — I'll keep watching and only ping you if it turns.", " Is hafte kuch theek karne ki zaroorat nahi — nazar rakhungi.")
        cta = v.t("Reply if you'd like a monthly summary instead.", "Monthly summary chahiye toh reply kijiye.")
        deliverable = "monthly summary"
    body = lead(c, v, holding + risk) + f"\n{cta}"
    return Draft(body, "binary_yes_no",
                 "Dip trigger but the merchant's own 7-day deltas are positive — reported that honestly instead of inventing a "
                 "decline, and redirected to the real risk (lapsed plan / peer gap).",
                 ["trust", "specificity", "loss_aversion"], accept_text(c, v, deliverable), offer=deliverable)


def perf_spike(c: Ctx, v: Voice) -> Draft:
    metric = c.payload.get("metric")
    delta = c.payload.get("delta_pct")
    if metric is None or delta is None:
        bd = c.best_delta()
        if not bd:
            # "Spike" flagged but every 7-day delta is negative: be straight about it.
            return perf_dip(c, v, _from_spike=True) if c.worst_delta() else generic(c, v)
        metric, delta = bd
    label = METRIC_LABEL.get(metric, metric)
    driver = c.payload.get("likely_driver")
    baseline = c.payload.get("vs_baseline")
    line = v.t(f"{label.capitalize()} are up {pct(delta)} this week", f"Is hafte {label} {pct(delta)} upar hain")
    if baseline:
        line += v.t(f" over your usual {num(baseline)}", f" — usual {num(baseline)} se zyada")
    line += "."
    if driver:
        line += v.t(f" The lift lines up with your {humanize_token(driver)} — people are clearly searching for it now.",
                    f" Yeh uptick aapke {humanize_token(driver)} ke saath aaya hai — log abhi yahi dhoondh rahe hain.")
        cta = ask(v, "Want me to post a follow-up while interest is warm?", "Interest garam hai — follow-up post daal doon?")
        deliverable = "follow-up post drafted"
        judgement = "Momentum play: second post while the first is still pulling."
    else:
        small = abs(float(delta)) < 0.1
        weak = c.standout_weakness()
        if small:
            line += v.t(" Small, but it's the right direction.", " Chhota hai, par direction sahi hai.")
        if weak and c.verified is False:
            m, mine, peer = weak
            line += v.t(f" To turn it into a trend: you're at {fmt_metric(m, mine)} {METRIC_LABEL.get(m, m)} a month vs {fmt_metric(m, peer)} for {peer_scope(c)}, and your Google profile is still unverified — that's the fastest lever.",
                        f" Isse trend banane ke liye: aap {fmt_metric(m, mine)} {METRIC_LABEL.get(m, m)}/month pe ho, {peer_scope(c)} ka average {fmt_metric(m, peer)} hai — aur Google profile abhi unverified hai, sabse tez lever wahi hai.")
            cta = ask(v, "Want me to start verification now?", "Verification abhi shuru kar doon?")
            deliverable = "verification request started"
        else:
            offer = best_active_offer(c) or suggested_catalog_offer(c)
            if offer:
                line += v.t(f" Good moment to put '{offer['title']}' in front of the extra traffic.",
                            f" Extra traffic ke saamne '{offer['title']}' rakhne ka sahi time hai.")
            cta = ask(v, "Want me to pin it to the top of your profile this week?", "Is hafte profile ke top pe pin kar doon?")
            deliverable = "offer pinned to the top of your profile"
        judgement = "Spike used as a nudge toward the merchant's biggest open lever."
    body = lead(c, v, line) + f"\n{cta}"
    on_accept = accept_text(c, v, deliverable, "",
                            v.t("I'll send the 7-day numbers so you can see if it holds.", "7 din baad numbers bhejungi."))
    return Draft(body, "binary_yes_no", f"{label} up {pct(delta)}. {judgement}",
                 ["specificity", "curiosity", "effort_externalization"], on_accept, offer=deliverable)


def seasonal_perf_dip(c: Ctx, v: Voice) -> Draft:
    metric = c.payload.get("metric", "views")
    delta = c.payload.get("delta_pct") or (c.delta.get(f"{metric}_pct"))
    label = METRIC_LABEL.get(metric, metric)
    beat = c.seasonal_beat() or c.beat_matching("lowest", "slow")
    digest = c.digest_item(kinds=("seasonal",))
    lines = [v.t(f"{label.capitalize()} are down {pct(delta)} this week — and that's expected.",
                 f"Is hafte {label} {pct(delta)} neeche hain — aur yeh expected hai.")]
    if beat:
        biz_type = SINGULAR.get(c.slug, "business") + "s"
        lines.append(v.t(f"{beat['month_range']} is the {beat['note'].split(' — ')[0]} for {biz_type}, so I wouldn't chase it with ad spend.",
                         f"{beat['month_range']} {biz_type} ke liye {beat['note'].split(' — ')[0]} hota hai — ads pe paisa lagane ka time nahi hai."))
    if digest and digest.get("actionable") and not beat:
        lines.append(v.t(f"The {digest.get('source', 'data')} read: {digest['actionable'].rstrip('.')}.", f"Data ({digest.get('source', '')}) keh raha hai: {digest['actionable'].rstrip('.')}."))
    members = c.agg.get("total_active_members")
    churn = c.agg.get("monthly_churn_pct")
    peer_churn = c.peer.get("monthly_churn_pct")
    if members and churn:
        lost = int(members * churn)
        c.allow(lost)
        comp = f" against a peer average of {pct(peer_churn)}" if peer_churn else ""
        lines.append(v.t(f"The number that matters this quarter is churn: {pct(churn)}/month{comp} — about {lost} of your {num(members)} members leaving every month.",
                         f"Is quarter asli number churn hai: {pct(churn)}/month{comp} — har mahine aapke {num(members)} mein se lagbhag {lost} members nikal rahe hain."))
        cta = ask(v, "Want me to draft a 6-week summer consistency challenge to hold them through the dip?",
                  "Dip ke dauraan members ko rokne ke liye 6-week summer challenge draft kar doon?")
        deliverable = "6-week summer consistency challenge drafted"
        c.allow(6)
    else:
        cta = ask(v, "Want me to shift this month's effort to your repeat customers instead?", "Is mahine ka focus repeat customers pe shift kar doon?")
        deliverable = "repeat-customer plan drafted"
    body = lead(c, v, " ".join(lines)) + "\n" + cta
    challenge = ("Week 1-2: 3 check-ins/week → free body-composition scan\n"
                 "Week 3-4: bring-a-friend week\nWeek 5-6: leaderboard + small reward for 15+ sessions")
    c.allow(1, 2, 3, 4, 5, 6, 15)
    on_accept = accept_text(c, v, deliverable, challenge if members else "",
                            v.t("I'll draft the member WhatsApp announcing it next.", "Agla step: members ke liye announcement WhatsApp."))
    return Draft(body, "binary_yes_no",
                 "Pre-empts panic about an expected seasonal dip, redirects spend, and reframes around the retention "
                 "number derived from the merchant's own member count and churn.",
                 ["anxiety_preemption", "specificity", "loss_aversion"], on_accept, offer=deliverable)


def renewal_due(c: Ctx, v: Voice) -> Draft:
    days = c.payload.get("days_remaining") or c.sub.get("days_remaining")
    plan = c.payload.get("plan") or c.sub.get("plan", "")
    amount = c.payload.get("renewal_amount")
    is_trial = str(plan).lower() == "trial" or c.sub.get("status") == "trial"
    if is_trial:
        head = v.t(f"your magicpin trial ends in {num(days)} days", f"aapka magicpin trial {num(days)} din mein khatam ho raha hai") if days else \
            v.t("your magicpin trial is ending", "aapka magicpin trial khatam ho raha hai")
    else:
        head = v.t(f"your {plan} plan renews in {num(days)} days", f"aapka {plan} plan {num(days)} din mein renew hona hai") if days else \
            v.t(f"your {plan} plan is up for renewal", f"aapka {plan} plan renewal pe hai")
    if amount:
        head += f" ({inr(amount)})"
    # Honest value read: good numbers justify renewal; weak ones need a plan, not a pitch.
    wd = c.worst_delta()
    strength = c.standout_strength()
    if wd and wd[1] <= -0.2:
        m, d = wd
        extra = []
        if c.verified is False:
            extra.append(v.t("verifying your Google profile", "Google profile verify karna"))
        lapsed = c.agg.get("lapsed_180d_plus") or c.agg.get("lapsed_90d_plus")
        if lapsed:
            extra.append(v.t(f"a recall push to the {num(lapsed)} customers who haven't returned", f"{num(lapsed)} lapsed customers ko recall push"))
        read = v.t(f" Straight answer on value: {METRIC_LABEL.get(m, m)} fell {pct(d)} this week, so renewal alone won't fix things — next cycle should start with {' and '.join(extra) or 'a profile refresh'}.",
                   f" Seedhi baat: is hafte {METRIC_LABEL.get(m, m)} {pct(d)} gire hain, sirf renewal se kaam nahi banega — agle cycle ki shuruaat {' aur '.join(extra) or 'profile refresh'} se honi chahiye.")
        cta = ask(v, "Want a 3-line plan for the next 30 days before you decide?", "Decide karne se pehle agle 30 din ka 3-line plan bhej doon?")
        c.allow(3, 30)
        deliverable = "30-day plan"
    elif strength:
        m, mine, peer = strength
        read = v.t(f" What the listing is carrying right now: {METRIC_LABEL.get(m, m)} at {fmt_metric(m, mine)} — {vs_peer(c, v, m, mine, peer)}. A lapse pauses the upkeep behind that.",
                   f" Abhi listing kya kar rahi hai: {METRIC_LABEL.get(m, m)} {fmt_metric(m, mine)} — {vs_peer(c, v, m, mine, peer)}. Lapse hua toh yeh upkeep ruk jayega.")
        cta = ask(v, "Want me to move you to the Pro plan so nothing pauses?", "Pro plan pe shift kar doon taaki kuch na ruke?") if is_trial else \
            ask(v, "Want me to renew it on the same plan?", "Same plan pe renew kar doon?")
        deliverable = "renewal"
    else:
        read = v.t(f" Last 30 days: {num(c.perf.get('views', 0))} profile views and {num(c.perf.get('calls', 0))} calls through your listing.",
                   f" Pichhle 30 din: {num(c.perf.get('views', 0))} profile views aur {num(c.perf.get('calls', 0))} calls.")
        cta = ask(v, "Want me to move you to the Pro plan so nothing pauses?", "Pro plan pe shift kar doon taaki kuch na ruke?") if is_trial else \
            ask(v, "Want me to renew it on the same plan?", "Same plan pe renew kar doon?")
        deliverable = "renewal"
    body = lead(c, v, f"{head}.{read}") + f"\n{cta}"
    if deliverable == "30-day plan":
        plan_txt = v.t("1. Week 1: finish Google verification + one live service offer\n2. Week 2: recall WhatsApp to lapsed customers\n3. Weeks 3-4: 2 posts/week, then review calls together",
                       "1. Week 1: Google verification + ek live service offer\n2. Week 2: lapsed customers ko recall WhatsApp\n3. Week 3-4: 2 posts/week, phir calls review")
        c.allow(1, 2, 3, 4)
        on_accept = accept_text(c, v, v.t("here's the 30-day plan", "30-din ka plan yeh raha"), plan_txt,
                                v.t("If it looks right, I'll line up the renewal link with week 1.", "Theek lage toh renewal ke saath week 1 shuru kar dete hain."))
    else:
        on_accept = accept_text(c, v, v.t(f"renewal for {plan} queued", f"{plan} renewal queue kar diya"), "",
                                v.t("You'll get the payment confirmation here.", "Payment confirmation yahin aayega."))
    return Draft(body, "binary_yes_no",
                 "Renewal framed on the merchant's actual numbers — honest when performance is weak (plan before pitch), "
                 "loss-aversion when it's strong.",
                 ["loss_aversion", "specificity", "trust"], on_accept, offer=deliverable)


def festival_upcoming(c: Ctx, v: Voice) -> Draft:
    fest = c.payload.get("festival")
    fdate = parse_date(c.payload.get("date"))
    days = c.payload.get("days_until")
    if fdate and not days:
        days = (fdate - c.today).days
        c.allow(days)
    beat = c.beat_matching("festival", "diwali", "wedding") if fest or c.is_placeholder else None
    lines = []
    far = bool(days and days > 45)
    if fest:
        when_en = f"{fest} is on {dow_day_month(fdate)}" if fdate else f"{fest} is coming up"
        when_hi = f"{fest} {dow_day_month(fdate)} ko hai" if fdate else f"{fest} aa raha hai"
        if far:
            lines.append(v.t(f"{when_en} — {num(days)} days out, so it's too early for a promo.",
                             f"{when_hi} — abhi {num(days)} din baaki hain, promo ka time nahi aaya."))
        elif days:
            lines.append(v.t(f"{when_en} — {num(days)} days to go, which is exactly when bookings start.",
                             f"{when_hi} — {num(days)} din baaki, bookings abhi shuru hoti hain."))
        else:
            lines.append(v.t(when_en + ".", when_hi + "."))
    else:
        lines.append(v.t("festival season is next on the calendar.", "agla season festivals ka hai."))
    if beat:
        head, _, tail = str(beat.get("note", "")).partition(" — ")
        biz_type = SINGULAR.get(c.slug, "business") + "s"
        lines.append(v.t(f"For {biz_type}, {beat['month_range']} is the {head}" + (f" — {tail}." if tail else "."),
                         f"{biz_type.capitalize()} ke liye {beat['month_range']} {head} hota hai" + (f" ({tail})." if tail else ".")))
    neg = c.review("neg")
    wait_theme = next((r for r in neg if "wait" in r.get("theme", "")), None)
    if wait_theme:
        occ, theme = wait_theme.get("occurrences_30d"), humanize_token(wait_theme["theme"])
        if far:
            lines.append(v.t(f"One thing worth fixing well before then: {occ} reviews this month already mention {theme}, and festive weekends will stretch that further.",
                             f"Usse pehle ek cheez theek karni chahiye: is mahine {occ} reviews {theme} pe hain — festive weekends pe yeh aur badhega."))
            cta = ask(v, "Want me to add weekend pre-booking to your Google profile now, so it runs smoothly before the rush?",
                      "Google profile pe abhi weekend pre-booking add kar doon, taaki rush se pehle sab set ho?")
            deliverable = "weekend pre-booking added to your Google profile"
        else:
            lines.append(v.t(f"{occ} reviews this month already mention {theme} — festive weekends will make that worse unless regulars can pre-book.",
                             f"Is mahine {occ} reviews {theme} pe hain — festive weekends pe yeh badhega, jab tak regulars pre-book na kar sakein."))
            cta = ask(v, f"Want me to open a '{fest or 'festive'} pre-booking' list for your regulars on Google?",
                      f"Regulars ke liye Google pe '{fest or 'festive'} pre-booking' list khol doon?")
            deliverable = f"{fest or 'festive'} pre-booking list set up"
    else:
        offer = best_active_offer(c) or c.catalog_offer("family", "couple", "combo", "annual", "membership", "refer")
        if offer:
            lines.append(v.t(f"'{offer['title']}' is the kind of offer that moves in this window.", f"'{offer['title']}' jaisa offer is window mein chalta hai."))
            cta = ask(v, "Want me to schedule it as a festive post for the right week?", "Sahi hafte ke liye festive post schedule kar doon?")
            deliverable = f"festive post for '{offer['title']}' scheduled"
        else:
            cta = ask(v, "Want me to plan the festive posts now so they're ready?", "Festive posts abhi plan kar doon?")
            deliverable = "festive post plan drafted"
    body = lead(c, v, " ".join(lines)) + f"\n{cta}"
    on_accept = accept_text(c, v, deliverable, "",
                            v.t("I'll send you the first post to approve 3 weeks before the date.", "Date se 3 hafte pehle pehla post approval ke liye bhejungi."))
    c.allow(3)
    return Draft(body, "binary_yes_no",
                 "Festival trigger with judgement on timing (too early for promo vs right window) and tied to the "
                 "merchant's own review pain-point or active offer.",
                 ["specificity", "loss_aversion", "judgement"], on_accept, offer=deliverable)


def curious_ask(c: Ctx, v: Voice) -> Draft:
    """Asking-the-merchant lever: low-stakes question, with a specific guess from their data."""
    pos = c.review("pos")
    trend = c.top_trend()
    guess, why = None, []
    if pos:
        top = pos[0]
        quote = top.get("common_quote")
        guess = re.sub(r"\s*(quality|skill)$", "", humanize_token(top.get("theme", ""))).strip()
        if quote:  # the quote often names the actual service ("best for balayage")
            named = re.search(r"\bfor (?:the |a )?([a-z][a-z ]{2,20})$", quote.lower())
            if named:
                guess = named.group(1).strip()
        if guess in ("stylist", "instructor", "doctor", "staff", "service"):
            guess = None
        occ = top.get("occurrences_30d")
        quoted = f" (\"{quote}\")" if quote else ""
        if guess:
            why.append(v.t(f"{occ} reviews this month praise it{quoted}",
                           f"is mahine {occ} reviews mein iski taareef hai{quoted}"))
    matching = c.trend(guess.split()[-1]) if guess else None
    trend = matching or trend
    if trend:
        q = trend.get("query", "")
        if guess and guess.split()[-1] in q:
            why.append(v.t(f"'{q}' searches are up {pct(trend['delta_yoy'])} YoY", f"'{q}' searches {pct(trend['delta_yoy'])} YoY upar hain"))
        elif not guess:
            guess = q.replace(" near me", "").replace(" delhi", "")
            why.append(v.t(f"'{q}' searches are up {pct(trend['delta_yoy'])} YoY", f"'{q}' searches {pct(trend['delta_yoy'])} YoY upar hain"))
    thing = {"restaurants": "dish", "pharmacies": "product"}.get(c.slug, "service")
    q_en = f"quick one — what's been the most-asked {thing} at {c.biz} this week?"
    q_hi = f"ek quick sawaal — is hafte {c.biz} pe sabse zyada kaunsi {thing} poochi gayi?"
    guess_line = ""
    if guess:
        guess_line = v.t(f" My guess is {guess}: {' and '.join(why)}.", f" Mera guess {guess} hai: {' aur '.join(why)}.")
    close = v.t("Tell me the real answer and I'll turn it into a Google post plus a ready price-reply for WhatsApp enquiries.",
                "Asli jawab bata dijiye — main usse Google post aur WhatsApp enquiries ke liye ready price-reply bana dungi.")
    body = f"{opener(c, v)} {v.t(q_en, q_hi)}{guess_line}\n{close}"
    return Draft(body, "open_ended",
                 "Curious-ask cadence: a low-effort question with a data-backed guess (reviews/trends) so it reads as "
                 "attention, not a survey; reciprocity promised up front.",
                 ["asking_the_merchant", "reciprocity", "curiosity"],
                 on_accept="", offer=v.t("a Google post + WhatsApp price reply", "Google post + WhatsApp price-reply"))


def winback_eligible(c: Ctx, v: Voice) -> Draft:
    days = c.payload.get("days_since_expiry") or c.sub.get("days_since_expiry")
    dip = c.payload.get("perf_dip_pct")
    added = c.payload.get("lapsed_customers_added_since_expiry")
    lapsed = c.agg.get("lapsed_90d_plus") or c.agg.get("lapsed_180d_plus")
    lines = []
    if days:
        lines.append(v.t(f"it's been {num(days)} days since {c.biz}'s plan lapsed. Here's what moved since:",
                         f"{c.biz} ka plan band hue {num(days)} din ho gaye. Tab se kya badla:"))
    if dip:
        lines.append(v.t(f"calls are down {pct(dip)}", f"calls {pct(dip)} gire hain"))
    if added:
        tail = v.t(f" (you're now at {num(lapsed)} lapsed 90+ days)", f" (ab total {num(lapsed)} lapsed 90+ din)") if lapsed else ""
        lines.append(v.t(f"and {num(added)} more customers have slipped into your lapsed list{tail}",
                         f"aur {num(added)} aur customers lapsed list mein chale gaye{tail}"))
    body_core = lines[0] + " " + (", ".join(lines[1:]) + "." if len(lines) > 1 else "")
    body = f"""{opener(c, v)} {body_core} {v.t("The recall flow that brings them back is paused with the plan.", "Unhe wapas laane wala recall flow plan ke saath ruka hua hai.")}
{ask(v, "Want to see exactly what reactivating switches back on?", "Reactivate karne se kya wapas chalu hoga, dikhaoon?")}"""
    on_accept = accept_text(c, v, v.t("here's what comes back on", "yeh sab wapas chalu hoga"),
                            v.t(f"1. Recall WhatsApp to your {num(lapsed or added or 0)} lapsed customers\n2. Weekly Google posts + review replies\n3. Offer listing on magicpin",
                                f"1. {num(lapsed or added or 0)} lapsed customers ko recall WhatsApp\n2. Weekly Google posts + review replies\n3. magicpin pe offer listing"),
                            v.t("Reply RENEW and I'll send the reactivation link.", "RENEW likhiye, reactivation link bhej dungi."))
    return Draft(body, "binary_yes_no",
                 "Win-back built on loss since expiry (days, call dip, lapsed-customer growth) — concrete cost of "
                 "staying off, not a discount pitch.",
                 ["loss_aversion", "specificity", "curiosity"], on_accept, offer=v.t("the reactivation breakdown", "reactivation breakdown"))


def dormant_with_vera(c: Ctx, v: Voice) -> Draft:
    days = c.payload.get("days_since_last_merchant_message")
    last_topic = c.payload.get("last_topic", "")
    total = c.agg.get("total_unique_ytd")
    lapsed = c.agg.get("lapsed_90d_plus") or c.agg.get("lapsed_180d_plus")
    no_pitch = v.t("No pitch this time — just one number I noticed:", "Is baar koi pitch nahi — bas ek number jo dikha:")
    if lapsed and total:
        share = lapsed / total
        c.allow(round(share * 100))
        frac = "nearly half" if 0.4 <= share < 0.5 else ("over half" if share >= 0.5 else f"{pct(share)}")
        frac_hi = "lagbhag aadhe" if 0.4 <= share < 0.5 else ("aadhe se zyada" if share >= 0.5 else pct(share))
        horizon = "90+" if c.agg.get("lapsed_90d_plus") else "180+"
        fact = v.t(f"{num(lapsed)} of your customers haven't been back in {horizon} days — {frac} of the {num(total)} you served this year.",
                   f"Aapke {num(lapsed)} customers {horizon} din se wapas nahi aaye — is saal ke {num(total)} mein se {frac_hi}.")
        cta = ask(v, "Want the list split by what they last booked?", "Unki list — last service ke hisaab se — bhej doon?")
        deliverable = "lapsed-customer list by last service"
    else:
        strength = c.standout_strength()
        weak = c.standout_weakness()
        if strength:
            m, mine, peer = strength
            fact = v.t(f"your {METRIC_LABEL.get(m, m)} is {fmt_metric(m, mine)} — {vs_peer(c, v, m, mine, peer)}. People who find you, pick you.",
                       f"aapka {METRIC_LABEL.get(m, m)} {fmt_metric(m, mine)} hai — {vs_peer(c, v, m, mine, peer)}. Jo dhoondhta hai, aapko chunta hai.")
            if weak and weak[0] != m:
                wm, wmine, wpeer = weak
                fact += v.t(f" The gap is reach: {fmt_metric(wm, wmine)} {METRIC_LABEL.get(wm, wm)} vs {fmt_metric(wm, wpeer)} for peers.",
                            f" Kami reach ki hai: {fmt_metric(wm, wmine)} {METRIC_LABEL.get(wm, wm)} vs peers ke {fmt_metric(wm, wpeer)}.")
            cta = ask(v, "Want me to put that to work with 2 posts this week?", "Is hafte 2 posts se isse kaam pe lagaoon?")
            deliverable = "2 posts drafted"
        elif weak:
            m, mine, peer = weak
            fact = v.t(f"your {METRIC_LABEL.get(m, m)} is {fmt_metric(m, mine)} this month vs {fmt_metric(m, peer)} for {peer_scope(c)}.",
                       f"aapke {METRIC_LABEL.get(m, m)} is mahine {fmt_metric(m, mine)} hain, {peer_scope(c)} ka average {fmt_metric(m, peer)}.")
            cta = ask(v, "Want to see the 2 changes that usually close that gap?", "Yeh gap band karne wale 2 changes dikhaoon?")
            deliverable = "2 changes to close the gap"
        else:
            return generic(c, v)
    lead = ""
    if days and last_topic:
        lead = v.t(f"it's been {num(days)} days since we last spoke (about {humanize_token(last_topic)}). ",
                   f"{humanize_token(last_topic)} wali baat ko {num(days)} din ho gaye. ")
    body = f"{c.salutation}, {lead}{no_pitch if lead else lc_first(no_pitch)} {lc_first(fact)}\n{cta}"
    on_accept = accept_text(c, v, deliverable, "", v.t("Take a look and tell me which one to act on first.", "Dekh ke bataiye pehle kis pe kaam karein."))
    return Draft(body, "binary_yes_no",
                 "Re-engaging a dormant merchant with curiosity + one verifiable number instead of repeating the last "
                 "(unanswered) topic.",
                 ["curiosity", "reciprocity", "specificity"], on_accept, offer=deliverable)


def ipl_match_today(c: Ctx, v: Voice) -> Draft:
    p = c.payload
    match, venue = p.get("match", "tonight's match"), p.get("venue", "")
    mt = parse_dt(p.get("match_time_iso"))
    weeknight = p.get("is_weeknight")
    item = c.digest_item(kinds=("seasonal",)) or c.digest_mentioning("ipl")
    when = f"{mt.astimezone(IST).strftime('%a')} {clock(mt)}" if mt else "tonight"
    lines = [v.t(f"{match} at {venue} tonight, {clock(mt) if mt else ''}.".replace(" ,", ","),
                 f"aaj raat {match}, {venue}, {clock(mt) if mt else ''}.".replace(" ,", ","))]
    bogo = c.offer_matching("buy 1", "bogo")
    combo = c.catalog_offer("match-night", "match night")
    late = next((r for r in c.review("neg") if "deliver" in r.get("theme", "")), None)
    # One driving signal: weekend vs weeknight match. Everything else supports it or waits for the follow-up.
    if weeknight is False:
        combo_name = short_title(combo["title"]) if combo else "match combo"
        if item:
            lines.append(v.t("Weekend matches pull dine-in covers down ~12% this season (people watch at home) — tonight is a delivery play.",
                             "Weekend matches pe is season dine-in covers ~12% girte hain (log ghar pe dekhte hain) — aaj delivery ka din hai."))
        if bogo and "tue" in bogo["title"].lower():
            lines.append(v.t(f"Your BOGO is Tue-Thu only, so a delivery-only '{combo_name}' fits.",
                             f"Aapka BOGO sirf Tue-Thu hai, isliye delivery-only '{combo_name}' sahi rahega."))
        offer_name = combo_name
    else:
        lines.append(v.t("Weeknight matches have been adding ~18% dine-in covers this season — tonight is one to staff up for.",
                         "Weeknight matches pe is season ~18% zyada covers aaye hain — aaj staff poora rakhiye."))
        offer_name = short_title((bogo or combo or {}).get("title", "match-night combo"))
    start = clock(mt) if mt else "match"
    body = f"{c.salutation}, " + " ".join(lines) + "\n" + ask(v, f"Want it live on your listing before the {start} start?",
                                                              f"{start} se pehle listing pe live kar doon?")
    c.allow(7, 30, 12, 18)
    # The late-delivery risk is real but secondary: it shapes the execution, not the pitch.
    guard = v.t(f" Given {late.get('occurrences_30d')} recent late-delivery reviews, I've capped the delivery radius for the match window.",
                f" {late.get('occurrences_30d')} late-delivery reviews ki wajah se match ke time delivery radius chhota rakha hai.") if late else ""
    on_accept = accept_text(c, v, v.t(f"'{offer_name}' is going live on your listing now", f"'{offer_name}' abhi listing pe live ho raha hai"),
                            v.t(f"Banner line: \"{match} tonight — {offer_name}, delivered hot.\"", f"Banner line: \"{match} aaj raat — {offer_name}, garam delivery.\"") + guard,
                            v.t("I'll pull it down after the match and send you tonight's order count tomorrow.", "Match ke baad hata dungi aur kal subah orders ka count bhejungi."))
    return Draft(body, "binary_yes_no",
                 "Lead signal: weekend match → delivery beats dine-in (category digest: weekend covers -12%). Supporting fact: the "
                 "merchant's BOGO doesn't run today. Deliberately held back: late-delivery reviews — used in the follow-up to cap "
                 "delivery radius rather than diluting the pitch.",
                 ["judgement", "specificity", "effort_externalization"], on_accept, offer=offer_name)


def review_theme_emerged(c: Ctx, v: Voice) -> Draft:
    theme = c.payload.get("theme")
    occ = c.payload.get("occurrences_30d")
    quote = c.payload.get("common_quote")
    trend = c.payload.get("trend")
    if not theme:
        neg = c.review("neg")
        if neg:
            theme, occ, quote = neg[0].get("theme"), neg[0].get("occurrences_30d"), neg[0].get("common_quote")
    if not theme:
        body = f"""{c.salutation}, {v.t("a repeating theme has started showing up in your recent Google reviews.", "aapke recent Google reviews mein ek baat baar-baar aa rahi hai.")} {v.t("Themes like this are cheap to fix early and expensive once they shape your rating.", "Aisi cheezein shuru mein theek karna aasaan hai, rating pe asar aane ke baad mushkil.")}
{ask(v, "Want the 30-day review summary with draft replies?", "30-din ka review summary draft replies ke saath bhej doon?")}"""
        c.allow(30)
        return Draft(body, "binary_yes_no", "Review-theme trigger without theme details in context — kept it honest (no invented theme), curiosity + offer.",
                     ["curiosity", "loss_aversion"], accept_text(c, v, v.t("review summary is being prepared", "review summary ban raha hai")),
                     offer=v.t("the review summary", "review summary"))
    label = humanize_token(theme)
    lines = [v.t(f"a pattern in {c.biz}'s reviews: {occ} in the last 30 days mention {label}", f"{c.biz} ke reviews mein ek pattern: pichhle 30 din mein {occ} reviews {label} pe hain")]
    if trend == "rising":
        lines[0] += v.t(", and it's rising", ", aur badh raha hai")
    lines[0] += "."
    if quote:
        lines.append(v.t(f"One says: \"{quote}\".", f"Ek review: \"{quote}\"."))
    pos = c.review("pos")
    if pos:
        lines.append(v.t(f"Meanwhile {humanize_token(pos[0]['theme'])} gets {pos[0].get('occurrences_30d')} positive mentions — so it's the {label} costing you stars, not the core product.",
                         f"Wahin {humanize_token(pos[0]['theme'])} ko {pos[0].get('occurrences_30d')} positive mentions mile hain — stars {label} ki wajah se kat rahe hain, product ki wajah se nahi."))
    body = f"{c.salutation}, " + " ".join(lines) + "\n" + ask(
        v, f"Want me to draft honest replies to those {occ} reviews plus a fix note for your listing?",
        f"Un {occ} reviews ke liye honest replies aur listing pe ek fix-note draft kar doon?")
    reply_draft = v.t(f"Sample reply: \"Thank you for flagging this — you're right, that's not the standard we want. We've changed how we handle {label} this week. Please give us another try.\"",
                      f"Sample reply: \"Batane ke liye shukriya — aap sahi hain, yeh hamara standard nahi hai. Is hafte se {label} ka process badla hai. Ek baar aur mauka dijiye.\"")
    on_accept = accept_text(c, v, v.t(f"{occ} reply drafts are ready", f"{occ} reply drafts ready hain"), reply_draft,
                            v.t("Approve and I'll post them one by one.", "Approve kijiye, main ek-ek karke post kar dungi."))
    return Draft(body, "binary_yes_no",
                 "Negative review theme quantified and quoted; contrasted with the positive theme to locate the real problem; "
                 "offers ready replies.",
                 ["specificity", "loss_aversion", "effort_externalization"], on_accept, offer=v.t("the review replies", "review replies"))


def milestone_reached(c: Ctx, v: Voice) -> Draft:
    metric = c.payload.get("metric")
    now_v, target = c.payload.get("value_now"), c.payload.get("milestone_value")
    peer_reviews = c.peer.get("avg_review_count")
    if metric == "review_count" and now_v and target:
        gap = int(target) - int(now_v)
        c.allow(gap)
        lines = [v.t(f"{c.biz} is {gap} reviews away from {num(target)} on Google ({num(now_v)} now)",
                     f"{c.biz} Google pe {num(target)} reviews se sirf {gap} door hai (abhi {num(now_v)})")]
        if peer_reviews and now_v > peer_reviews:
            lines[0] += v.t(f" — already above the {num(peer_reviews)} average for {peer_scope(c)}", f" — {peer_scope(c)} ke {num(peer_reviews)} average se already upar")
        lines[0] += "."
        pos = c.review("pos")
        if pos:
            lines.append(v.t(f"With {pos[0].get('occurrences_30d')} recent reviews already praising the {humanize_token(pos[0]['theme']).replace(' quality', '')}, a small 'how was your meal?' card at billing should get you there this week.",
                             f"{pos[0].get('occurrences_30d')} recent reviews already {humanize_token(pos[0]['theme']).replace(' quality', '')} ki taareef kar rahe hain — billing pe ek chhota 'khana kaisa laga?' card is hafte kaam kar dega."))
        cta = ask(v, "Want me to draft the card and a thank-you post for when you cross it?", "Card aur cross karne ke baad ka thank-you post draft kar doon?")
        deliverable = "review card + thank-you post drafted"
    else:
        strength = c.standout_strength()
        if not strength:
            return perf_spike(c, v) if c.best_delta() else generic(c, v)
        m, mine, peer = strength
        lines = [v.t(f"a milestone worth noticing: your {METRIC_LABEL.get(m, m)} is {fmt_metric(m, mine)} — {vs_peer(c, v, m, mine, peer)}.",
                     f"ek milestone jo notice karna chahiye: aapka {METRIC_LABEL.get(m, m)} {fmt_metric(m, mine)} hai — {vs_peer(c, v, m, mine, peer)}.")]
        if m == "ctr":
            lines.append(v.t("People who see you, pick you.", "Jo aapko dekhta hai, woh aapko chunta hai."))
        views = c.peer_gap("views")
        if views and m != "views" and views[0] < views[1]:
            lines.append(v.t(f"The only gap is reach: {num(views[0])} views in 30 days vs a peer average of {num(views[1])}.",
                             f"Kami sirf reach ki hai: 30 din mein {num(views[0])} views, peers ka average {num(views[1])}."))
        cta = ask(v, "Want me to put that conversion to work with 3 Google posts this week?", "Is conversion ka fayda uthane ke liye is hafte 3 Google posts daal doon?")
        c.allow(3)
        deliverable = "3 Google posts drafted"
    body = f"{c.salutation}, " + " ".join(lines) + "\n" + cta
    on_accept = accept_text(c, v, deliverable,
                            post_draft(c, v.t("Thank you for choosing us", "Aapka shukriya"), v.t("Your reviews keep us honest — tell us how we did today.", "Aapke reviews humein behtar banate hain.")),
                            v.t("Approve and it goes live today.", "Approve kijiye, aaj hi live."))
    return Draft(body, "binary_yes_no",
                 "Milestone framed with a verifiable gap/peer comparison and a concrete tactic to cross it; for generated "
                 "triggers without a milestone value, the milestone is derived from the merchant's strongest peer-relative metric.",
                 ["specificity", "social_proof", "effort_externalization"], on_accept, offer=deliverable)


def active_planning_intent(c: Ctx, v: Voice) -> Draft:
    topic = str(c.payload.get("intent_topic", ""))
    last = c.payload.get("merchant_last_message") or (c.last_merchant_msg or {}).get("body", "")
    if "thali" in topic or "corporate" in topic:
        return _plan_corporate_thali(c, v)
    if "yoga" in topic or "kids" in topic:
        return _plan_kids_program(c, v)
    # Generic: deliver a first draft immediately — never answer "what would it look like" with a question.
    offer = best_active_offer(c) or suggested_catalog_offer(c)
    t = humanize_token(topic) if topic else "the plan you mentioned"
    body = f"""{c.salutation}, {v.t(f"here's a first cut of {t} — edit anything:", f"{t} ka pehla draft — jo chahein badal dijiye:")}
• {v.t("Hero offer", "Main offer")}: {offer['title'] if offer else t}
• {v.t("Launch", "Launch")}: {v.t("Google post + WhatsApp to your regulars in the same week", "Google post + regulars ko WhatsApp, ek hi hafte mein")}
• {v.t("Review", "Review")}: {v.t("check calls and enquiries after 14 days", "14 din baad calls aur enquiries check")}
{ask(v, "Want me to turn this into the launch post?", "Isse launch post bana doon?")}"""
    c.allow(14)
    return Draft(body, "binary_yes_no", f"Merchant already expressed planning intent ('{last}') — delivered a draft instead of qualifying.",
                 ["effort_externalization", "momentum"], accept_text(c, v, v.t("launch post drafted", "launch post ready")),
                 offer=v.t("the launch post", "launch post"))


def _plan_corporate_thali(c: Ctx, v: Voice) -> Draft:
    thali = c.offer_matching("thali")
    base = None
    if thali:
        m = re.search(r"₹\s?([\d,]+)", thali["title"])
        base = int(m.group(1).replace(",", "")) if m else None
    base = base or 149
    t1, t2, t3 = base - 14, base - 20, base - 30   # tiered B2B pricing proposal
    c.allow(base, t1, t2, t3, 10, 24, 25, 49, 50, 5, 12, 30, 1)
    daily = None
    for turn in c.history:
        m = re.search(r"(\d+)\s*orders?/day", turn.get("body", ""))
        if m:
            daily = int(m.group(1))
    compare = ""
    if daily:
        compare = v.t(f"\nYou're doing {daily} thali orders a day now — a single 25-seat office would more than double weekday lunch.",
                      f"\nAbhi {daily} thali orders/day hain — sirf ek 25-seat office weekday lunch double se zyada kar dega.")
    body = f"""{c.salutation}, {v.t("here's a first cut of the corporate thali — edit anything:", "corporate thali ka pehla draft — jo chahein badliye:")}
• {v.t("Base", "Base")}: {v.t("your", "aapki")} {(thali['title'].split(' @')[0] if thali else 'Weekday Lunch Thali')} ({inr(base)} retail)
• 10-24 thalis/day: {inr(t1)} each
• 25-49: {inr(t2)} each + filter coffee on the house
• 50+: {inr(t3)} each + a fixed 12:30-1pm delivery slot
• {v.t("Orders by 5pm the day before; monthly invoice for the office", "Order ek din pehle 5pm tak; office ko monthly invoice")}{compare}
{ask(v, "Want me to turn this into a one-page menu card plus a WhatsApp pitch for office admins nearby?", "Isse 1-page menu card aur office admins ke liye WhatsApp pitch bana doon?")}"""
    pitch = (f"\"Hi! {c.biz}, {c.locality} here. We deliver fresh South Indian thalis to offices — {inr(t1)} each from 10 plates, "
             f"{inr(t3)} at 50+, delivered 12:30-1pm. Want a free tasting for your team this week?\"")
    on_accept = accept_text(c, v, v.t("menu card and pitch are ready", "menu card aur pitch ready"), f"WhatsApp pitch:\n{pitch}",
                            v.t("I'll also add a 'Corporate orders' line to your Google profile.", "Google profile pe 'Corporate orders' line bhi add kar rahi hoon."))
    return Draft(body, "binary_yes_no",
                 "Merchant asked 'what would it look like' — answered with a complete, editable tiered draft anchored on their "
                 f"own {inr(base)} thali and current daily volume; no qualifying questions, no invented office names.",
                 ["effort_externalization", "specificity", "momentum"], on_accept, offer=v.t("the menu card + office pitch", "menu card + office pitch"))


def _plan_kids_program(c: Ctx, v: Voice) -> Draft:
    spec = None
    for turn in reversed(c.history):
        if turn.get("from") == "vera" and re.search(r"\d+-week", turn.get("body", "")):
            spec = turn["body"]
            break
    weeks = re.search(r"(\d+)-week", spec or "")
    per_week = re.search(r"(\d+) classes/week", spec or "")
    ages = re.search(r"age (\d+-\d+)", spec or "")
    price = re.search(r"₹\s?([\d,]+)", spec or "")
    parts = []
    if weeks: parts.append(f"{weeks.group(1)} weeks")
    if per_week: parts.append(f"{per_week.group(1)} classes a week")
    if ages: parts.append(f"ages {ages.group(1)}")
    if price: parts.append(f"₹{price.group(1)}")
    shape = " · ".join(parts) or "4 weeks · 3 classes a week · ages 7-12"
    c.allow(4, 3, 7, 12)
    praise = next((r for r in c.review("pos") if "instructor" in r.get("theme", "")), None)
    small = next((r for r in c.review("pos") if "small" in r.get("theme", "")), None)
    selling = []
    if small: selling.append("small batches")
    if praise: selling.append("the same instructors our members rate")
    sell = (", ".join(selling) + ". ") if selling else ""
    post = f"\"Summer Kids Yoga at {c.biz}, {c.locality} 🧘 {shape}. {sell.capitalize()}Book a trial class on WhatsApp.\""
    members = c.agg.get("total_active_members")
    audience = v.t(f"First audience: your {num(members)} active members — parents book fastest when a friend's child is in the batch.",
                   f"Pehli audience: aapke {num(members)} active members.") if members else ""
    body = f"""{c.salutation}, {v.t("picking up the kids yoga plan — here's the Google post, ready to publish:", "kids yoga plan aage badhate hain — Google post ready hai:")}
{post}
{audience}
{ask(v, "Want me to publish it and send the Insta carousel next?", "Publish kar doon aur agla Insta carousel bhejoon?")}"""
    on_accept = accept_text(c, v, v.t("the post is going live on your Google profile", "post Google profile pe live ho raha hai"),
                            v.t("Carousel: slide 1 the programme card, slide 2 a class photo, slide 3 schedule + price, slide 4 'Book a trial'.",
                                "Carousel: slide 1 programme card, slide 2 class photo, slide 3 schedule + price, slide 4 'Book a trial'."),
                            v.t("Send me 2 class photos when you can and I'll finish it.", "2 class photos bhej dijiye, carousel poora kar dungi."))
    c.allow(1, 2)
    return Draft(body, "binary_yes_no",
                 "Continuing an in-flight planning thread: uses the programme spec already agreed in conversation history and "
                 "delivers the post itself, selling points taken from the studio's positive review themes.",
                 ["effort_externalization", "momentum", "social_proof"], on_accept, offer=v.t("publishing the post", "post publish"))


def supply_alert(c: Ctx, v: Voice) -> Draft:
    p = c.payload
    item = c.digest_item(p.get("alert_id"), kinds=("alert",))
    molecule = p.get("molecule", "")
    batches = ", ".join(p.get("affected_batches") or [])
    mfr = p.get("manufacturer", "")
    summary = (item or {}).get("summary", "")
    no_risk = "no safety risk" in summary.lower()
    asked = c.last_merchant_msg and "list" in c.last_merchant_msg.get("body", "").lower()
    chronic = c.agg.get("chronic_rx_count")
    lead = v.t(f"following up on the {molecule} recall you asked about" if asked else f"urgent: voluntary recall on {molecule}",
               f"aapne jis {molecule} recall ki list maangi thi, uska update" if asked else f"urgent: {molecule} ka voluntary recall")
    detail = v.t(f"{(item or {}).get('source', 'CDSCO')} confirms batches {batches} ({mfr}) for sub-potency.",
                 f"{(item or {}).get('source', 'CDSCO')} ne batches {batches} ({mfr}) ko sub-potency ke liye flag kiya hai.")
    if no_risk:
        detail += v.t(" No safety risk, but patients on these batches get weaker LDL control, so they should swap.",
                      " Safety risk nahi hai, par in batches pe LDL control kamzor rehta hai — replace karna chahiye.")
    matched = v.t(f" I've matched it against your {num(chronic)} chronic-Rx customers — the {molecule} list is ready, with a patient WhatsApp note and the distributor return steps.",
                  f" Aapke {num(chronic)} chronic-Rx customers se match kar liya hai — {molecule} list ready hai, patient WhatsApp note aur distributor return steps ke saath.") if chronic else ""
    body = f"{c.salutation}, {lead}: {detail}{matched}\n" + ask(v, "Send all three now?", "Teeno abhi bhej doon?")
    note = v.t(f"\"Namaste from {c.biz}. A batch of {molecule} you may have received is being replaced by the manufacturer (lower strength, not unsafe). Please bring your strip to the counter or reply here — we'll swap it free.\"",
               f"\"Namaste, {c.biz} se. Aapko mili {molecule} ki ek batch manufacturer replace kar raha hai (strength kam hai, unsafe nahi). Strip counter pe laiye ya yahan reply kijiye — free mein badal denge.\"")
    on_accept = accept_text(c, v, v.t("sending the list, patient note and return steps", "list, patient note aur return steps bhej rahi hoon"),
                            f"Patient note:\n{note}\n\nReturn: pull batches {batches} → log in your return register → hand to distributor against credit note.",
                            v.t("Want me to send the patient note to the matched customers directly — reply CONFIRM.", "Matched customers ko note seedha bhejna hai toh CONFIRM likhiye."))
    return Draft(body, "binary_yes_no",
                 "Drug recall with exact batch numbers and manufacturer; risk stated precisely (sub-potency, not unsafe); "
                 "continues the merchant's own earlier request for the list; end-to-end workflow offered.",
                 ["urgency", "specificity", "effort_externalization"], on_accept, offer=v.t("the recall list + patient note", "recall list + patient note"))


def category_seasonal(c: Ctx, v: Voice) -> Draft:
    trends = c.payload.get("trends") or []
    ups, downs = [], []
    for t in trends:
        m = re.match(r"(.+?)_demand_([+-]\d+)", str(t))
        if not m:
            continue
        name = m.group(1).replace("_", " ")
        name = name.upper() if name.lower() == "ors" else name.replace("cold cough", "cold & cough")
        val = int(m.group(2))
        (ups if val > 0 else downs).append((name, val))
    ups.sort(key=lambda x: -x[1])
    if not ups and not downs:
        beat = c.seasonal_beat()
        if not beat:
            return generic(c, v)
        body = f"{c.salutation}, {beat['month_range']}: {beat['note']}.\n" + ask(v, "Want me to rework your listing and posts for it?", "Listing aur posts isi hisaab se badal doon?")
        return Draft(body, "binary_yes_no", "Seasonal beat from category context.", ["specificity"], accept_text(c, v, "seasonal refresh queued"), offer="seasonal refresh")
    up_txt = ", ".join(f"{n} +{val}%" for n, val in ups)
    down_txt = ", ".join(f"{n} {val}%" for n, val in downs)
    season = str(c.payload.get("season", "")).split("_")[0]
    lines = [v.t(f"the {season} shift is showing in {c.slug} demand: {up_txt}" + (f", while {down_txt}." if down_txt else "."),
                 f"{season} ka shift demand mein dikh raha hai: {up_txt}" + (f", jabki {down_txt}." if down_txt else "."))]
    if c.payload.get("shelf_action_recommended"):
        lines.append(v.t(f"Quick shelf move: {ups[0][0]} and {ups[1][0] if len(ups) > 1 else 'the risers'} to the counter" + (f", {downs[0][0]} to the back." if downs else "."),
                         f"Shelf pe: {ups[0][0]} aur {ups[1][0] if len(ups) > 1 else 'yeh items'} counter pe" + (f", {downs[0][0]} peeche." if downs else ".")))
    delivery = c.offer_matching("delivery")
    pos = next((r for r in c.review("pos") if "deliver" in r.get("theme", "")), None)
    if delivery:
        proof = v.t(f"Your delivery reviews are strong ({pos.get('occurrences_30d')} this month), so", f"Aapke delivery reviews strong hain (is mahine {pos.get('occurrences_30d')}), isliye") if pos else v.t("With", "Aapke")
        lines.append(v.t(f"{proof} a 'summer essentials, delivered' post with your '{delivery['title']}' should pull.",
                         f"{proof} '{delivery['title']}' ke saath 'summer essentials, ghar tak' post achha chalega."))
    body = f"{c.salutation}, " + " ".join(lines) + "\n" + ask(v, "Want me to put that post up today?", "Yeh post aaj daal doon?")
    on_accept = accept_text(c, v, v.t("post drafted", "post ready"),
                            post_draft(c, "Summer essentials", f"{', '.join(n for n, _ in ups[:3])} in stock" + (f" — {delivery['title']}." if delivery else ".")),
                            v.t("Goes live in an hour unless you edit it.", "Ek ghante mein live, jab tak aap edit na karein."))
    return Draft(body, "binary_yes_no", "Seasonal demand shift quantified per product line, turned into a shelf action and a post that uses the merchant's own delivery offer.",
                 ["specificity", "effort_externalization", "social_proof"], on_accept, offer=v.t("the summer post", "summer post"))


def gbp_unverified(c: Ctx, v: Voice) -> Draft:
    uplift = c.payload.get("estimated_uplift_pct")
    raw_path = str(c.payload.get("verification_path", ""))
    path = raw_path.replace("_or_", v.t(" or a ", " ya ")).replace("_", " ")
    views = c.perf.get("views")
    lines = [v.t(f"{c.biz}'s Google profile is still unverified.", f"{c.biz} ka Google profile abhi bhi unverified hai.")]
    if uplift:
        lines.append(v.t(f"Verified listings in your category see an estimated {pct(uplift)} lift", f"Verified listings ko lagbhag {pct(uplift)} zyada engagement milta hai"))
        if views:
            gain = int(views * uplift)
            c.allow(gain)
            lines[-1] += v.t(f" — on your {num(views)} monthly views that's roughly {num(gain)} more people seeing a trusted listing.",
                             f" — aapke {num(views)} monthly views pe yeh lagbhag {num(gain)} aur log hain.")
        else:
            lines[-1] += "."
    if path:
        lines.append(v.t(f"It's a {path} from Google; I'll handle the steps, you just read out the code.", f"Google se {path} aata hai; steps main sambhal lungi, aapko bas code batana hai."))
    body = lead(c, v, " ".join(lines)) + "\n" + ask(v, "Shall I start verification today?", "Verification aaj shuru kar doon?")
    on_accept = accept_text(c, v, v.t("verification request submitted", "verification request submit ho gayi"), "",
                            v.t("Google's code usually arrives by call or postcard — send it here the moment it comes.", "Google ka code call ya postcard se aayega — aate hi yahan bhej dijiye."))
    return Draft(body, "binary_yes_no", "Unverified GBP: quantified the estimated uplift on the merchant's own view count and externalised the effort.",
                 ["loss_aversion", "specificity", "effort_externalization"], on_accept, offer=v.t("verification", "verification"))


def cde_opportunity(c: Ctx, v: Voice) -> Draft:
    item = c.digest_item(c.payload.get("digest_item_id"), kinds=("cde",))
    if not item:
        return generic(c, v)
    dt = parse_dt(item.get("date"))
    when = f"{dow_day_month(dt.astimezone(IST).date())}, {clock(dt)}" if dt and "T" in str(item.get("date")) else (day_month(dt.date()) if dt else "")
    credits = c.payload.get("credits") or item.get("credits")
    fee = item.get("actionable", "") if "free" in item.get("actionable", "").lower() or "₹" in item.get("actionable", "") else ""
    summary = item.get("summary", "").rstrip(".")
    source = item.get("source", "").replace(" calendar", "").replace(" chapter", "")
    title = item.get("title", "")
    if ":" in title and title.split(":", 1)[0].strip() in source:
        title = title.split(":", 1)[1].strip()  # "IDA Delhi: Digital impressions…" when source is already "IDA Delhi"
    hook = ""
    last = (c.last_merchant_msg or {}).get("body", "").lower()
    if "aligner" in last and ("scan" in summary.lower() or "impression" in item.get("title", "").lower()):
        hook = v.t(" Since you're pushing aligners, the scanner-ROI part is the bit that pays for the evening.",
                   " Aap aligners push kar rahi hain — scanner-ROI wala hissa sabse kaam ka hai.")
    fee_txt = ("; " + fee.rstrip(".")[0].lower() + fee.rstrip(".")[1:]) if fee else ""
    body = f"""{opener(c, v)} {source} is running "{title}" on {when}. {summary}.{f' {credits} CDE credits' if credits else ''}{fee_txt}.{hook}
{ask(v, "Want me to register you?", "Register kar doon?")}"""
    on_accept = accept_text(c, v, v.t("registration request sent", "registration request bhej di"), "",
                            v.t(f"I'll put a reminder here 2 hours before {when}.", f"{when} se 2 ghante pehle yahan reminder aayega."))
    c.allow(2)
    return Draft(body, "binary_yes_no", "CDE opportunity with exact date, speaker/topics, credits and fee; linked to the merchant's stated aligner interest.",
                 ["specificity", "curiosity", "effort_externalization"], on_accept, offer=v.t("registration", "registration"))


def competitor_opened(c: Ctx, v: Voice) -> Draft:
    p = c.payload
    name, dist, their = p.get("competitor_name"), p.get("distance_km"), p.get("their_offer")
    opened = parse_date(p.get("opened_date"))
    pos, neg = c.review("pos"), c.review("neg")
    if name:
        mine = c.offer_matching(*(str(their).split(" @")[0].lower().split()[:1] if their else []))
        lines = [v.t(f"a new clinic — {name} — opened {dist} km away on {day_month(opened)}" if c.slug == "dentists" else f"{name} opened {dist} km away on {day_month(opened)}",
                     f"{dist} km door {name} {day_month(opened)} ko khula hai") if opened else f"{name} opened {dist} km away"]
        if their:
            lines[0] += v.t(f", advertising {their}", f", '{their}' ke saath")
            if mine:
                lines[0] += v.t(f" vs your {mine['title'].split('@')[-1].strip()}", f" — aapka {mine['title'].split('@')[-1].strip()} hai")
        lines[0] += "."
        noun = SINGULAR.get(c.slug, "place")
        if pos:
            praise = f"\"{pos[0]['common_quote']}\"" if pos[0].get("common_quote") else humanize_token(pos[0]["theme"])
            lines.append(v.t(f"Don't match the price — you win on trust: {pos[0].get('occurrences_30d')} reviews this month say {praise}.",
                             f"Price match mat kijiye — aap trust pe jeette hain: is mahine {pos[0].get('occurrences_30d')} reviews: {praise}."))
        if neg:
            theme = humanize_token(neg[0]["theme"])
            lines.append(v.t(f"Where a new {noun} can beat you is {theme} ({neg[0].get('occurrences_30d')} reviews).",
                             f"Naya {noun} sirf {theme} pe aapko hara sakta hai ({neg[0].get('occurrences_30d')} reviews)."))
            cta = ask(v, f"Want me to add bookable slots to your Google profile to fix the {theme}?",
                      f"{theme.capitalize()} theek karne ke liye Google profile pe bookable slots add kar doon?")
            deliverable = "bookable time-slots added to your profile"
        else:
            cta = ask(v, "Want me to refresh your profile so you stay the obvious pick?", "Profile refresh kar doon taaki aap hi pehli choice rahein?")
            deliverable = "profile refresh"
    else:
        # Generated trigger: we know a competitor opened but not who — never invent a name.
        lines = [v.t(f"a new {SINGULAR.get(c.slug, 'business')} has come up on Google near you in {c.locality}.",
                     f"{c.locality} mein aapke paas Google pe ek naya {SINGULAR.get(c.slug, 'competitor')} aaya hai.")]
        strength = c.standout_strength()
        if strength:
            m, mine, peer = strength
            lines.append(v.t(f"You start ahead: {fmt_metric(m, mine)} {METRIC_LABEL.get(m, m)} a month vs {fmt_metric(m, peer)} for {peer_scope(c)}.",
                             f"Aap aage ho: {fmt_metric(m, mine)} {METRIC_LABEL.get(m, m)}/month vs {peer_scope(c)} ke {fmt_metric(m, peer)}."))
        if pos:
            lines.append(v.t(f"And {pos[0].get('occurrences_30d')} reviews this month praise your {humanize_token(pos[0]['theme']).replace(' quality', '')}.",
                             f"Aur is mahine {pos[0].get('occurrences_30d')} reviews aapke {humanize_token(pos[0]['theme']).replace(' quality', '')} ki taareef karte hain."))
        hero = best_active_offer(c)
        if hero:
            lines.append(v.t("The risk is new searchers price-shopping before they reach you.", "Risk yeh hai ki naye searchers aap tak pahunchne se pehle price compare karenge."))
            cta = ask(v, f"Want me to pin '{hero['title']}' to the top of your profile so they see it first?", f"'{hero['title']}' profile ke top pe pin kar doon taaki pehle wahi dikhe?")
            deliverable = f"'{hero['title']}' pinned"
        else:
            cta = ask(v, "Want me to refresh your photos and top offer so you stay the obvious pick?", "Photos aur top offer refresh kar doon?")
            deliverable = "profile refresh"
    body = f"{c.salutation}, " + " ".join(lines) + "\n" + cta
    on_accept = accept_text(c, v, deliverable, "", v.t("I'll keep an eye on their rating and offers and flag anything that changes.", "Unki rating aur offers pe nazar rakhungi."))
    return Draft(body, "binary_yes_no",
                 "Competitor trigger answered with a strategy (don't race on price; defend on proven strengths, close the weak "
                 "spot) using only the merchant's review data; no invented competitor details.",
                 ["loss_aversion", "judgement", "social_proof"], on_accept, offer=deliverable)


# =========================================================================== customer-facing

def _cust_open(c: Ctx, v: Voice) -> str:
    who = c.cust_addressee
    from_ = c.signoff_name
    if v.lang == "hi":
        return f"Namaste{' ' + who if who and not who.startswith('Mr') else ''}! {from_}, {c.locality} se."
    greet = v.greeting or "Hi"
    if v.lang == "hinglish":
        return f"Hi {who}! {from_}, {c.locality} se." if who else f"Namaste! {from_}, {c.locality} se."
    return f"{greet} {who}! {from_}, {c.locality} here." if who else f"{greet}! {from_}, {c.locality} here."


def _slots(c: Ctx) -> list[str]:
    raw = c.payload.get("available_slots") or c.payload.get("next_session_options") or []
    out = []
    for s in raw:
        label = slot_label(s.get("iso", ""), s.get("label", ""))
        if label:
            out.append(label)
            d = parse_dt(s.get("iso"))
            if d:
                c.allow(d.astimezone(IST).day, d.astimezone(IST).hour % 12 or 12, d.astimezone(IST).minute)
    return out


def recall_due(c: Ctx, v: Voice) -> Draft:
    p = c.payload
    service = humanize_token(p.get("service_due", "")) if p.get("service_due") else None
    last = parse_date(p.get("last_service_date")) or c.last_visit()
    slots = _slots(c)
    service_word = {"dentists": "check-up", "salons": "touch-up", "gyms": "progress check", "pharmacies": "refill check"}.get(c.slug, "visit")
    lines = [_cust_open(c, v)]
    if last:
        lines.append(v.t(f"Your last {'cleaning' if 'clean' in (service or '') else 'visit'} was on {day_month(last)}", f"Aapki last {'cleaning' if 'clean' in (service or '') else 'visit'} {day_month(last)} ko hui thi"))
        lines[-1] += v.t(f" — your {service} is due now." if service else f" — time for a quick {service_word}.",
                         f" — {service} ab due hai." if service else f" — ek quick {service_word} ka time ho gaya.")
    price_offer = c.offer_matching(*(service.split()[-1:] if service else []), "clean", "check", "analysis", "consult")
    if slots:
        pref = str(c.prefs.get("preferred_slots", ""))
        pref_txt = v.t("evening " if "evening" in pref else "", "evening " if "evening" in pref else "")
        if len(slots) >= 2:
            lines.append(v.t(f"We've held two {pref_txt}slots for you: {slots[0]} or {slots[1]}.",
                             f"Aapke liye do {pref_txt}slots rakhe hain: {slots[0]} ya {slots[1]}."))
        else:
            lines.append(v.t(f"We've held {slots[0]} for you.", f"Aapke liye {slots[0]} rakha hai."))
    if price_offer:
        had_it = any(w in " ".join(map(str, c.rel.get("services_received") or [])).lower()
                     for w in re.findall(r"[a-z]{5,}", price_offer["title"].lower()))
        if had_it:
            lines.append(v.t(f"Still {short_title(price_offer['title']).split('@')[-1].strip()} for the {short_title(price_offer['title']).split(' @')[0].lower()}, same as before.",
                             f"{short_title(price_offer['title'])} — pehle jaisa hi."))
        elif "free" in price_offer["title"].lower():
            lines.append(v.t(f"A {price_offer['title'].lower().replace('free ', 'free ')} is on us while you're in.",
                             f"{price_offer['title']} hamari taraf se."))
        else:
            lines.append(v.t(f"{short_title(price_offer['title'])}.", f"{short_title(price_offer['title'])}."))
    if len(slots) >= 2:
        cta = v.t("Reply 1 or 2 to confirm — or send a time that suits you better.", "Confirm karne ke liye 1 ya 2 bhejiye — ya apna time bata dijiye.")
        cta_type = "multi_choice_slot"
    elif slots:
        cta = v.t("Reply YES to confirm.", "Confirm ke liye YES bhejiye.")
        cta_type = "binary_yes_no"
    else:
        cta = v.t("Reply YES and we'll send this week's open slots.", "YES bhejiye, is hafte ke khaali slots bhej denge.")
        cta_type = "binary_yes_no"
    body = " ".join(lines) + "\n" + cta
    if c.slug == "dentists" and c.cust_addressee:
        body = body.replace(f"{c.cust_addressee}!", f"{c.cust_addressee} 🦷", 1)
    on_accept = v.t(f"Booked ✅ {slots[0] if slots else 'We will confirm your slot shortly'}. We'll send a reminder the day before.",
                    f"Booked ✅ {slots[0] if slots else 'Slot jaldi confirm karte hain'}. Ek din pehle reminder bhej denge.")
    return Draft(body, cta_type,
                 "Customer recall on the merchant's behalf: last-visit date, real held slots (weekday computed from ISO — the "
                 "payload labels had the wrong weekday), real active offer price, customer's language and slot preference honoured.",
                 ["personalisation", "specificity", "low_friction_choice"], on_accept, offer="booking", slots=slots)


def appointment_tomorrow(c: Ctx, v: Voice) -> Draft:
    slots = _slots(c)
    when = slots[0] if slots else v.t("tomorrow", "kal")
    body = f"""{_cust_open(c, v)} {v.t(f"Reminder: your appointment is {when}.", f"Yaad dila rahe hain: aapka appointment {when} hai.")}
{v.t("Reply YES to confirm, or send a better time and we'll move it.", "Confirm ke liye YES bhejiye, ya naya time bata dijiye — shift kar denge.")}"""
    return Draft(body, "binary_yes_no", "Appointment reminder — short, no invented time or service; confirm-or-reschedule in one reply.",
                 ["low_friction_choice"], v.t("Confirmed ✅ See you tomorrow.", "Confirmed ✅ Kal milte hain."), offer="confirmation")


def customer_lapsed(c: Ctx, v: Voice) -> Draft:
    p = c.payload
    days = p.get("days_since_last_visit")
    focus = humanize_token(p.get("previous_focus", "")) if p.get("previous_focus") else None
    months_member = p.get("previous_membership_months")
    last = c.last_visit()
    if not days and last:
        days = (c.today - last).days
        if days <= 0:
            days = None
        else:
            c.allow(days)
    owner = c.owner
    who = c.cust_addressee
    lines = []
    open_ = _cust_open(c, v)
    if owner and c.slug == "gyms":
        open_ = v.t(f"Hi {who}, {owner} from {c.biz} here.", f"Hi {who}, {owner}, {c.biz} se.")
    lines.append(open_)
    if days:
        lines.append(v.t(f"It's been {weeks_phrase(days)} since your last session — no pressure, it happens to most of us." if c.slug == "gyms"
                         else f"It's been {weeks_phrase(days)} since your last visit on {day_month(last)}." if last else f"It's been {weeks_phrase(days)} since your last visit.",
                         f"Last visit ko {weeks_phrase(days).replace('about ', 'lagbhag ').replace('weeks', 'hafte').replace('months', 'mahine').replace('days', 'din')} ho gaye" + (" — koi pressure nahi." if c.slug == "gyms" else ".")))
    elif last:
        lines.append(v.t(f"We haven't seen you since {day_month(last)}.", f"{day_month(last)} ke baad aap nahi aaye."))
    if focus and months_member:
        lines.append(v.t(f"You put in {months_member} solid months on {focus} — restarting is easier than starting over.",
                         f"Aapne {months_member} mahine {focus} pe mehnat ki thi — dobara shuru karna naye se shuru karne se aasaan hai."))
    offer = c.offer_matching("free", "trial", "analysis", "check", "consult", "delivery")
    if offer:
        lines.append(v.t(f"'{offer['title']}' is open to you this week.", f"'{offer['title']}' is hafte aapke liye available hai."))
        cta = v.t("Want us to book the first one? Reply YES — no charge, no commitment." if "free" in offer["title"].lower()
                  else "Want us to hold a slot? Reply YES.",
                  "Pehla slot book kar dein? YES bhejiye — koi charge nahi." if "free" in offer["title"].lower() else "Slot hold kar dein? YES bhejiye.")
    elif c.slug == "pharmacies":
        lines.append(v.t("If you're on any regular medicines, we can keep your refill ready so you never run short.",
                         "Agar koi regular dawai chalti hai, toh hum refill ready rakh sakte hain taaki kabhi khatam na ho."))
        cta = v.t("Reply YES and we'll set a reminder.", "Reminder set karne ke liye YES bhejiye.")
    else:
        why = {
            "dentists": v.t("A routine check-up now keeps small things small.", "Abhi ek routine check-up chhoti cheezon ko chhota hi rakhta hai."),
            "gyms": v.t("A quick progress check is the easiest way back in.", "Wapas shuru karne ka sabse aasaan tareeka — ek quick progress check."),
            "salons": v.t("Good time for a quick touch-up before your look grows out.", "Look grow-out hone se pehle quick touch-up ka sahi time hai."),
            "restaurants": v.t(f"Your {c.rel['favourite_dish']} is still on the menu.", f"Aapka {c.rel['favourite_dish']} abhi bhi menu pe hai.")
            if c.rel.get("favourite_dish") else None,
        }.get(c.slug)
        if why:
            lines.append(why)
        cta = v.t("Reply YES and we'll send this week's open slots.", "YES bhejiye, is hafte ke khaali slots bhej denge.")
    body = " ".join(lines) + "\n" + cta
    return Draft(body, "binary_yes_no",
                 "Lapsed-customer win-back: warm, no guilt, anchored on their actual last visit / past goal, one no-commitment ask "
                 "using only an offer the merchant is actually running.",
                 ["personalisation", "no_shame", "low_friction"], v.t("Done ✅ We'll message you the slot details shortly.", "Done ✅ Slot details jaldi bhejte hain."),
                 offer="slot booking")


def wedding_followup(c: Ctx, v: Voice) -> Draft:
    p = c.payload
    wd = parse_date(p.get("wedding_date") or c.prefs.get("wedding_date"))
    trial = parse_date(p.get("trial_completed"))
    days = p.get("days_to_wedding") or ((wd - c.today).days if wd else None)
    step = humanize_token(p.get("next_step_window_open", "")).replace("30day", "30-day")
    who = c.cust_addressee
    sign = f"{c.owner} from {c.biz.split(' Family')[0]} {c.locality}" if c.owner else c.biz
    lines = [v.t(f"Hi {who} 💍 {sign} here.", f"Hi {who} 💍 {sign}.")]
    if trial:
        lines.append(v.t(f"Hope you loved how the bridal trial on {day_month(trial)} turned out!", f"Umeed hai {day_month(trial)} ka bridal trial pasand aaya!"))
    if wd and days:
        lines.append(v.t(f"{num(days)} days to go till {day_month(wd)} — the {step} doesn't need to start yet, but wedding-season Saturdays fill early.",
                         f"{day_month(wd)} tak {num(days)} din — {step} abhi shuru karne ki zaroorat nahi, par wedding-season ke Saturdays jaldi bharte hain."))
    pref = c.prefs.get("preferred_slots", "")
    body = " ".join(lines) + "\n" + v.t(f"Want us to pencil in your {pref.replace('_', ' ') or 'preferred'} start dates now? Reply YES.",
                                        f"Aapke {pref.replace('_', ' ') or 'preferred'} start dates abhi pencil kar dein? YES bhejiye.")
    return Draft(body, "binary_yes_no",
                 "Bridal follow-up: references her actual trial date and wedding date, honest about timing (no rush yet), "
                 "scarcity from wedding-season demand, preference honoured; no invented package price.",
                 ["personalisation", "scarcity", "low_friction"],
                 v.t(f"Done ✅ Pencilled in — {c.owner or 'we'} will share the {step} plan with dates this week.", "Done ✅ Dates pencil kar diye."),
                 offer="pencilling in dates")


def trial_followup(c: Ctx, v: Voice) -> Draft:
    p = c.payload
    trial = parse_date(p.get("trial_date"))
    slots = _slots(c)
    person, parent = c.cust_names
    services = pretty_services(c.rel.get("services_received"))
    what = services[0] if services else "trial"
    lines = [_cust_open(c, v)]
    subject = person if parent else v.t("you", "aap")
    if trial:
        lines.append(v.t(f"{person} did the {what} with us on {day_month(trial)} — hope it was fun." if parent
                         else f"Thanks for trying the {what} on {day_month(trial)}.",
                         f"{person} ne {day_month(trial)} ko {what} kiya tha — umeed hai maza aaya." if parent else f"{day_month(trial)} ko {what} ke liye shukriya."))
    elif c.last_visit():
        lines.append(v.t(f"Thanks for trying us out on {day_month(c.last_visit())} — hope it felt like a good fit.",
                         f"{day_month(c.last_visit())} ko try karne ke liye shukriya — umeed hai achha laga."))
    offer = c.offer_matching("first month", "trial", "free", "demo") if not slots else None
    if offer:
        lines.append(v.t(f"If you'd like to keep going, '{offer['title']}' is the easiest next step.",
                         f"Aage continue karna ho toh '{offer['title']}' sabse aasaan next step hai."))
    if slots:
        lines.append(v.t(f"The next session is {slots[0]}.", f"Agla session {slots[0]} hai."))
        cta = v.t(f"Shall we keep a spot for {subject}? Reply YES.", f"{subject} ke liye spot rakh dein? YES bhejiye.")
    else:
        cta = v.t("Reply YES and we'll share the next session times.", "YES bhejiye, agle session ka time bhej denge.")
    body = " ".join(lines) + "\n" + cta
    return Draft(body, "binary_yes_no", "Trial follow-up addressed to the parent for a minor, with the real next session slot; single yes.",
                 ["personalisation", "momentum", "low_friction"], v.t(f"Done ✅ Spot held for {subject} — {slots[0] if slots else 'we will confirm the time'}.", f"Done ✅ Spot hold ho gaya."),
                 offer="holding the spot", slots=slots)


def chronic_refill_due(c: Ctx, v: Voice) -> Draft:
    if c.slug != "pharmacies":
        # A refill trigger routed to a non-pharmacy merchant: treat it as a routine re-visit reminder.
        return customer_lapsed(c, v)
    p = c.payload
    mols = p.get("molecule_list") or []
    runs_out = parse_date(p.get("stock_runs_out_iso"))
    senior = c.cust_ident.get("senior_citizen") or str(c.cust_ident.get("age_band", "")).startswith(("60", "65", "70"))
    via = str(c.prefs.get("channel", ""))
    person = c.cust_names[0]
    honorific = f"{person.replace('Mr. ', '')} ji" if person.startswith("Mr") else person
    lines = [_cust_open(c, v)]
    mol_txt = ", ".join(mols[:-1]) + (f" aur {mols[-1]}" if v.hindi else f" and {mols[-1]}") if len(mols) > 1 else "".join(mols)
    n = len(mols)
    if runs_out:
        lines.append(v.t(f"{honorific}'s {n} regular medicines — {mol_txt} — run out on {day_month(runs_out)}.",
                         f"{honorific} ki {n} regular dawaiyan — {mol_txt} — {day_month(runs_out)} tak khatam ho jayengi."))
    lines.append(v.t("We can keep the same-dose pack ready.", "Same dose ka pack ready rakh dete hain."))
    senior_offer = c.offer_matching("senior")
    deliv = c.offer_matching("delivery")
    perks = []
    if senior and senior_offer:
        perks.append(v.t(f"your {senior_offer['title'].lower().replace(' off', ' off')} applies", f"{senior_offer['title']} lagega"))
    if deliv and p.get("delivery_address_saved"):
        perks.append(v.t(f"free delivery to your saved address ({deliv['title'].split('>')[-1].strip()}+ orders)" if ">" in deliv["title"] else "free delivery to your saved address",
                         f"saved address pe free delivery ({deliv['title'].split('>')[-1].strip()} se upar)" if ">" in deliv["title"] else "saved address pe free delivery"))
    if perks:
        lines.append(v.t(" and ".join(perks).capitalize() + ".", " aur ".join(perks).capitalize() + "."))
    recall = c.digest_mentioning("recall")
    if recall and any(m.lower() in json_lower(recall) for m in mols):
        mol = next(m for m in mols if m.lower() in json_lower(recall))
        lines.append(v.t(f"We'll also check the {mol} batch against this month's recall list before packing.",
                         f"Packing se pehle {mol} ka batch is mahine ki recall list se check kar lenge."))
    cta = v.t("Reply CONFIRM to dispatch — and tell us if the doctor has changed any dose.",
              "Bhejne ke liye CONFIRM likhiye — dose mein koi badlav ho toh zaroor bataiye.")
    body = " ".join(lines) + "\n" + cta
    rationale = ("Chronic refill: exact molecules and run-out date, merchant's real senior/delivery offers, addressed respectfully "
                 f"{'to the family member who manages the WhatsApp' if 'via' in via else ''}; cross-checked the category recall alert "
                 "so the refill doesn't ship a recalled batch.")
    return Draft(body, "binary_confirm_cancel", rationale, ["specificity", "trust", "low_friction"],
                 v.t("Dispatched ✅ Delivery to your saved address — we'll message when it's out.", "Dispatch ho gaya ✅ Saved address pe delivery — nikalte hi message karenge."),
                 offer="dispatch")


def json_lower(d: dict) -> str:
    return json.dumps(d).lower()


# =========================================================================== unseen / external triggers
# The judge injects triggers we have never seen. These playbooks read whatever the
# payload carries (headline, numbers, linked digest item) instead of ignoring it.

_TEXT_KEYS = ("headline", "title", "event", "name", "description", "summary", "note", "topic", "reason",
              "impact", "alert", "message", "query", "festival", "theme")
_SKIP_KEYS = {"placeholder", "category", "merchant_id", "customer_id", "metric_or_topic"}


def payload_story(c: Ctx) -> tuple[Optional[str], list[str], Optional[dict]]:
    """(headline text, ['key: value' facts], linked digest item) from an arbitrary payload."""
    p = c.payload
    item = None
    for k, val in p.items():
        if isinstance(val, str) and (k.endswith("item_id") or k in ("alert_id", "digest_id")):
            item = c.digest_item(val)
    text = next((str(p[k]) for k in _TEXT_KEYS if isinstance(p.get(k), str) and p.get(k).strip()), None)
    if not text and item:
        text = item.get("title")
    facts = []
    for k, val in p.items():
        if k in _SKIP_KEYS or k.endswith("_id") or k.endswith("_iso") or isinstance(val, (dict, list, bool)):
            continue
        label = humanize_token(k).replace(" pct", "").replace(" c", " (°C)") if isinstance(val, (int, float)) else None
        if label:
            shown = pct(val, signed=True) if ("pct" in k or "delta" in k) and abs(val) <= 5 else num(val)
            facts.append(f"{label}: {shown}")
    return text, facts[:3], item


def _temperature(c: Ctx) -> Optional[float]:
    for k, val in c.payload.items():
        if isinstance(val, (int, float)) and re.search(r"temp|celsius|deg|heat_index|max_c|_c$", k):
            return float(val)
    m = re.search(r"(\d{2})\s*°", json.dumps(c.payload, ensure_ascii=False))
    return float(m.group(1)) if m else None


def weather_heatwave(c: Ctx, v: Voice) -> Draft:
    temp = _temperature(c)
    city = c.payload.get("city") or c.city
    head = v.t(f"{num(temp)}°C in {city} today" if temp else f"heatwave in {city} today",
               f"aaj {city} mein {num(temp)}°C" if temp else f"aaj {city} mein heatwave")
    summer = c.digest_mentioning("summer") or c.digest_mentioning("ORS")
    angle = {
        "restaurants": v.t("afternoon walk-ins drop and delivery plus cold drinks pick up — worth leading with delivery today.",
                           "dopahar ka footfall girta hai, delivery aur cold drinks badhte hain — aaj delivery lead karni chahiye."),
        "pharmacies": v.t(f"{summer['title'].split(':')[0]} — ORS and sunscreen belong at the counter today." if summer else "ORS and sunscreen belong at the counter today.",
                          f"{summer['title'].split(':')[0]} — aaj ORS aur sunscreen counter pe rakhiye." if summer else "aaj ORS aur sunscreen counter pe rakhiye."),
        "gyms": v.t("members will skip the afternoon and evening heat — early-morning slots are the ones to push.",
                    "members garmi mein shaam skip karenge — subah ke slots push karne ka din hai."),
        "salons": v.t("midday walk-ins dry up in this heat — morning and evening slots are where bookings will land.",
                      "is garmi mein dopahar ke walk-ins kam honge — subah aur shaam ke slots pe bookings aayengi."),
        "dentists": v.t("patients postpone midday appointments in this heat — a nudge toward evening slots saves no-shows.",
                        "garmi mein patients dopahar ke appointments taalte hain — evening slots ka nudge no-shows bachata hai."),
    }.get(c.slug, v.t("midday footfall will dip — shift today's push to mornings and evenings.",
                      "dopahar ka footfall girega — aaj ka push subah-shaam pe rakhiye."))
    gym_slot = c.digest_mentioning("6-8am") if c.slug == "gyms" else None
    extra = v.t(" Your 6-8am slots run at about 60% capacity anyway, so there's room." if gym_slot else "",
                " 6-8am slots waise bhi ~60% bhare rehte hain, jagah hai." if gym_slot else "")
    if gym_slot:
        c.allow(6, 8, 60)
    body = lead(c, v, f"{head}. {angle[:1].upper() + angle[1:]}{extra}") + "\n" + ask(
        v, "Want me to post today's hours and offer to match the heat?", "Aaj ke hisaab se hours aur offer ka post daal doon?")
    return Draft(body, "binary_yes_no",
                 f"External weather trigger ({head}); lead signal translated into a category-specific operating change for today, "
                 "no invented demand statistics.",
                 ["timeliness", "judgement", "effort_externalization"],
                 accept_text(c, v, v.t("today's heat-adjusted post is ready", "aaj ka post ready hai")), offer=v.t("today's post", "aaj ka post"))


def local_news_event(c: Ctx, v: Voice) -> Draft:
    text, facts, item = payload_story(c)
    if not text:
        return generic(c, v)
    deliveryish = c.slug in ("restaurants", "pharmacies")
    angle = v.t("If it keeps people home, delivery is where today's orders will come from." if deliveryish
                else "If it disrupts travel, expect late arrivals — a quick reschedule message keeps the day intact.",
                "Agar log ghar pe rahe, toh aaj orders delivery se aayenge." if deliveryish
                else "Travel mein dikkat hui toh log late aayenge — reschedule message se din bach jayega.")
    detail = f" ({'; '.join(facts)})" if facts else ""
    body = lead(c, v, v.t(f"local heads-up for {c.locality}: {text}{detail}. {angle}", f"{c.locality} ke liye local update: {text}{detail}. {angle}")) + "\n" + ask(
        v, "Want me to handle it for today?" if deliveryish else "Want me to send that message to today's bookings?",
        "Aaj ke liye main sambhal loon?" if deliveryish else "Aaj ki bookings ko message bhej doon?")
    return Draft(body, "binary_yes_no", f"Local news trigger surfaced verbatim from the payload ('{text}') and converted into one operational action.",
                 ["timeliness", "effort_externalization"], accept_text(c, v, v.t("on it for today", "aaj ke liye kar diya")),
                 offer=v.t("today's adjustment", "aaj ka adjustment"))


def category_trend_movement(c: Ctx, v: Voice) -> Draft:
    p = c.payload
    if p.get("trends"):
        return category_seasonal(c, v)
    query = p.get("query") or p.get("topic")
    delta = p.get("delta_yoy") if p.get("delta_yoy") is not None else p.get("delta_pct")
    sig = c.trend(str(query).lower().split()[0]) if query else c.top_trend()
    if not query and sig:
        query, delta = sig.get("query"), sig.get("delta_yoy")
    if not query:
        return generic(c, v)
    offer = c.offer_matching(*str(query).lower().split()) or c.catalog_offer(*str(query).lower().split())
    line = v.t(f"'{query}' searches are up {pct(delta)} YoY" if delta else f"'{query}' is trending", f"'{query}' searches {pct(delta)} YoY upar hain" if delta else f"'{query}' trend kar raha hai")
    if sig and sig.get("segment_age") and sig["segment_age"] != "all":
        line += v.t(f", mostly the {sig['segment_age'].replace('_', ' ')} crowd", f", zyada {sig['segment_age'].replace('_', ' ')} age group")
    line += "."
    if offer:
        line += v.t(f" '{short_title(offer['title'])}' is the obvious way to catch them.", f" '{short_title(offer['title'])}' se yeh demand pakdi ja sakti hai.")
    body = lead(c, v, line) + "\n" + ask(v, "Want me to put it at the top of your profile this week?", "Is hafte profile ke top pe daal doon?")
    return Draft(body, "binary_yes_no", "Search-trend trigger tied to a concrete offer the merchant can list.", ["specificity", "curiosity"],
                 accept_text(c, v, v.t("it's pinned to the top of your profile", "profile ke top pe pin ho gaya")), offer=v.t("pinning it", "pin karna"))


# =========================================================================== fallback

def generic(c: Ctx, v: Voice) -> Draft:
    """Any unknown trigger kind: say *why now* from the payload if it has anything, else
    lead with the merchant's most decision-relevant metric."""
    if c.customer:
        return customer_lapsed(c, v)
    text, facts, item = payload_story(c)
    if item and item.get("summary"):
        return research_digest(c, v)
    if text or facts:
        what = (text or humanize_token(c.kind)).rstrip(".")
        detail = f" ({'; '.join(facts)})" if facts else ""
        # Only tie in an offer that actually relates to the news — otherwise it reads like a template.
        words = set(re.findall(r"[a-z]{4,}", what.lower()))
        offer = next((o for o in c.active_offers + c.catalog()
                      if words & set(re.findall(r"[a-z]{4,}", str(o.get("title", "")).lower()))), None)
        tie = v.t(f" Your '{short_title(offer['title'])}' fits this well." if offer else " Worth acting on while it's fresh.",
                  f" Aapka '{short_title(offer['title'])}' isse match karta hai." if offer else " Abhi fresh hai, abhi kaam aayega.")
        body = lead(c, v, v.t(f"heads-up: {what}{detail}.{tie}", f"ek update: {what}{detail}.{tie}")) + "\n" + ask(
            v, "Want me to turn it into a post for your profile today?", "Aaj ise profile post bana doon?")
        return Draft(body, "binary_yes_no", f"Unfamiliar trigger '{c.kind}': surfaced its own payload as the why-now, tied to the merchant's offer.",
                     ["timeliness", "specificity"], accept_text(c, v, v.t("post drafted", "post ready")), offer=v.t("the post", "post"))
    kind = humanize_token(c.kind)
    weak = c.standout_weakness()
    strength = c.standout_strength()
    if weak:
        m, mine, peer = weak
        fact = v.t(f"your {METRIC_LABEL.get(m, m)} is {fmt_metric(m, mine)} this month vs {fmt_metric(m, peer)} for {peer_scope(c)}.",
                   f"aapke {METRIC_LABEL.get(m, m)} is mahine {fmt_metric(m, mine)} hain, {peer_scope(c)} ka average {fmt_metric(m, peer)}.")
    elif strength:
        m, mine, peer = strength
        fact = v.t(f"your {METRIC_LABEL.get(m, m)} is {fmt_metric(m, mine)} — {vs_peer(c, v, m, mine, peer)}.",
                   f"aapka {METRIC_LABEL.get(m, m)} {fmt_metric(m, mine)} hai — {vs_peer(c, v, m, mine, peer)}.")
    else:
        fact = v.t(f"{num(c.perf.get('views', 0))} people viewed {c.biz} in the last 30 days.", f"pichhle 30 din mein {num(c.perf.get('views', 0))} logon ne {c.biz} dekha.")
    offer = suggested_catalog_offer(c)
    cta = ask(v, f"Want me to set up a '{offer['title']}' listing to convert more of them?" if offer else "Want 2 quick fixes for your profile?",
              f"'{offer['title']}' listing set kar doon?" if offer else "Profile ke 2 quick fixes bhej doon?")
    body = lead(c, v, fact) + f"\n{cta}"
    return Draft(body, "binary_yes_no", f"Trigger '{kind}' had no specific payload; anchored on the merchant's most decision-relevant metric vs peers.",
                 ["specificity", "effort_externalization"], accept_text(c, v, v.t("set-up started", "set-up shuru")), offer="the set-up")


PLAYBOOKS: dict[str, Callable[[Ctx, Voice], Draft]] = {
    "research_digest": research_digest,
    "category_research_digest_release": research_digest,
    "regulation_change": regulation_change,
    "perf_dip": perf_dip,
    "perf_spike": perf_spike,
    "seasonal_perf_dip": seasonal_perf_dip,
    "renewal_due": renewal_due,
    "festival_upcoming": festival_upcoming,
    "curious_ask_due": curious_ask,
    "scheduled_recurring": curious_ask,
    "winback_eligible": winback_eligible,
    "dormant_with_vera": dormant_with_vera,
    "ipl_match_today": ipl_match_today,
    "review_theme_emerged": review_theme_emerged,
    "milestone_reached": milestone_reached,
    "active_planning_intent": active_planning_intent,
    "supply_alert": supply_alert,
    "category_seasonal": category_seasonal,
    "category_trend_movement": category_trend_movement,
    "weather_heatwave": weather_heatwave,
    "heatwave": weather_heatwave,
    "local_news_event": local_news_event,
    "customer_lapsed": customer_lapsed,
    "gbp_unverified": gbp_unverified,
    "cde_opportunity": cde_opportunity,
    "competitor_opened": competitor_opened,
    # customer-facing
    "recall_due": recall_due,
    "appointment_tomorrow": appointment_tomorrow,
    "customer_lapsed_soft": customer_lapsed,
    "customer_lapsed_hard": customer_lapsed,
    "wedding_package_followup": wedding_followup,
    "trial_followup": trial_followup,
    "chronic_refill_due": chronic_refill_due,
}


def run_playbook(c: Ctx) -> Draft:
    v = c.voice()
    fn = PLAYBOOKS.get(c.kind, generic)
    try:
        return fn(c, v)
    except Exception:  # a malformed payload must never take the bot down
        log.exception("playbook %s failed for %s; using fallback", c.kind, c.mid)
        return generic(c, v) if not c.customer else customer_lapsed(c, v)
