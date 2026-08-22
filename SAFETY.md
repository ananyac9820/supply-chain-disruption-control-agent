# Safety and scope

This system is fully simulated, per problem statement §18.

- Supplier messages go to POST /suppliers/{id}/message on localhost,
  answered by sandbox/supplier_sim.py. No SMTP, no IMAP, no mail library.
- ERP writes go to POST /erp/update, backed by a local SQLite file.
  No ERP SDK, no external API client.
- The inbox is a SQLite table. No mail provider credential exists.
- Costs are numbers in a solver and a database. No payment library.
- No external data feed is used. Not even input-side.

The trust ledger is four integer counters in the same SQLite file. No
external service scores a supplier.

Network activity in this codebase, exhaustively:

1. The LLM provider, from Track B's agent. The only call that leaves the
   machine.
2. `sandbox/client.py` to `http://localhost:<port>` — the agent talking to
   the simulated sandbox. It uses `urllib.request` from the standard library,
   and `_check_loopback` refuses any base URL whose host is not localhost.
3. The sandbox itself listens on localhost. The demo harnesses in `demo/`
   start it in-process and bind an ephemeral port on 127.0.0.1 to do so.

There is no client in this repo capable of reaching a supplier, an ERP, a
mail host or a payment processor — not by configuration, and not by editing a
base URL. Verify with:

    grep -rE "smtplib|imaplib|requests\.|httpx\.|stripe|boto3" sandbox/ solver/ guardrails/

Run that grep before submission. It returning nothing is the answer if a
judge asks about the safety boundary.
