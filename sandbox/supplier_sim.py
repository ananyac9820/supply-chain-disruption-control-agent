"""Scripted supplier replies — the three personas of §4.8.

Assigned at seed time, stored on `suppliers.persona`:

    honest         a specific date and a confirmed quantity, within one tick
    vague          no date and no quantity, until challenged properly
    contradictory  claims dispatch while tracking says otherwise

The vague persona is what makes PS §4.4's "challenge vague or contradictory
supplier replies" demonstrable at all: without it there is nothing to
challenge, and the agent's follow-up looks like ceremony.

Follow-up detection is a plain keyword check, deliberately. It needs to be
reliable on stage, not clever — a classifier that is right 90% of the time is
wrong once per three demos.
"""

import re
from datetime import timedelta

from sandbox import db

TICK = db.SIM_TICK      # one definition, shared with the stub

HONEST_REPLY = ("Confirmed: {quantity} units, dispatch on 2026-09-04, "
                "delivery by 2026-09-08.")
VAGUE_FIRST = "We are looking into this and will update you soon."
VAGUE_SPECIFIC = ("Understood. Confirmed: 300 units, dispatch 2026-09-05, "
                  "delivery by 2026-09-09.")
DISPATCH_CLAIM = ("Your order has been dispatched from our facility today. "
                  "Tracking will update shortly.")
REVISED_DOWN = ("On review, only 250 units have left our facility. "
                "The balance is still in production.")


# Word-bounded on purpose. A plain substring test for "date" also matches
# "update", so "Any update?" — the least specific follow-up there is — would
# count as a challenge and the vague persona would answer it. That is the
# demo failing in the direction that looks like it worked.
_ASKS_FOR = re.compile(r"\b(dates?|quantit(?:y|ies))\b", re.IGNORECASE)


def is_specific_follow_up(body: str) -> bool:
    """A challenge counts when it asks a question and names what it wants.

    §4.8: a question mark, plus either "date" or "quantity".
    """
    return "?" in body and _ASKS_FOR.search(body) is not None


def persona_of(supplier_id: str) -> str:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT persona FROM suppliers WHERE supplier_id = ? LIMIT 1",
            (supplier_id,)).fetchone()
    return row["persona"] if row else "honest"


def reply_body(persona: str, reply_index: int, follow_up: str,
               has_claimed_dispatch: bool = False) -> str:
    """The persona's next line.

    reply_index counts this supplier's *own prior persona replies* — not every
    message bearing its address. A chaos event queues messages from a supplier
    too, and counting those made the first solicited reply look like a second
    one, so SUP-21 opened by revising down a dispatch claim it had not yet
    made. That is incoherent on stage and invisible outside a full run.

    The contradictory persona keys off what it has actually said, which is
    sturdier than any counter: it revises downward only when challenged *and*
    it has a dispatch claim on the record to revise.
    """
    if persona == "contradictory":
        # Claims dispatch, then revises downward when challenged — but never
        # retracts the claim itself. Tracking is what contradicts it.
        if has_claimed_dispatch and is_specific_follow_up(follow_up):
            return REVISED_DOWN
        return DISPATCH_CLAIM
    if persona == "vague":
        if reply_index > 1 and is_specific_follow_up(follow_up):
            return VAGUE_SPECIFIC
        return VAGUE_FIRST
    return HONEST_REPLY.format(quantity=400)


def queue_reply(supplier_id: str, follow_up_body: str) -> str | None:
    """Queue this supplier's reply and advance the clock one tick.

    The reply is stored with visible_at at the new time, so it is invisible to
    the /inbox read inside the same call and appears on the next one.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT persona FROM suppliers WHERE supplier_id = ? LIMIT 1",
            (supplier_id,)).fetchone()
        if row is None:
            return None
        persona = row["persona"]
        sender = f"{supplier_id.lower().replace('-', '')}@example.com"
        prior = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE sender = ? AND persona_reply = 1",
            (sender,)).fetchone()[0]
        claimed_dispatch = conn.execute(
            "SELECT 1 FROM messages WHERE sender = ?"
            " AND LOWER(body) LIKE '%dispatched%' LIMIT 1",
            (sender,)).fetchone() is not None
        po = conn.execute(
            "SELECT po_id FROM purchase_orders WHERE supplier_id = ? "
            "ORDER BY po_id LIMIT 1", (supplier_id,)).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    visible_at = db.advance_clock(TICK)
    message_id = f"MSG-{total + 1:04d}"
    body = reply_body(persona, prior + 1, follow_up_body,
                      has_claimed_dispatch=claimed_dispatch)

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO messages (message_id, sender, recipient, subject, body,"
            " related_po_id, ts, visible_at, persona_reply)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (message_id, sender, "ops@example.com",
             f"Re: {po['po_id'] if po else 'enquiry'} / {supplier_id}", body,
             po["po_id"] if po else None,
             visible_at.isoformat(), visible_at.isoformat()))
    return message_id
