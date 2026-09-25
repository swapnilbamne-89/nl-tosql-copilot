# NL-to-SQL Copilot

A schema-aware natural language interface to a SQL database, built to demonstrate
product judgment around AI safety/guardrails as much as the AI integration itself.

## The problem

Non-technical stakeholders (support, sales, ops) constantly need one-off answers
from the database ("how many orders shipped to Germany last month?") and either
wait on a data analyst or don't ask at all. An LLM can translate the question to
SQL — the product risk is entirely in what happens *after* the SQL is generated.

## How it works

1. Introspect the SQLite schema at startup (`PRAGMA table_info`) — the prompt is
   schema-aware generically, not hardcoded to this one demo database.
2. Send the question + schema to Claude, asking for exactly one `SELECT`
   statement back, nothing else.
3. Execute it against a **read-only SQLite connection** (`mode=ro`) and print
   the rows, capped at 200.

## Guardrails (the actual point of this project)

- **Read-only DB connection is the real defense**, not the parser check below.
  Even if the model is tricked into emitting `DROP TABLE`, the OS-level
  read-only file handle rejects the write. This is a native SQLite feature,
  not custom code.
- A fast-fail layer runs *before* execution: the SQL is parsed with
  [`sqlglot`](https://github.com/tobymao/sqlglot) and only accepted if it's
  exactly one statement whose root is a genuine read (`SELECT`/`UNION`).
  This is an **allowlist**, not a keyword denylist — a denylist has to
  enumerate every dangerous keyword and missed real bypasses (see
  `SCENARIOS.md`); an allowlist rejects anything that isn't a known-safe
  read by default. Still defense in depth, not the primary control.
- Results are capped at 200 rows to avoid a runaway `SELECT *` dumping the
  whole table into a chat window.
- A `SELECT` with no `LIMIT` is still valid even if it's expensive to
  *compute* — a SQLite progress-handler timeout aborts any query that runs
  past a 5-second budget, so an expensive cross join or recursive CTE can't
  hang the app indefinitely.
- The model is instructed to return `NO_QUERY` rather than guess when a
  question can't be mapped to the schema, instead of hallucinating a plausible
  but wrong query.

## What this doesn't handle (explicitly out of scope for a portfolio piece)

- Multi-tenant row-level security (would need per-user SQL views, not app-level filtering)
- Real query cost estimation — the timeout above is a blunt wall-clock guard,
  not `EXPLAIN QUERY PLAN`-based cost analysis or a per-user quota
- Ambiguous question clarification (the model gets one shot, no follow-up turn)
- Auth — this is a local CLI, not a hosted service
- Rate limiting on the Claude API calls themselves

See `SCENARIOS.md` for the test matrix that exercises these boundaries, and
`INTERVIEW_QA.md` for the reasoning behind each tradeoff above.

## Running it

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your-key-here   # or copy .env.example to .env
python app.py
```

First run seeds `shop.db` (customers/products/orders/order_items, ~90 rows)
automatically. Ask things like:

- "How many orders are pending?"
- "Top 5 customers by total order value"
- "Which country has the most customers?"
- "Delete all the orders" — should be blocked (not a SELECT)
