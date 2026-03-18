# Executor Prompt

This is the prompt used to design and verify the executor logic. Unlike the
planner prompt (which is injected at runtime into an LLM agent), this prompt
was used during development — to reason through the executor's design, validate
its correctness, and stress-test its edge cases.

The executor itself contains no LLM calls. This prompt documents the thinking
that produced the deterministic Python code in `src/executor.py`.

---

## Prompt

```
You are a senior Python engineer. I need you to design and implement a
deterministic executor for a pricing execution agent.

The executor receives a validated JSON plan (already Pydantic-validated by the
time it reaches execution) and applies it to a CSV dataset. It must be:

  - Deterministic: same plan + same CSV always produces the same output
  - Atomic: if anything fails mid-execution, the source CSV is never modified
  - Idempotent: re-running the same execution_id is always a no-op
  - Auditable: every change is logged with full before/after state

EXECUTION PLAN STRUCTURE

The plan contains an ordered array of operations. Each operation has:

  filter  — criteria to select which CSV rows to act on (all fields ANDed):
              categories: list of category strings to match
              in_stock:   true (in-stock only) | false (out-of-stock only) | omitted (all)
              skus:       specific SKU list (takes precedence over other filters)
              price_gte:  match rows where price >= value
              price_lte:  match rows where price <= value

  action  — the transformation to apply:
              type:  one of percent_increase | percent_decrease |
                          fixed_increase | fixed_decrease | set_price | set_stock
              value: number for price actions, boolean for set_stock

  options — execution controls:
              round_to:  decimal places to round to (default: 2)
              min_price: price floor (result never goes below this)
              max_price: price ceiling (result never exceeds this)

DATASET

CSV columns: sku, category, price, in_stock
All values are read as strings from the CSV and must be cast before comparison.

REQUIREMENTS

1. FILTER LOGIC
   - All filter conditions are ANDed — a row must satisfy every specified
     condition to be selected
   - Omitted filter fields match all rows (not none)
   - skus filter takes precedence: if specified, only those SKUs are considered
     regardless of other filter fields
   - in_stock comparison: CSV stores "true"/"false" as strings — normalise
     to lowercase before comparing to the boolean filter value

2. ACTION LOGIC
   For percent_increase:  new_price = price * (1 + value / 100)
   For percent_decrease:  new_price = price * (1 - value / 100)
   For fixed_increase:    new_price = price + value
   For fixed_decrease:    new_price = price - value
   For set_price:         new_price = value
   For set_stock:         in_stock = str(value).lower()  (no price change)

   After calculating new_price:
   - Apply min_price clamp: new_price = max(new_price, min_price)
   - Apply max_price clamp: new_price = min(new_price, max_price)
   - Round to round_to decimal places

3. ATOMICITY
   - Load CSV into memory as a list of dicts
   - Work on a deep copy — never mutate the original in-memory rows
   - Only write the updated CSV and audit log to disk after ALL operations
     across ALL rows complete successfully
   - If any exception is raised during execution, write nothing to disk
     (the source CSV file remains completely untouched)

4. IDEMPOTENCY
   - Use SQLite to persist execution state
   - Before executing, query: SELECT status FROM executions WHERE execution_id = ?
   - If status = 'completed' → skip execution entirely, exit with [SKIPPED]
   - If status = 'failed' → allow retry (do not block re-execution)
   - If no row exists → proceed with execution
   - After successful execution, INSERT with status = 'completed'
   - After failed execution, INSERT with status = 'failed'

5. AUDIT LOG
   Append one JSON line to a .jsonl file (never overwrite — each execution
   adds one line so the file accumulates across runs). Structure of each line:
   {
     "execution_id":       string
     "source_instruction": string
     "executed_at":        ISO 8601 UTC timestamp
     "status":             "completed" | "skipped" | "failed"
     "error":              string or null
     "operations_count":   integer
     "rows_changed":       integer
     "skus_changed":       list of strings
     "changes": [
       {
         "operation_id": string,
         "sku":          string,
         "before":       { full row dict },
         "after":        { full row dict }
       }
     ],
     "plan_snapshot":  { full plan dict }
   }

   Only record rows that actually changed (before != after).
   plan_snapshot must be the complete plan as executed — not a reference to
   an external file — so the audit log is forensically self-contained.

6. CHANGE DETECTION
   After applying an action, compare the before and after row dicts. Only
   add to change_records if they differ. This handles edge cases like:
   - A set_price action that sets price to the existing price (no-op)
   - A set_stock action on a row that already has the target stock status

EDGE CASES TO HANDLE

  - Float precision: 29.99 * 1.10 = 32.989000000000004 → must round to 2dp = 32.99
  - Empty filter: an operation with an empty filter {} matches ALL rows
  - All rows filtered out: valid execution — rows_changed = 0, not an error
  - price_gte > price_lte: should be caught at validation time (Pydantic
    model_validator), not at execution time
  - set_stock on a row already in that state: detected by change comparison,
    not added to change_records
  - CSV with extra whitespace in values: strip() before type casting

Please implement this as a clean, well-commented Python module with:
  - A load_csv / save_csv pair
  - A row_matches_filter function
  - An apply_action function
  - An execute_plan function (orchestrates filter + action across all rows)
  - SQLite helpers: init_db, already_executed, record_execution
  - An audit log builder: build_audit_log
  - A CLI entry point via argparse
```

---

## Key Decisions Produced by This Prompt

### 1. Deep copy before execution

Working on a `copy.deepcopy()` of the CSV rows — not the original list —
is the atomicity mechanism. Since Python writes happen at `save_csv()` call
time, and `save_csv()` is only called after `execute_plan()` returns
successfully, a mid-execution exception leaves the source CSV completely
untouched. No temp files, no rollback logic — just don't write until success.

### 2. Change detection by dict comparison

Rather than assuming every matched row changed, the executor compares
`before` and `after` dicts. This handles silent no-ops cleanly — a
`set_price` action that sets a price to its current value won't pollute
the audit log with phantom changes. It also means `rows_changed` is an
accurate count of actual mutations, not just matched rows.

### 3. SQLite status semantics

`completed` blocks re-execution. `failed` does not. This is a deliberate
design choice: a failed execution may have had a transient cause (malformed
CSV row, file permission error) that is worth retrying after fixing. Only
confirmed successful executions are treated as idempotency barriers.

### 4. Filter AND semantics with omit-means-all

Every filter field is optional, and omitting it means "match all rows for
this dimension." This is the correct default for a pricing agent — an
operation that specifies only `categories: ["fitness"]` should affect all
fitness rows regardless of stock status, not zero rows because `in_stock`
was omitted.

### 5. String normalisation for CSV booleans

The CSV stores `in_stock` as the strings `"true"` and `"false"`. The
filter's `in_stock` field is a Python boolean. The comparison normalises
the CSV string to lowercase before comparing:
```python
row["in_stock"].strip().lower() == "true"
```
This handles CSV values like `"True"`, `"TRUE"`, or `" true "` correctly.

### 6. Price rounding after clamping

The rounding step happens after min/max clamping, not before. This ensures
the clamp values are respected exactly — rounding before clamping could
push a value above `max_price` or below `min_price` by a rounding delta.

### 7. plan_snapshot in audit log

The full plan dict is stored verbatim in the audit log under `plan_snapshot`.
This makes the audit log forensically self-contained — if the plan JSON file
is later modified, renamed, or deleted, the audit log still shows exactly
what instructions were executed. This is standard practice in financial and
operational audit systems.

### 8. CLI interface

The executor exposes a clean argparse CLI so it can be run standalone
without importing the planner:
```bash
uv run src/executor.py \
  --plan  plans/example_plan.json \
  --csv   data/products.csv \
  --out   data/products_updated.csv \
  --audit logs/audit.jsonl \
  --db    executions.db
```
This separation allows the executor to be tested, debugged, and demonstrated
independently of the ADK planner agent.