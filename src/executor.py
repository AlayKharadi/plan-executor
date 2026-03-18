"""
executor.py — Deterministic Execution Agent
============================================
Validates a JSON plan against the schema, applies operations to a CSV
dataset atomically, persists idempotency state in SQLite, and writes
a structured audit log.

Usage:
    uv run src/executor.py \
        --plan  plans/example_plan.json \
        --csv   data/products.csv \
        --out   data/products_updated.csv \
        --audit logs/audit.jsonl \
        --db    executions.db
"""

import argparse
import copy
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

# ---------------------------------------------------------------------------
# Schema (inline so the executor is self-contained; could also load from file)
# ---------------------------------------------------------------------------
SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ExecutionPlan",
    "type": "object",
    "required": ["execution_id", "created_at", "source_instruction", "operations"],
    "additionalProperties": False,
    "properties": {
        "execution_id":       {"type": "string", "pattern": "^[a-zA-Z0-9_-]{8,64}$"},
        "created_at":         {"type": "string", "format": "date-time"},
        "source_instruction": {"type": "string", "minLength": 5},
        "operations":         {"type": "array", "minItems": 1, "items": {"$ref": "#/definitions/Operation"}},
        "summary_prompt":     {"type": "string"},
    },
    "definitions": {
        "Operation": {
            "type": "object",
            "required": ["operation_id", "filter", "action"],
            "additionalProperties": False,
            "properties": {
                "operation_id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,32}$"},
                "description":  {"type": "string"},
                "filter":       {"$ref": "#/definitions/Filter"},
                "action":       {"$ref": "#/definitions/Action"},
                "options":      {"$ref": "#/definitions/Options"},
            },
        },
        "Filter": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "categories": {"type": ["array", "null"], "items": {"type": "string"}},
                "in_stock":   {"type": ["boolean", "null"]},
                "skus":       {"type": ["array", "null"], "items": {"type": "string"}},
                "price_gte":  {"type": ["number", "null"]},
                "price_lte":  {"type": ["number", "null"]},
            },
        },
        "Action": {
            "type": "object",
            "required": ["type", "value"],
            "additionalProperties": False,
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "percent_increase", "percent_decrease",
                        "fixed_increase",   "fixed_decrease",
                        "set_price",        "set_stock",
                    ],
                },
                "value": {"oneOf": [{"type": "number", "minimum": 0}, {"type": "boolean"}]},
            },
        },
        "Options": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "round_to":  {"type": "integer", "minimum": 0, "maximum": 6, "default": 2},
                "min_price": {"type": ["number", "null"]},
                "max_price": {"type": ["number", "null"]},
            },
        },
    },
}


# ---------------------------------------------------------------------------
# SQLite idempotency store
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    """Create the executions table if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS executions (
            execution_id TEXT PRIMARY KEY,
            status       TEXT NOT NULL,          -- 'completed' | 'failed'
            executed_at  TEXT NOT NULL,
            plan_json    TEXT NOT NULL,
            audit_path   TEXT
        )
    """)
    conn.commit()
    return conn


def already_executed(conn: sqlite3.Connection, execution_id: str) -> dict | None:
    """
    Return the stored record if this execution_id has already COMPLETED, else None.
    Failed executions return None — they are allowed to retry.
    """
    row = conn.execute(
        "SELECT status, executed_at, audit_path FROM executions WHERE execution_id = ? AND status = 'completed'",
        (execution_id,),
    ).fetchone()
    if row:
        return {"status": row[0], "executed_at": row[1], "audit_path": row[2]}
    return None


def record_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    status: str,
    plan: dict,
    audit_path: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO executions (execution_id, status, executed_at, plan_json, audit_path)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            execution_id,
            status,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(plan),
            audit_path,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_csv(path: str) -> list[dict]:
    """Load CSV into a list of row dicts. Preserves original types as strings."""
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def save_csv(rows: list[dict], path: str) -> None:
    if not rows:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

def row_matches_filter(row: dict, filt: dict) -> bool:
    """Return True if a CSV row satisfies ALL conditions in the filter."""

    # SKU allowlist (takes precedence) — null treated as omitted
    skus = filt.get("skus")
    if skus is not None:
        if row["sku"] not in skus:
            return False

    # Category filter — null treated as omitted
    categories = filt.get("categories")
    if categories is not None:
        if row["category"] not in categories:
            return False

    # in_stock filter — null/missing = match all
    in_stock_filter = filt.get("in_stock")
    if in_stock_filter is not None:
        row_in_stock = row["in_stock"].strip().lower() == "true"
        if row_in_stock != in_stock_filter:
            return False

    # Price range filters — null treated as omitted
    price = float(row["price"])
    price_gte = filt.get("price_gte")
    price_lte = filt.get("price_lte")
    if price_gte is not None and price < price_gte:
        return False
    if price_lte is not None and price > price_lte:
        return False

    return True


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

def apply_action(row: dict, action: dict, options: dict) -> dict:
    """
    Apply an action to a single row and return the mutated row.
    Does NOT mutate the input — returns a new dict.
    """
    row = dict(row)  # shallow copy: safe since all values are primitives
    action_type = action["type"]
    value = action["value"]
    round_to = options.get("round_to", 2)
    min_price = options.get("min_price")
    max_price = options.get("max_price")

    if action_type == "set_stock":
        row["in_stock"] = str(value).lower()
        return row

    # All remaining actions operate on price
    price = float(row["price"])

    if action_type == "percent_increase":
        new_price = price * (1 + value / 100)
    elif action_type == "percent_decrease":
        new_price = price * (1 - value / 100)
    elif action_type == "fixed_increase":
        new_price = price + value
    elif action_type == "fixed_decrease":
        new_price = price - value
    elif action_type == "set_price":
        new_price = float(value)
    else:
        raise ValueError(f"Unknown action type: {action_type!r}")

    # Clamp to floor/ceiling if specified
    if min_price is not None:
        new_price = max(new_price, min_price)
    if max_price is not None:
        new_price = min(new_price, max_price)

    row["price"] = str(round(new_price, round_to))
    return row


# ---------------------------------------------------------------------------
# Core executor
# ---------------------------------------------------------------------------

def execute_plan(plan: dict, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Apply all operations in the plan to an in-memory copy of the rows.

    Returns:
        (updated_rows, change_records)
        change_records: one entry per modified row per operation.
    """
    # Work on a deep copy — original untouched until we decide to commit
    working_rows = copy.deepcopy(rows)
    change_records = []

    for op in plan["operations"]:
        filt    = op["filter"]
        action  = op["action"]
        options = op.get("options", {})
        op_id   = op["operation_id"]

        for i, row in enumerate(working_rows):
            if not row_matches_filter(row, filt):
                continue

            before = dict(row)
            after  = apply_action(row, action, options)
            working_rows[i] = after

            # Only record rows that actually changed
            if before != after:
                change_records.append({
                    "operation_id": op_id,
                    "sku":          row["sku"],
                    "before":       before,
                    "after":        after,
                })

    return working_rows, change_records


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def build_audit_log(
    plan: dict,
    change_records: list[dict],
    status: str,
    error: str | None = None,
) -> dict:
    skus_changed = [r["sku"] for r in change_records]
    return {
        "execution_id":       plan["execution_id"],
        "source_instruction": plan["source_instruction"],
        "executed_at":        datetime.now(timezone.utc).isoformat(),
        "status":             status,               # completed | skipped | failed
        "error":              error,
        "operations_count":   len(plan["operations"]),
        "rows_changed":       len(change_records),
        "skus_changed":       skus_changed,
        "changes":            change_records,
        "plan_snapshot":      plan,
    }


def append_audit_log(path: str, entry: dict) -> None:
    """
    Append one audit entry as a single JSON line to the .jsonl audit log.
    Creates the file and parent directories if they don't exist.
    Each call adds one line — the file grows across multiple executions.

    Each line is a complete, self-contained audit record including
    before/after state per row and a full plan_snapshot.

    Example queries:
        grep 'completed' logs/audit.jsonl
        jq 'select(.status == "failed")' logs/audit.jsonl
        jq '.changes[] | select(.sku == "A101")' logs/audit.jsonl
    """
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class PlanValidationError(Exception):
    pass


def validate_plan(plan: dict) -> None:
    """Validate plan against the JSON schema using jsonschema Draft-7."""
    validator = jsonschema.Draft7Validator(SCHEMA)
    errors = sorted(validator.iter_errors(plan), key=lambda e: e.path)
    if errors:
        messages = "\n".join(f"  - {e.json_path}: {e.message}" for e in errors)
        raise PlanValidationError(f"Plan validation failed:\n{messages}")


def strip_nulls(obj):
    """
    Recursively remove keys with None/null values from dicts.
    Pydantic serialises unset Optional fields as null — the JSON Schema
    treats omitted fields as optional, so nulls must be stripped before
    validation to avoid false type errors.
    """
    if isinstance(obj, dict):
        return {k: strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [strip_nulls(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Deterministic Execution Agent")
    parser.add_argument("--plan",  required=True, help="Path to plan JSON file")
    parser.add_argument("--csv",   required=True, help="Path to input CSV")
    parser.add_argument("--out",   required=True, help="Path for updated CSV output")
    parser.add_argument("--audit", required=True, help="Path for audit log JSONL file (appended, not overwritten)")
    parser.add_argument("--db",    default="executions.db", help="SQLite DB path (default: executions.db)")
    args = parser.parse_args()

    # 1. Load plan
    try:
        plan = json.loads(Path(args.plan).read_text())
    except Exception as e:
        print(f"[ERROR] Could not load plan: {e}", file=sys.stderr)
        sys.exit(1)

    execution_id = plan.get("execution_id", "<unknown>")

    # 2. Strip nulls then validate plan against schema
    plan = strip_nulls(plan)
    try:
        validate_plan(plan)
    except PlanValidationError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(2)

    # 3. Init SQLite and check idempotency
    conn = init_db(args.db)
    prior = already_executed(conn, execution_id)
    if prior:
        print(
            f"[SKIPPED] execution_id '{execution_id}' already ran "
            f"(status={prior['status']}, at={prior['executed_at']}). "
            f"Audit log: {prior['audit_path']}"
        )
        audit = build_audit_log(plan, [], "skipped")
        append_audit_log(args.audit, audit)
        sys.exit(0)

    # 4. Load CSV
    try:
        rows = load_csv(args.csv)
    except Exception as e:
        print(f"[ERROR] Could not load CSV: {e}", file=sys.stderr)
        sys.exit(1)

    # 5. Execute (atomic: work in memory, commit only on success)
    try:
        updated_rows, change_records = execute_plan(plan, rows)
    except Exception as e:
        # Execution failed — original CSV is untouched (nothing written yet)
        audit = build_audit_log(plan, [], "failed", error=str(e))
        append_audit_log(args.audit, audit)
        record_execution(conn, execution_id, "failed", plan, args.audit)
        print(f"[ERROR] Execution failed: {e}", file=sys.stderr)
        sys.exit(3)

    # 6. Commit: write updated CSV and audit log to disk
    try:
        save_csv(updated_rows, args.out)
        audit = build_audit_log(plan, change_records, "completed")
        append_audit_log(args.audit, audit)
        record_execution(conn, execution_id, "completed", plan, args.audit)
    except Exception as e:
        print(f"[ERROR] Failed to write output files: {e}", file=sys.stderr)
        sys.exit(4)

    # 7. Report
    print(f"[OK] Execution '{execution_id}' completed.")
    print(f"     Rows changed : {len(change_records)}")
    print(f"     SKUs affected: {[r['sku'] for r in change_records]}")
    print(f"     Updated CSV  : {args.out}")
    print(f"     Audit log    : {args.audit}")


if __name__ == "__main__":
    main()