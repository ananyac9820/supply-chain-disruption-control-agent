"""Track B exceptions."""

from __future__ import annotations


class ToolBudgetExhausted(Exception):
    """G10. Raised when a metered call is attempted with no budget left.

    This fails closed to escalation and is never caught into a retry loop: the
    graph routes to node 5, which escalates with the best plan available,
    flagged INCOMPLETE_INVESTIGATION.
    """

    def __init__(self, tool: str, necessity: str) -> None:
        super().__init__(
            f"tool budget exhausted; refused {tool} (necessity: {necessity})")
        self.tool = tool
        self.necessity = necessity


class MissingNecessity(ValueError):
    """Every tool call must say why it is being made. No default, no empty
    string. If the planner cannot say why, it should not call the tool."""
