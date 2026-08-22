"""Track B — the output layer.

Build order is deliberate and is the order these modules appeared:
    1. audit.py   JSONL writer          — machine-gradable, PS §4.10
    2. cli.py     streaming trace       — the demo safety net, cannot break
    3. brief.py   decision brief        — PS §17 shape, the leave-behind
    4. dashboard/ rubric scoreboard     — cuttable without regret

Everything here works standalone against a fixture. Nothing in this package
imports agent/, so a renderer is never blocked on the graph.
"""
