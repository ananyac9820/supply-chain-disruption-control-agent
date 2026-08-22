"""The CLI streaming trace - Track B 4.7.

The cheapest surface and the least likely to break live. It renders an audit
JSONL as a live terminal trace: every tool call with its necessity, every
verification verdict, every guardrail firing, every replan trigger, the solver
result and the final decision.

    python -m output.cli audit_logs/fixture-DIS-001.jsonl
    python -m output.cli --fixture              generate the fixture, then render it
    python -m output.cli <path> --follow        tail a run as the agent writes it
    python -m output.cli <path> --speed 0       no pacing, for tests and CI

This module imports nothing from agent/. It works standalone against a file,
which is what makes it useful from hour 3.5 and what makes it the safety net:
if the dashboard dies at hour 20 the demo runs from here and loses almost
nothing.

Colour matters more than it sounds with a judge standing behind you -
CONTRADICTED in red and a guardrail firing in amber are legible from across a
room.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Iterator

from rich.console import Console
from rich.text import Text

from output.audit import iter_jsonl

# Verdict and status words that get colour wherever they appear in a summary.
VERDICT_STYLES = {
    "CONTRADICTED": "bold red",
    "UNVERIFIABLE": "yellow",
    "VAGUE": "yellow",
    "GROUNDED": "green",
    "SPECIFIC": "green",
    "INFEASIBLE": "bold red",
    "OPTIMAL": "bold green",
    "FEASIBLE": "green",
}

# type -> (label, label style). The label column is what makes the trace
# scannable; keep it to six characters so the continuation lines line up.
TYPE_LABELS = {
    "disruption_detected": ("", "bold white"),
    "tool_call": ("TOOL", "cyan"),
    "calculation": ("", "white"),
    "verification": ("VERIFY", "bold"),
    "decision": ("SOLVE", "bold blue"),
    "plan_proposed": ("PLAN", "blue"),
    "guardrail": ("GUARD", "bold yellow"),
    "escalation": ("PAUSE", "bold magenta"),
    "erp_update": ("ERP", "green"),
    "replan": ("REPLAN", "bold yellow"),
    "assumption_break": ("BROKEN", "bold red"),
    "run_complete": ("DONE", "bold green"),
}

CONTINUATION = " " * 10          # width of "[hh:mm:ss] "


class TraceRenderer:
    def __init__(self, console: Console | None = None, speed: float = 0.06) -> None:
        self.console = console or Console(highlight=False, soft_wrap=True)
        self.speed = speed

    # ---- one event ---------------------------------------------------

    def render(self, event: dict) -> None:
        clock = str(event.get("ts", ""))[11:19] or "--:--:--"
        etype = event.get("type", "")
        label, style = TYPE_LABELS.get(etype, (etype[:6].upper(), "white"))
        detail = event.get("detail") or {}

        line = Text()
        line.append(f"[{clock}] ", style="dim")

        if etype == "disruption_detected":
            line.append(f"{event.get('disruption_id', '')}  ", style="bold white")
            line.append("disruption detected - ", style="bold white")
            line.append_text(self._styled(event.get("summary", "")))
        elif etype == "tool_call":
            line.append(f"{label:<6}", style=style)
            line.append(" -> " if not detail.get("served_from_cache") else "    ")
            body = self._styled(event.get("summary", ""))
            if detail.get("served_from_cache"):
                body.stylize("dim")          # a cache hit should recede, not shout
            line.append_text(body)
        else:
            line.append(f"{label:<6}", style=style)
            line.append("   ")
            line.append_text(self._styled(event.get("summary", "")))

        self._emit(line)

        try:
            extras = list(self._continuations(event, detail))
        except Exception as exc:      # noqa: BLE001 - never break the trace
            extras = [Text(f"(detail unrenderable: {type(exc).__name__})", style="dim red")]
        for extra in extras:
            self._emit(Text(CONTINUATION).append_text(extra))

    def _continuations(self, event: dict, detail: dict) -> Iterator[Text]:
        etype = event.get("type", "")

        if etype == "tool_call":
            why = event.get("necessity")
            budget = ""
            if "budget_used" in detail:
                budget = f"   [{detail['budget_used']}/{detail['budget_total']}]"
            if why:
                t = Text()
                t.append("why: ", style="dim")
                t.append(why, style="dim italic")
                t.append(budget, style="dim")
                yield t

        if etype == "calculation":
            for key in ("coverage_days", "gap", "free_of_safety_stock"):
                if key in detail:
                    yield Text(f"{key.replace('_', ' ')}: {detail[key]}", style="dim")
                    break
            rows = detail.get("cumulative_requirement")
            for row in rows if isinstance(rows, list) else []:
                yield Text(
                    f"  {row['production_order_id']} day {row['deadline_day']}: "
                    f"{row['units']} units, cumulative {row['cumulative']}, "
                    f"short {row.get('cumulative_shortfall', row.get('shortfall'))}",
                    style="dim")
            base = detail.get("baseline")
            if base:
                yield Text(
                    f"{base['units_short']} units short - "
                    f"{base['production_days_lost']} production-days lost - "
                    f"cost of inaction {base['cost_of_inaction']:,.0f}", style="dim")

        if etype == "verification":
            before, after = detail.get("trust_before"), detail.get("trust_after")
            if before is not None and after is not None:
                t = Text()
                t.append(f"trust {detail.get('supplier_id', '')}".rstrip() + "  ", style="dim")
                t.append(f"{before} -> {after}", style="bold red")
                t.append("   (feeds the next solve)", style="dim")
                yield t

        if etype == "decision":
            for a in detail.get("allocations", []):
                yield Text(f"  {a['units']} x {a['supplier_id']} "
                           f"({a['arrival_day']}d)  {a['cost']:,.0f}", style="dim")
            for r in detail.get("reschedules", []):
                yield Text(f"  {r['production_order_id']} delayed {r['delay_days']}d",
                           style="yellow")
            if detail.get("binding_constraint"):
                yield Text(f"binding: {detail['binding_constraint']}", style="red")
            bd = event.get("baseline_delta")
            if bd and "net_avoided" in bd:
                yield Text(f"avoids {bd['net_avoided']:,.0f} of the "
                           f"{bd['cost_of_inaction']:,.0f} cost of inaction", style="green")

        for alt in event.get("alternatives_rejected", []):
            t = Text()
            t.append("rejected ", style="dim")
            t.append(alt.get("supplier_id", ""), style="dim bold")
            t.append(f": {alt.get('reason', '')}", style="dim")
            yield t

        if event.get("remaining_risk"):
            t = Text()
            t.append("risk: ", style="dim")
            t.append(event["remaining_risk"], style="dim yellow")
            yield t

    def _styled(self, summary: str) -> Text:
        """Colour any verdict or solver-status word wherever it appears.

        Word boundaries, not substrings: FEASIBLE occurs inside INFEASIBLE, and
        highlighting the substring would paint half of an infeasible solve
        green. That is the one colour on screen a judge reads fastest, so it
        has to be the right one."""
        text = Text(summary)
        for word, style in VERDICT_STYLES.items():
            text.highlight_regex(rf"\b{word}\b", style)
        return text

    def _emit(self, line: Text) -> None:
        self.console.print(line)
        if self.speed:
            time.sleep(self.speed)

    # ---- whole files -------------------------------------------------

    def render_file(self, path: str | Path) -> int:
        n = 0
        for event in iter_jsonl(path):
            self.render(event)
            n += 1
        return n

    def follow(self, path: str | Path, poll: float = 0.25,
               idle_timeout: float = 30.0) -> int:
        """Tail a file the agent is still writing. Stops on run_complete, or
        after idle_timeout seconds with no new line."""
        path = Path(path)
        n, offset, last = 0, 0, time.monotonic()
        while True:
            if path.exists():
                with path.open("r", encoding="utf-8") as fh:
                    fh.seek(offset)
                    for line in fh:
                        if not line.endswith("\n"):     # half-written record
                            break
                        offset += len(line.encode("utf-8"))
                        if not line.strip():
                            continue
                        import json
                        event = json.loads(line)
                        self.render(event)
                        n, last = n + 1, time.monotonic()
                        if event.get("type") == "run_complete":
                            return n
            if time.monotonic() - last > idle_timeout:
                return n
            time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="output.cli",
                                 description="render an audit JSONL as a streaming trace")
    ap.add_argument("path", nargs="?", help="audit JSONL to render")
    ap.add_argument("--fixture", action="store_true",
                    help="generate the 20-event fixture first, then render it")
    ap.add_argument("--follow", action="store_true",
                    help="tail the file as it is written")
    ap.add_argument("--speed", type=float, default=0.06,
                    help="seconds between lines (0 = no pacing)")
    args = ap.parse_args(argv)

    if args.fixture:
        from output.fixtures import generate
        args.path = str(generate(args.path))
    if not args.path:
        ap.error("give a path, or --fixture")

    renderer = TraceRenderer(speed=args.speed)
    if args.follow:
        n = renderer.follow(args.path)
    else:
        if not Path(args.path).exists():
            print(f"no such audit file: {args.path}", file=sys.stderr)
            return 1
        n = renderer.render_file(args.path)

    renderer.console.print(f"\n[dim]{n} events - {args.path}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
