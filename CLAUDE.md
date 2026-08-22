# Repo: supply-chain-disruption-control-agent

## Document precedence — read this first
Three documents may be in context:
1. contracts/ — the frozen interface. Highest authority. Never edit.
2. The Track document for whichever person is working. This is the spec.
3. The Master Build Plan PDF — background and rationale for the WHOLE
   project, including work owned by the other person.

Where the Master Build Plan and a Track document differ, the Track
document wins. Read the master plan for WHY. Never build from it.
It describes another person's folders; do not implement them.

Do not attach the four original research PDFs to any session. Two of them
describe a logistics / port-routing problem with a different rubric. None
of that exists in this project.

## Ownership — enforced, not advisory
Person A owns:  sandbox/  solver/  guardrails/  trust.py  tests/contract/
Person B owns:  agent/  output/
Joint, frozen after hour 1.5:  contracts/

Never create, edit, move or delete a file outside the folders owned by
the person you are currently working with. If something in another
person's folder looks wrong, say so. Do not fix it.

---

# Track A — Person A

## Hard rules
- No LLM calls anywhere in my code. No prompts. No model SDK imports.
- No network libraries at all: no smtplib, imaplib, requests, httpx,
  boto3, or any payment SDK. Local SQLite + FastAPI only. This is a
  hackathon safety requirement (problem statement §18), not a preference.
- Response shapes must match contracts/models.py exactly. Never add,
  rename or drop a field to make something convenient.
- Money is handled as integers (paise) inside the CP-SAT model. Never
  floats in the objective.
- Guardrails G3, G4, G6, G7, G11 are pre-solve filters or model
  constraints. They must be impossible to violate, not checked afterwards.

## Domain
This is a PROCUREMENT and MANUFACTURING simulation: components,
suppliers, purchase orders, RFQs, production orders, certifications,
minimum order quantities, safety stock, approval thresholds.
It is NOT logistics routing. There are no ports, carriers, transport
modes, routes, shipping lanes or SLAs anywhere in this project.
If you find yourself writing about a route or a carrier, stop.

## Definition of done
tests/solver/ (8 checks) and tests/contract/ (6 checks) pass, and
tests/contract/ passes against both HttpSandbox and StubSandbox.

---

# Track B — Person B

PLACEHOLDER. Person B must paste their section here from §8.3 of the
Track B build document, before the hour-1.5 freeze. Do not write it for
them and do not guess at its hard rules — the Track B document is the
authority for this section.

Until it is filled in, the ownership block above still applies: a session
working with Person B writes only agent/ and output/.
