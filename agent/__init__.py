"""Track B — the agent runtime.

Owns the LangGraph state machine, the metered tool wrappers, the tool-call
ledger and the assumption register. Never implements solve() or the G1-G12
rules; it calls them.
"""
