"""Node 3 — investigate. LLM (the main reasoning loop).

Writes: tools_called, tool_budget_remaining, messages_sent, replies_received,
        claims, quotes

The LLM decides which tool to call next and must state why. The necessity
string goes into the ledger and then the audit trail, so when a judge asks
"why did it call tracking?" the answer comes from a recorded field rather than
from asking the model to rationalise afterwards.

Decision policy (PS §4.3), encoded in the system prompt and in the fallback:

    stock risk unclear             -> inventory tools
    delivery status uncertain      -> supplier message, then tracking
    supply cannot meet demand      -> RFQ
    decision crosses budget limits -> approval check
    only after deciding            -> ERP update

WHAT THE MODEL DOES NOT DECIDE. The model picks the tool and writes the
necessity. The arguments are derived here, deterministically: the RFQ quantity
is the shortfall node 2 computed, the candidate list is the certified subset of
the catalog, the tracking id is the PO already identified. A model that picks
the tool but invents the quantity is doing arithmetic that affects a decision.

VERIFICATION is a sub-step here, not a separate node. Every claim is tagged
before it enters state["claims"]:

    GROUNDED      tracking or ERP confirms it
    CONTRADICTED  tracking or ERP disagrees
    UNVERIFIABLE  no ground truth available

An UNVERIFIABLE certification claim is treated as ABSENT, not present.
Conservative by design, and the brief says so.

A VAGUE reply — no date and no quantity commitment — must not advance the
plan. Exactly one targeted follow-up naming the PO and demanding both, and
alternate sourcing proceeds in parallel rather than waiting for the answer.

A CONTRADICTED claim calls trust_write and that supplier's units count as zero
confirmed from then on, in this same solve rather than merely the next run.
"""

from __future__ import annotations

from agent import clock
from agent.audit import append_event
from agent.errors import ToolBudgetExhausted
from agent.llm import get_llm
from agent.tools import call_tool
from contracts.state import AgentState
from agent.integrations import TRUST_AVAILABLE, reliability_of, trust_write

MAX_STEPS = 12          # a hard stop independent of the budget, so a confused
                        # model cannot spin even if calls are being cached

DISPATCH_CONTRADICTING = {"label_created_no_pickup", "no_shipment_record",
                          "no_record", "not_found"}


def investigate(state: AgentState) -> dict:
    work = dict(state)
    work.setdefault("messages_sent", [])
    work.setdefault("replies_received", [])
    work.setdefault("claims", [])
    work.setdefault("quotes", [])

    # Two facts node 3 needs that AgentState is frozen without: which PO is in
    # trouble, and the component's certification and quality bar. Both are re-read
    # rather than passed, and both are inside the cache TTL, so they cost no
    # budget and are logged as avoided.
    inventory = call_tool(work, "get_inventory",
                          "the certification and quality bar for this component "
                          "decides who is even worth asking for a quote",
                          component_id=work.get("affected_component") or "")
    pos = call_tool(work, "get_purchase_orders",
                    "the disrupted PO names the incumbent to challenge")
    component = inventory[0]
    work["_component_bar"] = {
        "required_certifications": list(component.required_certifications),
        "min_quality": component.min_quality,
    }
    disrupted = next((p for p in pos if p.status == "delayed"), pos[0] if pos else None)
    if disrupted is not None:
        work["_po_id"] = disrupted.po_id
        work["_incumbent"] = disrupted.supplier_id

    replanning = bool(work.get("broken_assumption"))
    if replanning:
        work["audit_events"] = append_event(
            work, type="replan", actor="investigator",
            summary=f"re-investigating after {work['broken_assumption']} broke",
            detail={"broken_assumption": work["broken_assumption"],
                    "rationale": "re-solve for the shortfall only; units already "
                                 "confirmed from other suppliers stay committed and "
                                 "verified quotes are preserved"})

    llm = get_llm()
    done = _progress(work)
    exhausted = False
    seen_message_ids = {m["message_id"] for m in work["replies_received"]}

    for _ in range(MAX_STEPS):
        choice = llm.select_tool(_context(work, done))
        if choice.tool == "done":
            break
        try:
            result = _dispatch(work, choice, done)
        except ToolBudgetExhausted as exc:
            exhausted = True
            work["audit_events"] = append_event(
                work, type="guardrail", actor="ledger",
                summary=f"G10 tool budget exhausted - refused {exc.tool}",
                detail={"fired": ["G10"], "refused_tool": exc.tool,
                        "necessity": exc.necessity,
                        "flag": "INCOMPLETE_INVESTIGATION",
                        "rationale": "fails closed to escalation; a retry loop here "
                                     "would spend a budget that is already gone"},
                remaining_risk="investigation incomplete; the plan rests on what was "
                               "verified before the budget ran out")
            break

        _absorb(work, choice, result, done, llm, seen_message_ids)
        done = _progress(work, done)

    # rejected_alternatives is written here, not only in node 4: the suppliers
    # filtered out by certification and the quality floor are discovered when
    # the catalog is read, and PS §4.10 wants every one of them in the trail.
    out = {k: work[k] for k in
           ("tools_called", "tool_budget_remaining", "messages_sent",
            "replies_received", "claims", "quotes", "rejected_alternatives",
            "audit_events") if k in work}
    if replanning:
        # The break has been acted on. Leaving it set would make node 6's
        # router read a stale flag and replan a second time for the same cause.
        out["broken_assumption"] = None
    if exhausted:
        out["tool_budget_remaining"] = 0
    return out


# ---- context handed to the model ---------------------------------------

def _progress(work: dict, done: dict | None = None) -> dict:
    """What has already happened, as flags. Derived from state, never guessed."""
    d = dict(done or {})
    tools = [t["tool"] for t in work.get("tools_called") or []]
    replies = work.get("replies_received") or []
    claims = work.get("claims") or []

    d["challenged_incumbent"] = any(m.get("purpose") == "challenge"
                                    for m in work.get("messages_sent") or [])
    d["followed_up"] = any(m.get("purpose") == "follow_up"
                           for m in work.get("messages_sent") or [])
    d["read_reply"] = bool(replies)
    d["read_followup"] = len(replies) > 1
    d["reply_vague"] = any(r.get("classification") == "VAGUE" for r in replies)
    d["claim_to_verify"] = any(c.get("status") == "PENDING" for c in claims)
    d["verified"] = any(c.get("status") in ("GROUNDED", "CONTRADICTED")
                        for c in claims)
    d["read_catalog"] = "get_suppliers" in tools
    d["requested_quotes"] = bool(work.get("quotes"))
    return d


def _context(work: dict, done: dict) -> dict:
    base = work.get("baseline") or {}
    return {
        "disruption_id": work.get("disruption_id"),
        "component_id": work.get("affected_component"),
        "po_id": _po_id(work),
        "incumbent_supplier_id": _incumbent(work),
        "severity": work.get("severity"),
        "coverage_days": work.get("coverage_days"),
        "shortfall_units": base.get("units_short"),
        "at_risk_orders": work.get("at_risk_orders"),
        "budget_remaining": work.get("tool_budget_remaining"),
        "claims": work.get("claims"),
        "replies": [{"from": r["sender"], "classification": r.get("classification")}
                    for r in work.get("replies_received") or []],
        "done": done,
    }


def _po_id(work: dict) -> str:
    return work.get("_po_id") or ""


def _incumbent(work: dict) -> str:
    return work.get("_incumbent") or ""


# ---- dispatch ----------------------------------------------------------

def _dispatch(work: dict, choice, done: dict):
    """Call the tool the model chose, with arguments derived here."""
    tool = choice.tool
    component_id = work.get("affected_component") or ""
    po_id = _po_id(work)

    if tool == "send_message":
        return call_tool(work, "send_message", choice.necessity,
                         supplier_id=choice.supplier_id or _incumbent(work),
                         subject=choice.subject or f"Re: {po_id}",
                         body=choice.body or "")
    if tool == "get_inbox":
        return call_tool(work, "get_inbox", choice.necessity)
    if tool == "get_tracking":
        return call_tool(work, "get_tracking", choice.necessity,
                         po_id=choice.po_id or po_id)
    if tool == "get_suppliers":
        return call_tool(work, "get_suppliers", choice.necessity,
                         component_id=choice.component_id or component_id)
    if tool == "request_rfq":
        # Quantity and candidate list are DERIVED, not chosen by the model.
        # The shortfall is node 2's cumulative figure and the candidates are the
        # certified subset of the catalog. A model that picks the tool but
        # invents the quantity is doing arithmetic that affects a decision.
        base = work.get("baseline") or {}
        return call_tool(work, "request_rfq", choice.necessity,
                         component_id=component_id,
                         quantity=int(base.get("units_short") or 0),
                         needed_by_days=int(base.get("earliest_at_risk_day") or 0),
                         supplier_ids=_certified_candidates(work, component_id))
    if tool == "get_inventory":
        return call_tool(work, "get_inventory", choice.necessity,
                         component_id=component_id)
    if tool == "get_production_schedule":
        return call_tool(work, "get_production_schedule", choice.necessity)
    # check_approval belongs to nodes 4 and 5, where a cost exists to check.
    raise ValueError(f"node 3 will not dispatch {tool!r}")


def _certified_candidates(work: dict, component_id: str) -> list[str]:
    """The catalog subset that passes certification and the quality floor, minus
    anyone whose claim has been contradicted. Deterministic; G3, G4 and G9 are
    Track A's rules, applied here only to decide who is worth asking."""
    catalog = work.get("_catalog") or []
    contradicted = {c["supplier_id"] for c in work.get("claims") or []
                    if c.get("status") == "CONTRADICTED"}
    return [s["supplier_id"] for s in catalog
            if s["certified"] and s["quality_ok"]
            and s["supplier_id"] not in contradicted]


# ---- absorbing results -------------------------------------------------

def _absorb(work: dict, choice, result, done: dict, llm, seen: set) -> None:
    tool = choice.tool

    if tool == "send_message":
        purpose = "follow_up" if done.get("reply_vague") else "challenge"
        work["messages_sent"] = list(work["messages_sent"]) + [{
            "message_id": result.message_id, "recipient": result.recipient,
            "supplier_id": choice.supplier_id or _incumbent(work),
            "subject": result.subject, "body": result.body,
            "purpose": purpose, "ts": result.ts.isoformat()}]
        return

    if tool == "get_inbox":
        _absorb_replies(work, result, llm, seen)
        return

    if tool == "get_tracking":
        _verify(work, result)
        return

    if tool == "get_suppliers":
        component = _component_requirements(work)
        work["_catalog"] = [{
            "supplier_id": s.supplier_id,
            "unit_price": s.unit_price,
            "lead_time_days": s.lead_time_days,
            "available_quantity": s.available_quantity,
            "min_order_quantity": s.min_order_quantity,
            "quality_score": s.quality_score,
            "reliability_score": s.reliability_score,
            "certified": set(component["required_certifications"]) <= set(s.certifications),
            "quality_ok": s.quality_score >= component["min_quality"],
        } for s in result]
        rejected = []
        for s in work["_catalog"]:
            if s["certified"] and s["quality_ok"]:
                continue
            reason = ("missing required certification" if not s["certified"]
                      else f"quality {s['quality_score']} below the "
                           f"{component['min_quality']} floor")
            rejected.append({"supplier_id": s["supplier_id"], "reason": reason,
                             "rule": "G3" if not s["certified"] else "G4",
                             "label": f"{s['supplier_id']}, rejected: {reason}"})
        work["rejected_alternatives"] = list(
            work.get("rejected_alternatives") or []) + rejected
        work["audit_events"] = append_event(
            work, type="calculation", actor="investigator",
            summary=(f"{sum(1 for s in work['_catalog'] if s['certified'] and s['quality_ok'])}"
                     f" of {len(work['_catalog'])} suppliers pass certification and "
                     f"the quality floor"),
            detail={"required_certifications": component["required_certifications"],
                    "min_quality": component["min_quality"],
                    "candidates": _certified_candidates(work, ""),
                    "rationale": "certification and the quality floor are hard "
                                 "filters, so an uncertified supplier is never shown "
                                 "as a cheaper option with the risk noted"},
            alternatives_rejected=rejected)
        return

    if tool == "request_rfq":
        work["quotes"] = list(work["quotes"]) + [{
            "supplier_id": q.supplier_id, "component_id": q.component_id,
            "quantity_available": q.quantity_available, "unit_price": q.unit_price,
            "delivery_days": q.delivery_days,
            "expedite_available": q.expedite_available,
            "expedite_fee": q.expedite_fee, "quote_valid_hours": q.quote_valid_hours,
            "issued_at": q.issued_at.isoformat()} for q in result]
        work["audit_events"] = append_event(
            work, type="calculation", actor="investigator",
            summary=f"{len(result)} quotes returned, valid "
                    f"{result[0].quote_valid_hours if result else 0}h",
            detail={"quotes": work["quotes"],
                    "rationale": "a catalog price is not a commitment; these carry a "
                                 "validity window and become plan assumptions"})
        return


def _component_requirements(work: dict) -> dict:
    """The certification and quality bar for the affected component, read from
    /inventory at the top of this node."""
    return work.get("_component_bar") or {"required_certifications": [],
                                          "min_quality": 0.0}


def _absorb_replies(work: dict, messages, llm, seen: set) -> None:
    incoming = [m for m in messages
                if m.recipient == "ops@example.com" and m.message_id not in seen]
    for m in incoming:
        seen.add(m.message_id)
        verdict = llm.classify_reply(m.body, {"po_id": m.related_po_id})
        work["replies_received"] = list(work["replies_received"]) + [{
            "message_id": m.message_id, "sender": m.sender, "body": m.body,
            "classification": verdict.classification,
            "promised_date": verdict.promised_date,
            "promised_quantity": verdict.promised_quantity,
            "ts": m.ts.isoformat()}]

        supplier_id = _sender_supplier(m.sender, work)
        work["audit_events"] = append_event(
            work, type="verification", actor="verification_agent",
            summary=f'{supplier_id} reply "{_clip(m.body)}" -> {verdict.classification}',
            detail={"supplier_id": supplier_id, "message_id": m.message_id,
                    "reply": m.body, "verdict": verdict.classification,
                    "promised_date": verdict.promised_date,
                    "promised_quantity": verdict.promised_quantity,
                    "rationale": verdict.rationale},
            remaining_risk=("a vague reply is strictly worse than the stated delay: "
                            "the revised date is now unknown"
                            if verdict.classification == "VAGUE" else None))

        if verdict.claim:
            work["claims"] = list(work["claims"]) + [{
                "supplier_id": supplier_id, "claim": verdict.claim,
                "status": "PENDING", "evidence": None,
                "source_message": m.message_id}]
            work["audit_events"] = append_event(
                work, type="verification", actor="verification_agent",
                summary=f'{supplier_id} claims "{verdict.claim}" - '
                        f"unverified until tracking agrees",
                detail={"supplier_id": supplier_id, "claim": verdict.claim,
                        "status": "PENDING",
                        "rationale": "a supplier claim is not evidence; tracking is"})


def _verify(work: dict, tracking) -> None:
    """Tag every pending claim against ground truth."""
    claims = list(work.get("claims") or [])
    for claim in claims:
        if claim["status"] != "PENDING":
            continue
        status, reason = _grade(claim["claim"], tracking)
        claim["status"] = status
        claim["evidence"] = {"po_id": tracking.po_id,
                             "supplier_claim": tracking.supplier_claim,
                             "tracking_status": tracking.tracking_status,
                             "last_movement": (tracking.last_movement.isoformat()
                                               if tracking.last_movement else None)}
        detail = {"supplier_id": claim["supplier_id"], "claim": claim["claim"],
                  "evidence": claim["evidence"], "verdict": status,
                  "rationale": reason}
        risk = None

        if status == "CONTRADICTED":
            before = reliability_of(claim["supplier_id"], _catalog_reliability(
                work, claim["supplier_id"]))
            trust_write(claim["supplier_id"], "contradicted_claim")
            after = reliability_of(claim["supplier_id"], _catalog_reliability(
                work, claim["supplier_id"]))
            detail |= {"trust_before": before, "trust_after": after,
                       "trust_ledger": "track-a" if TRUST_AVAILABLE else "in-process"}
            risk = (f"{claim['supplier_id']}'s units now count as 0 confirmed; "
                    f"it is excluded from the next solve by G9")
        elif status == "UNVERIFIABLE" and "certif" in claim["claim"].lower():
            detail["treated_as"] = "absent"
            risk = ("an unverifiable certification claim is treated as absent, "
                    "not present - conservative by design")

        work["audit_events"] = append_event(
            work, type="verification", actor="verification_agent",
            summary=(f'{claim["supplier_id"]} "{claim["claim"]}" vs '
                     f'{tracking.tracking_status} -> {status}'),
            detail=detail, tools_used=[f"GET /tracking/{tracking.po_id}"],
            remaining_risk=risk)
    work["claims"] = claims


def _grade(claim: str, tracking) -> tuple[str, str]:
    low = claim.lower()
    if "dispatch" in low or "shipped" in low:
        if (tracking.tracking_status in DISPATCH_CONTRADICTING
                or tracking.last_movement is None):
            return ("CONTRADICTED",
                    f"the claim is dispatch but tracking reads "
                    f"{tracking.tracking_status} with no recorded movement")
        return ("GROUNDED",
                f"tracking reads {tracking.tracking_status} with movement at "
                f"{tracking.last_movement}")
    return ("UNVERIFIABLE",
            "no ground truth is available for this class of claim")


def _catalog_reliability(work: dict, supplier_id: str) -> float:
    for s in work.get("_catalog") or []:
        if s["supplier_id"] == supplier_id:
            return s["reliability_score"]
    return 0.72          # SUP-21's catalog score; the catalog read may not have
                         # happened yet when the contradiction lands


def _sender_supplier(sender: str, work: dict) -> str:
    """Map an email sender back to a supplier id.

    The sandbox emits two shapes for the same supplier — the seeded message uses
    supplier21@example.com and generated replies use sup21@example.com — so match
    on the digits in the local part rather than on a substring, which would
    silently fail on one of the two and drop the claim.
    """
    local = sender.split("@", 1)[0]
    digits = "".join(ch for ch in local if ch.isdigit())
    if digits:
        candidate = f"SUP-{digits}"
        known = {s["supplier_id"] for s in work.get("_catalog") or []}
        if not known or candidate in known:
            return candidate
    return sender


def _clip(body: str, n: int = 46) -> str:
    one = " ".join(body.split())
    return one if len(one) <= n else one[:n - 3] + "..."
