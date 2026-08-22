"""Attributing a delivery discrepancy to a cause, from evidence.

This lives in sandbox/ because sandbox/ owns tracking ground truth, and
attribution is a reading of that evidence rather than a judgement about a
supplier's character.

The distinction that matters
----------------------------
A dispatch claim standing against `label_created_no_pickup` is genuinely
ambiguous. It is consistent with a supplier that printed a label and never
handed the goods over, and equally consistent with a supplier that packed and
tendered on time while the courier failed to collect. Nothing in the tracking
record separates those two worlds.

So it is UNATTRIBUTED, and an UNATTRIBUTED incident must not move anyone's
reputation. The shipment is still unverifiable — that is what
shipment_confidence is for — but "we cannot confirm these units" and "this
supplier is unreliable" are different claims, and only the first is supported
by the evidence.

SUPPLIER is reserved for evidence that actually establishes it: nothing was
ever tendered *and* the supplier's own record shows no pack event. COURIER
for a shipment that was picked up and then stalled.

FACTORY and EXTERNAL are in the vocabulary because incidents can arrive from
outside tracking. No tracking record can establish either on its own, so
nothing here returns them; a caller with other evidence supplies them
directly.
"""

from datetime import datetime, timedelta

ATTRIBUTIONS = ("SUPPLIER", "COURIER", "FACTORY", "EXTERNAL", "UNATTRIBUTED")

# A shipment scanned as collected and then silent for this long is stalled in
# the carrier's network, not sitting on the supplier's dock.
COURIER_STALL_HOURS = 48

# Tracking states meaning the carrier never took possession.
NOT_TENDERED = ("label_created_no_pickup", "not_found", "awaiting_tender")
# Tracking states meaning the carrier did take possession.
IN_CARRIER_HANDS = ("picked_up", "in_transit", "at_facility", "out_for_delivery")


def has_discrepancy(row) -> bool:
    """True when the supplier's claim is not supported by tracking."""
    if row["supplier_claim"] != "dispatched":
        return False
    return row["tracking_status"] not in IN_CARRIER_HANDS + ("delivered",)


def classify(row, now: datetime) -> tuple[str, str, str, str]:
    """(attribution, basis, observed, expected) for one tracking row.

    Only called when has_discrepancy() is true.
    """
    status = row["tracking_status"]
    packed_at = _parse(row["packed_at"] if "packed_at" in row.keys() else None)
    tendered_at = _parse(row["tendered_at"] if "tendered_at" in row.keys() else None)
    last_movement = _parse(row["last_movement"])

    expected = "goods with the carrier and moving"
    observed = f"tracking_status {status!r}"
    if last_movement is None:
        observed += ", no movement recorded"

    # Picked up, then nothing. Possession is established; the stall is not
    # the supplier's to answer for.
    if tendered_at is not None or status in IN_CARRIER_HANDS:
        stalled_for = now - (last_movement or tendered_at or now)
        if stalled_for >= timedelta(hours=COURIER_STALL_HOURS):
            return ("COURIER",
                    f"handover scanned, then no movement for "
                    f"{int(stalled_for.total_seconds() // 3600)}h",
                    observed, expected)
        return ("UNATTRIBUTED",
                "handover scanned and movement is recent; too early to "
                "attribute a delay to anyone",
                observed, expected)

    # Nothing ever reached the carrier. Only the supplier's own pack record
    # can say whether the goods existed to be collected.
    if status in NOT_TENDERED:
        if packed_at is None and status != "label_created_no_pickup":
            return ("SUPPLIER",
                    "no consignment was ever tendered and the supplier's own "
                    "record shows no pack event",
                    observed, expected)
        return ("UNATTRIBUTED",
                "a label exists but no pickup was scanned; consistent both "
                "with goods never tendered and with a collection that was "
                "never made",
                observed, expected)

    return ("UNATTRIBUTED",
            f"tracking state {status!r} does not establish a cause",
            observed, expected)


def _parse(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
