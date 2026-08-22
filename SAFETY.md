# Safety and scope

This system is fully simulated, per problem statement §18.

- Supplier messages go to POST /suppliers/{id}/message on localhost,
  answered by sandbox/supplier_sim.py. No SMTP, no IMAP, no mail library.
- ERP writes go to POST /erp/update, backed by a local SQLite file.
  No ERP SDK, no external API client.
- The inbox is a SQLite table. No mail provider credential exists.
- Costs are numbers in a solver and a database. No payment library.
- No external data feed is used. Not even input-side.

The only outbound network call in this entire codebase is to the LLM
provider. Verify with:

    grep -rE "smtplib|imaplib|requests\.|httpx\.|stripe|boto3" sandbox/ solver/ guardrails/

Run that grep before submission. It returning nothing is the answer if a
judge asks about the safety boundary.
