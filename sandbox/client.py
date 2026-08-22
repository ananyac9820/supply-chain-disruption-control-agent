"""HttpSandbox — the real client behind the SandboxClient protocol.

Same methods, same shapes, same return types as contracts.stub_sandbox.
StubSandbox. The hour-12 merge is one line:

    SANDBOX = StubSandbox()  ->  SANDBOX = HttpSandbox("http://localhost:8000")

On the transport, and why it is not requests or httpx
-----------------------------------------------------
The §2.6 safety grep bans smtplib, imaplib, requests, httpx, stripe and boto3
from this package, and CLAUDE.md restates it as a hard rule. A client that
speaks to the sandbox over HTTP is nonetheless unavoidable — the merge is
defined as a base-URL swap, and the sandbox is a FastAPI process.

This uses urllib.request from the standard library, with a base URL that
must resolve to loopback. That satisfies both readings of the rule: no
third-party network library enters the dependency tree, the grep stays
clean, and there is no client here capable of reaching a supplier, an ERP,
a mail host or a payment processor. _check_loopback below refuses anything
that is not localhost, so this cannot become an outbound channel by an edit
to a config value.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from contracts.models import (
    ApprovalResult, Component, Message, ProductionOrder, PurchaseOrder, Quote,
    Supplier, TrackingRecord,
)

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


class SandboxError(RuntimeError):
    """Non-2xx from the sandbox. Carries the status so callers can branch."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"sandbox returned {status}: {detail}")
        self.status = status
        self.detail = detail


class HttpSandbox:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        _check_loopback(self.base_url)

    # ---- reads ---------------------------------------------------------

    def get_inventory(self, component_id: str | None = None) -> list[Component]:
        if component_id is None:
            return [Component.model_validate(r) for r in self._get("/inventory")]
        return [Component.model_validate(self._get(f"/inventory/{component_id}"))]

    def get_purchase_orders(self, po_id: str | None = None) -> list[PurchaseOrder]:
        if po_id is None:
            return [PurchaseOrder.model_validate(r)
                    for r in self._get("/purchase-orders")]
        return [PurchaseOrder.model_validate(self._get(f"/purchase-orders/{po_id}"))]

    def get_suppliers(self, component_id: str) -> list[Supplier]:
        return [Supplier.model_validate(r)
                for r in self._get("/suppliers", {"component_id": component_id})]

    def get_production_schedule(self) -> list[ProductionOrder]:
        return [ProductionOrder.model_validate(r)
                for r in self._get("/production-schedule")]

    def get_inbox(self, since: datetime | None = None) -> list[Message]:
        params = {"since": since.isoformat()} if since is not None else None
        return [Message.model_validate(r) for r in self._get("/inbox", params)]

    def get_tracking(self, po_id: str) -> TrackingRecord:
        return TrackingRecord.model_validate(self._get(f"/tracking/{po_id}"))

    # ---- writes --------------------------------------------------------

    def send_message(self, supplier_id: str, subject: str, body: str) -> Message:
        return Message.model_validate(self._post(
            f"/suppliers/{supplier_id}/message", {"subject": subject, "body": body}))

    def request_rfq(self, component_id: str, quantity: int, needed_by_days: int,
                    supplier_ids: list[str]) -> list[Quote]:
        return [Quote.model_validate(r) for r in self._post("/rfq", {
            "component_id": component_id, "quantity": quantity,
            "needed_by_days": needed_by_days, "supplier_ids": supplier_ids})]

    def check_approval(self, action: str, estimated_cost: float) -> ApprovalResult:
        return ApprovalResult.model_validate(self._post(
            "/approval/check", {"action": action, "estimated_cost": estimated_cost}))

    def erp_update(self, action: str, payload: dict) -> dict:
        try:
            return self._post("/erp/update", {"action": action, "payload": payload})
        except SandboxError as exc:
            if exc.status == 400:
                # StubSandbox returns a rejection body rather than raising;
                # the two must behave identically for the same call.
                return {"status": "rejected", "message": exc.detail, "record_id": None}
            raise

    # ---- simulation control (not part of the protocol) ------------------

    def sim_clock(self) -> dict:
        return self._get("/sim/clock")

    def sim_reset(self) -> dict:
        return self._post("/sim/reset", {})

    # ---- transport -----------------------------------------------------

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._send(urllib.request.Request(url, method="GET"))

    def _post(self, path: str, body: dict) -> Any:
        request = urllib.request.Request(
            self.base_url + path, method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        return self._send(request)

    def _send(self, request: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise SandboxError(exc.code, _detail(exc.read())) from exc


def _detail(body: bytes) -> str:
    try:
        return json.loads(body).get("detail", body.decode())
    except (ValueError, UnicodeDecodeError):
        return body.decode(errors="replace")


def _check_loopback(base_url: str) -> None:
    host = urllib.parse.urlparse(base_url).hostname or ""
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"HttpSandbox refuses a non-loopback base URL: {base_url!r}. "
            "The sandbox is fully simulated (PS §18); there is nothing real "
            "to talk to.")
