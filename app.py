"""NL-to-SQL Copilot: ask questions in plain English, get answers from a real database.

Architecture: introspect the SQLite schema -> ask Claude for a single SELECT
statement -> execute it on a READ-ONLY connection -> print the rows.
The read-only connection (not the AST check) is what actually stops the
LLM from running anything destructive; the parser check below is just a
fast, cheap second opinion before the query is even attempted.
"""

import os
import random
import re
import sqlite3
import sys
import time

import sqlglot
from sqlglot import exp

from anthropic import Anthropic

DB_PATH = os.path.join(os.path.dirname(__file__), "shop.db")
MODEL = "claude-sonnet-5"
ROW_LIMIT = 200
QUERY_TIMEOUT_SECONDS = 5
PROGRESS_HANDLER_STEP_INTERVAL = 1000  # how often SQLite checks the clock

# Allowlist, not a denylist: a new write-capable SQLite keyword added in some
# future version is rejected by default here, whereas a keyword denylist
# would need updating to catch it. Union covers `SELECT ... UNION SELECT ...`;
# CTEs (`WITH x AS (...) SELECT ...`) parse as a Select node with `with`
# populated, so they're covered by Select without a separate case -- and a
# WITH-prefixed write (SQLite 3.35+ allows CTEs before DELETE/UPDATE/INSERT)
# still parses with a Delete/Update/Insert root, so it's still rejected.
ALLOWED_SQL_ROOT_TYPES = (exp.Select, exp.Union)


def init_db(db_path: str = DB_PATH) -> None:
    """Create the demo shop database with seed data if it doesn't exist yet."""
    if os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            country TEXT NOT NULL,
            signup_date TEXT NOT NULL
        );

        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL
        );

        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            order_date TEXT NOT NULL,
            status TEXT NOT NULL
        );

        CREATE TABLE order_items (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL
        );
        """
    )

    rng = random.Random(42)

    countries = ["USA", "UK", "India", "Germany", "Canada", "Australia"]
    customers = [
        (i, f"Customer {i}", f"customer{i}@example.com", rng.choice(countries),
         f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}")
        for i in range(1, 13)
    ]
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customers)

    categories = ["Electronics", "Books", "Home", "Sports"]
    products = [
        (i, f"Product {i}", rng.choice(categories), round(rng.uniform(5, 500), 2))
        for i in range(1, 21)
    ]
    conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", products)

    statuses = ["completed", "pending", "cancelled"]
    orders = [
        (i, rng.randint(1, 12), f"2026-{rng.randint(1, 9):02d}-{rng.randint(1, 28):02d}",
         rng.choice(statuses))
        for i in range(1, 31)
    ]
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", orders)

    item_id = 1
    order_items = []
    for order_id, *_ in orders:
        for _ in range(rng.randint(1, 4)):
            product_id = rng.randint(1, 20)
            price = next(p[3] for p in products if p[0] == product_id)
            order_items.append((item_id, order_id, product_id, rng.randint(1, 5), price))
            item_id += 1
    conn.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", order_items)

    conn.commit()
    conn.close()


def get_schema_description(db_path: str = DB_PATH) -> str:
    """Introspect the schema so the prompt (and the app) stay generic, not hardcoded."""
    conn = sqlite3.connect(db_path)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    lines = []
    for table in tables:
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        col_desc = ", ".join(f"{col[1]} {col[2]}" for col in columns)
        lines.append(f"{table}({col_desc})")
    conn.close()
    return "\n".join(lines)


def is_select_only(sql: str) -> bool:
    """Defense-in-depth check. The read-only connection is the real guardrail.

    Parses the SQL with sqlglot and accepts it only if it's exactly one
    statement whose root AST node is a genuine read (see
    ALLOWED_SQL_ROOT_TYPES). This replaced an earlier regex/keyword denylist:
    a denylist has to enumerate every dangerous keyword up front and missed
    `pragma_table_info(...)` used as a table-valued function inside a SELECT
    (harmless -- see SCENARIOS.md -- but the miss itself showed the approach's
    limits). Parsing the actual grammar instead of pattern-matching text also
    means a harmless trailing SQL comment (`SELECT ... -- ; DROP TABLE x`)
    is correctly recognized as one inert statement instead of being rejected
    just because a `;` character appears somewhere in the string.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except sqlglot.errors.ParseError:
        return False
    if len(statements) != 1:
        return False  # no stacked statements
    return isinstance(statements[0], ALLOWED_SQL_ROOT_TYPES)


def generate_sql(client: Anthropic, question: str, schema: str) -> str:
    system_prompt = (
        "You are a SQL generator for a SQLite database. Given a schema and a "
        "question in plain English, output ONLY a single valid SQLite SELECT "
        "statement that answers it. No explanation, no markdown fences, no "
        "semicolon-separated statements. If the question cannot be answered "
        "with a SELECT against this schema, output exactly: NO_QUERY\n\n"
        f"Schema:\n{schema}"
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    # strip markdown fences if the model added them anyway
    text = re.sub(r"^```(sql)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE).strip()
    return text


def run_query(sql: str, db_path: str = DB_PATH, timeout_seconds: float = QUERY_TIMEOUT_SECONDS):
    """Execute against a READ-ONLY connection — this, not the regex check, is what
    actually prevents the LLM from mutating the database.

    A SELECT with no LIMIT is still a valid SELECT (fetchmany caps what we
    return, not what SQLite has to compute) — an expensive cross join or a
    recursive CTE can still burn CPU for a long time. The progress handler
    below aborts the query once it's run past its time budget.
    # ponytail: wall-clock/step-count heuristic, not a real cost estimator —
    # upgrade to EXPLAIN QUERY PLAN-based cost checks if abuse becomes real.
    """
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    start = time.monotonic()
    conn.set_progress_handler(
        lambda: 1 if time.monotonic() - start > timeout_seconds else 0,
        PROGRESS_HANDLER_STEP_INTERVAL,
    )
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(ROW_LIMIT)
        return columns, rows
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e).lower():
            raise sqlite3.OperationalError(
                f"query exceeded {timeout_seconds}s budget (aborted)"
            ) from e
        raise
    finally:
        conn.close()


def ask(client: Anthropic, question: str, schema: str) -> None:
    sql = generate_sql(client, question, schema)
    if sql == "NO_QUERY":
        print("Claude couldn't map that question to this schema.")
        return

    print(f"\nGenerated SQL:\n  {sql}\n")

    if not is_select_only(sql):
        print("Blocked: generated SQL failed the SELECT-only guardrail.")
        return

    try:
        columns, rows = run_query(sql)
    except sqlite3.Error as e:
        print(f"Query failed: {e}")
        return

    if not rows:
        print("(no rows)")
        return

    print(" | ".join(columns))
    for row in rows:
        print(" | ".join(str(v) for v in row))
    if len(rows) == ROW_LIMIT:
        print(f"... (truncated at {ROW_LIMIT} rows)")


def main() -> None:
    init_db()
    schema = get_schema_description()
    client = Anthropic()

    print("NL-to-SQL Copilot. Schema:")
    print(schema)
    print("\nAsk a question about the shop database (Ctrl+C to quit).\n")

    while True:
        try:
            question = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break
        if not question:
            continue
        ask(client, question, schema)


if __name__ == "__main__":
    sys.exit(main())
