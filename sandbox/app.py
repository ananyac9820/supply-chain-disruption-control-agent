"""The simulated ERP sandbox — FastAPI over one SQLite file.

Fifteen endpoints (§4.6), localhost only, no auth, no async workers, no
message queue. Response shapes come straight from contracts/models.py and are
never adjusted for convenience.

PS §18: nothing here reaches outside the process. No mail library, no ERP
SDK, no payment library, no HTTP client. Prove it with the §2.6 grep.

STATUS: schema, seed and the inventory endpoint only. The remaining
fourteen endpoints are deliberately not written yet — see README in this
package.
"""

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from contracts.models import Component
from sandbox import db

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="SCDA simulated ERP sandbox",
    description="Fully simulated procurement environment. PS §18: no real "
                "suppliers, no real ERP, no real email, no real payments.",
    version="0.1.0",
)



def _component(row) -> Component:
    """One row -> one contract model. JSON columns decoded here, not in the route."""
    return Component(
        component_id=row["component_id"],
        name=row["name"],
        current_stock=row["current_stock"],
        usable_stock=row["usable_stock"],
        daily_usage=row["daily_usage"],
        safety_stock=row["safety_stock"],
        warehouse=row["warehouse"],
        last_updated=row["last_updated"],
        required_certifications=json.loads(row["required_certifications"]),
        min_quality=row["min_quality"],
    )


@app.get("/inventory", response_model=list[Component])
def get_inventory() -> list[Component]:
    """T-01. Every component. usable_stock is the field that matters."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM components ORDER BY component_id").fetchall()
    return [_component(r) for r in rows]


@app.get("/inventory/{component_id}", response_model=Component)
def get_component(component_id: str) -> Component:
    """T-01 by id. 404 rather than an empty list, so a typo is loud."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM components WHERE component_id = ?",
            (component_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"unknown component: {component_id}")
    return _component(row)
