"""SQLite schema and seed loader for the simulated ERP sandbox.

One file, one process, no service to stand up (§8.1). Standard-library
sqlite3 only — no ORM, and per §2.6 no network library appears anywhere in
this package.

Tables marked ADD are our additions beyond PS §5; every one of them is
justified in master plan §8.3 and stays sandbox-internal.
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

SEED_DIR = Path(__file__).parent / "seed"
DB_PATH = Path(os.environ.get("SCDA_DB", Path(__file__).parent / "scda.db"))

# The simulated clock's origin. Every seeded timestamp is relative to this,
# so the fixture deadlines (PROD-914 day 2, PROD-882 day 4) hold no matter
# when the sandbox is started. Matches contracts/stub_sandbox.NOW.
SIM_EPOCH = datetime(2026, 9, 2, 10, 0, 0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS components (            -- PS §5.1
    component_id            TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    current_stock           INTEGER NOT NULL,
    usable_stock            INTEGER NOT NULL,      -- always reason from this
    daily_usage             INTEGER NOT NULL,
    safety_stock            INTEGER NOT NULL,
    warehouse               TEXT NOT NULL,
    last_updated            TEXT NOT NULL,
    required_certifications TEXT NOT NULL DEFAULT '[]',   -- ADD, JSON array
    min_quality             REAL NOT NULL DEFAULT 0.0     -- ADD
);

CREATE TABLE IF NOT EXISTS suppliers (             -- PS §5.3
    supplier_id        TEXT NOT NULL,
    component_id       TEXT NOT NULL REFERENCES components(component_id),
    supplier_name      TEXT NOT NULL,
    unit_price         REAL NOT NULL,
    lead_time_days     INTEGER NOT NULL,
    available_quantity INTEGER NOT NULL,
    quality_score      REAL NOT NULL,
    reliability_score  REAL NOT NULL,
    min_order_quantity INTEGER NOT NULL,
    certifications     TEXT NOT NULL DEFAULT '[]',        -- JSON array
    persona            TEXT NOT NULL DEFAULT 'honest',    -- ADD §4.8
    PRIMARY KEY (supplier_id, component_id)
);

CREATE TABLE IF NOT EXISTS purchase_orders (       -- PS §5.2
    po_id                   TEXT PRIMARY KEY,
    component_id            TEXT NOT NULL REFERENCES components(component_id),
    supplier_id             TEXT NOT NULL,
    quantity                INTEGER NOT NULL,
    expected_delivery       TEXT NOT NULL,
    status                  TEXT NOT NULL
        CHECK (status IN ('in_transit','delayed','delivered','cancelled')),
    unit_price              REAL NOT NULL,
    total_value             REAL NOT NULL,
    approval_required_above REAL NOT NULL DEFAULT 150000
);

CREATE TABLE IF NOT EXISTS production_orders (     -- PS §5.4
    production_order_id       TEXT PRIMARY KEY,
    product                   TEXT NOT NULL,
    required_component        TEXT NOT NULL REFERENCES components(component_id),
    units_planned             INTEGER NOT NULL,
    component_required_per_unit INTEGER NOT NULL DEFAULT 1,
    deadline                  TEXT NOT NULL,
    priority                  TEXT NOT NULL
        CHECK (priority IN ('high','medium','low')),
    max_delay_days            INTEGER NOT NULL DEFAULT 0  -- ADD -> solver r[p]
);

CREATE TABLE IF NOT EXISTS messages (              -- PS §5.5 / §5.6
    message_id    TEXT PRIMARY KEY,
    sender        TEXT NOT NULL,
    recipient     TEXT NOT NULL,
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    related_po_id TEXT,
    ts            TEXT NOT NULL,
    -- ADD: a queued persona reply is invisible to /inbox until its tick.
    visible_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracking (              -- PS §5.10, ground truth
    po_id           TEXT PRIMARY KEY REFERENCES purchase_orders(po_id),
    supplier_claim  TEXT NOT NULL,
    tracking_status TEXT NOT NULL,
    last_movement   TEXT
);

CREATE TABLE IF NOT EXISTS quotes (                -- PS §5.7, issued by /rfq
    quote_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id        TEXT NOT NULL,
    component_id       TEXT NOT NULL,
    quantity_available INTEGER NOT NULL,
    unit_price         REAL NOT NULL,
    delivery_days      INTEGER NOT NULL,
    expedite_available INTEGER NOT NULL DEFAULT 1,
    expedite_fee       REAL NOT NULL DEFAULT 0,
    quote_valid_hours  INTEGER NOT NULL DEFAULT 6,  -- G8 expiry
    issued_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS erp_log (               -- PS §5.9 write audit
    record_id TEXT PRIMARY KEY,
    action    TEXT NOT NULL,
    payload   TEXT NOT NULL,                        -- JSON
    ts        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_trust (        -- ADD §4.5, trust.py owns
    supplier_id        TEXT PRIMARY KEY,
    on_time_count      INTEGER NOT NULL DEFAULT 0,
    late_count         INTEGER NOT NULL DEFAULT 0,
    contradicted_claims INTEGER NOT NULL DEFAULT 0,
    moq_failures       INTEGER NOT NULL DEFAULT 0,
    quality_delta      REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS sim_clock (             -- ADD §4.6 GET /sim/clock
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    now TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_flags (            -- ADD, chaos side-effects
    key   TEXT PRIMARY KEY,                         -- e.g. 'expedite_withdrawn'
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chaos_log (             -- ADD §4.8 POST /sim/inject
    disruption_id TEXT PRIMARY KEY,
    event         TEXT NOT NULL,
    params        TEXT NOT NULL,                    -- JSON
    ts            TEXT NOT NULL
);
"""

TABLES = ("components", "suppliers", "purchase_orders", "production_orders",
          "messages", "tracking", "quotes", "erp_log", "supplier_trust",
          "sim_clock", "sim_flags", "chaos_log")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset: bool = False) -> None:
    """Create the schema, and seed it when the tables are empty.

    reset=True drops everything first — this is what POST /sim/reset uses to
    put the world back after a chaos run.
    """
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    with connect() as conn:
        conn.executescript(SCHEMA)
        empty = conn.execute("SELECT COUNT(*) FROM components").fetchone()[0] == 0
    if empty:
        seed()


def _rows(name: str) -> list[dict]:
    path = SEED_DIR / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else []


def seed() -> None:
    """Load the hand-authored catalog. Never sampled, never generated (§4.7)."""
    with connect() as conn:
        for table in ("components", "suppliers", "purchase_orders",
                      "production_orders", "messages", "tracking"):
            rows = _rows(table)
            if not rows:
                continue
            cols = list(rows[0])
            placeholders = ", ".join("?" for _ in cols)
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                [tuple(_encode(r[c]) for c in cols) for r in rows],
            )
        for row in _rows("suppliers"):
            conn.execute(
                "INSERT OR IGNORE INTO supplier_trust (supplier_id) VALUES (?)",
                (row["supplier_id"],))
        conn.execute("INSERT OR REPLACE INTO sim_clock (id, now) VALUES (1, ?)",
                     (SIM_EPOCH.isoformat(),))


def _encode(value):
    """JSON-encode list columns; sqlite3 has no array type."""
    return json.dumps(value) if isinstance(value, (list, dict)) else value


def sim_now() -> datetime:
    with connect() as conn:
        row = conn.execute("SELECT now FROM sim_clock WHERE id = 1").fetchone()
    return datetime.fromisoformat(row["now"]) if row else SIM_EPOCH


def advance_clock(delta) -> datetime:
    """Move the simulated clock forward and return the new time.

    Sending a supplier a message advances it by one tick, which is what makes
    "the reply arrives on the next tick" mean something without a scheduler.
    """
    now = sim_now() + delta
    with connect() as conn:
        conn.execute("UPDATE sim_clock SET now = ? WHERE id = 1", (now.isoformat(),))
    return now
