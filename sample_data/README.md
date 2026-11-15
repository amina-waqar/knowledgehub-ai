# Sample data

- `incident_checkout_outage.jsonl` — synthetic multi-service logs (JSON lines).
  A database connection-pool exhaustion starting around 2026-01-15T14:20:00Z
  causes payments-svc timeouts and checkout-api 503s, with unrelated noise from
  search-svc and email-worker. To try it: create an incident with title
  "Checkout failing: payment timeouts", service `checkout-api`, started at
  `2026-01-15T14:20:00Z`, then upload this file to the incident.
- `sample.log`, `database_runbook.txt` — the original plain-text samples.
