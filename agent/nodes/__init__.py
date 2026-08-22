"""The six nodes of §4.1. Six, not nine — every node exists because it buys a
scoring outcome, and two of them contain no LLM call at all.

HOLLOW PASS: every node here writes its expected AgentState keys and returns.
No node has real logic yet. Nodes 1, 2 and 3 land at hour 6; the ledger at
hour 9; nodes 4 and 5 at hour 10.5; node 6 and the assumption register at 14.
"""

from agent.nodes.monitor import monitor
from agent.nodes.impact import impact
from agent.nodes.investigate import investigate
from agent.nodes.plan import plan
from agent.nodes.gate import gate
from agent.nodes.execute import execute

__all__ = ["monitor", "impact", "investigate", "plan", "gate", "execute"]
