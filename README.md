# Mini Execution Agent

A deterministic, auditable execution agent that translates natural-language pricing instructions into structured JSON plans and safely applies them to a CSV dataset.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Repository Structure](#3-repository-structure)
4. [File Reference](#4-file-reference)
5. [Data Model & Schema](#5-data-model--schema)
6. [Setup & Installation](#6-setup--installation)
7. [Running the Agent](#7-running-the-agent)
8. [Adding a New Scenario](#8-adding-a-new-scenario)
9. [Idempotency](#9-idempotency)
10. [Audit Log Format](#10-audit-log-format)
11. [LLM Prompts](#11-llm-prompts)
12. [Design Decisions](#12-design-decisions)
13. [Submission Checklist](#13-submission-checklist)

---

## 1. Project Overview

This agent accepts a plain-English instruction such as:

> "Increase prices by 10% for all in-stock fitness products. Do not change prices for out-of-stock items."

It then:

1. **Plans** — an LLM (via Google ADK / Gemini) converts the instruction into a validated JSON execution plan
2. **Validates** — the plan is checked against a strict Pydantic schema before any data is touched
3. **Executes** — a deterministic Python executor applies the plan to a CSV dataset in memory
4. **Commits atomically** — only if execution succeeds does it write the updated CSV and audit log to disk
5. **Logs** — a structured JSON audit log records every before/after change, keyed by `execution_id`
6. **Protects against retries** — SQLite persists completed executions; re-running the same `execution_id` is always a no-op

---

## 2. Architecture

```
Natural Language Instruction
          │
          ▼
  ┌───────────────┐
  │  Planner      │  ← Google ADK Agent (Gemini)
  │  (planner.py) │    Receives instruction + schema context
  │               │    Outputs a valid ExecutionPlan JSON
  └───────┬───────┘
          │  ExecutionPlan (Pydantic-validated)
          ▼
  ┌───────────────┐
  │  Executor     │  ← Pure Python, zero LLM calls
  │ (executor.py) │    1. Validate plan (Pydantic)
  │               │    2. Check idempotency (SQLite)
  │               │    3. Apply operations in memory
  │               │    4. Commit: write CSV + audit log
  └───────┬───────┘
          │
     ┌────┴─────┐
     │          │
     ▼          ▼
products_   audit.jsonl
updated.csv
```

### Key separation

| Component | Responsibility | LLM involved? |
|-----------|---------------|---------------|
| `planner.py` | Natural language → structured JSON plan | Yes |
| `models.py` | Schema definition + Pydantic validation | No |
| `executor.py` | JSON plan → CSV mutations + audit log | No |

The executor **never calls an LLM**. Given the same plan JSON, it always produces the same output. This is the core of the determinism guarantee.

---

## 3. Repository Structure

```
mini-execution-agent/
│
├── README.md                      ← This file
│
├── data/
│   ├── products.csv               ← Input dataset
│   └── products_updated.csv       ← Output after execution (generated)
│
├── plans/
│   └── example_plan.json          ← Example plan for the required instruction
│
├── schemas/
│   └── execution_plan_schema.json ← JSON Schema (Draft-7)
│
├── src/
│   ├── models.py                  ← Pydantic models (ExecutionPlan + nested)
│   ├── planner.py                 ← Google ADK planner agent
│   └── executor.py                ← Deterministic executor
│
├── logs/
│   └── audit.jsonl                ← Append-only JSONL audit log (generated)
│
├── prompts/
│   ├── architecture_prompt.md     ← Prompt used to design the system
│   ├── planner_prompt.md          ← Prompt used inside the planner agent
│   └── executor_prompt.md         ← Prompt used to design/verify executor logic
│
├── executions.db                  ← SQLite idempotency store (generated on first run)
├── pyproject.toml                 ← uv project config + dependencies
```

---

## 4. File Reference

### `src/models.py`
Pydantic v2 models that mirror `execution_plan_schema.json` exactly. This is the **single source of truth** for plan structure. Both the planner (which produces plans) and the executor (which consumes plans) import from here.

Key classes:

| Class | Purpose |
|-------|---------|
| `ExecutionPlan` | Root model — the full plan object |
| `Operation` | One rule: a filter + action + options |
| `Filter` | Row selection criteria (category, in_stock, skus, price range) |
| `Action` | The transformation to apply (type + value) |
| `ActionType` | Enum of all valid action types |
| `Options` | Execution controls (rounding, price floor/ceiling) |

**Google ADK integration:** Pass `ExecutionPlan` as a type hint on a tool function — ADK introspects it and generates the JSON schema automatically. No manual `FunctionDeclaration` needed.

```python
from google.adk.agents import Agent
from src.models import ExecutionPlan

def generate_execution_plan(plan: ExecutionPlan) -> dict:
    """Convert a natural language instruction into a structured execution plan."""
    return plan.model_dump()

agent = Agent(
    name="planner_agent",
    model="gemini-2.0-flash",
    tools=[generate_execution_plan],
)
```

> **Important:** Import all nested models (`Operation`, `Filter`, `Action`, `Options`) in the same module as the tool function — ADK's schema resolver needs them in scope even if you only reference `ExecutionPlan` directly.

---

### `src/planner.py`
Google ADK agent that wraps the planner tool. Accepts a natural-language instruction, injects the schema context into the system prompt, and returns a validated `ExecutionPlan`.

**Inputs:** Natural language string  
**Outputs:** `ExecutionPlan` Pydantic object (or raises `ValidationError` if the LLM output is malformed)

The planner is the **only** component that calls an LLM. It is intentionally thin — its sole job is to produce a valid plan. No CSV reading, no execution logic.

---

### `src/executor.py`
Pure Python executor. Self-contained — no LLM, no network calls.

**Inputs:**
- `--plan`  : path to plan JSON file
- `--csv`   : path to input CSV
- `--out`   : path for updated CSV output
- `--audit` : path for audit JSONL log (appended, not overwritten)
- `--db`    : path to SQLite DB (default: `executions.db`)

**Execution steps (in order):**

1. Load and parse plan JSON
2. Validate against Pydantic schema — rejects invalid plans before touching any data
3. Check SQLite for `execution_id` — skip entirely if already completed
4. Load CSV into memory
5. For each operation: filter rows, apply action, collect before/after records
6. On success: write updated CSV + append to audit JSONL, record in SQLite
7. On any failure: nothing is written to disk (atomicity guarantee)

```bash
uv run src/executor.py \
  --plan  plans/example_plan.json \
  --csv   data/products.csv \
  --out   data/products_updated.csv \
  --audit logs/audit.jsonl \
  --db    executions.db
```

---

### `schemas/execution_plan_schema.json`
JSON Schema (Draft-7) for the execution plan. Used as documentation and for optional validation via `jsonschema` library. The Pydantic models in `models.py` are the canonical runtime representation.

---

### `plans/example_plan.json`
The execution plan generated by the planner for the required instruction:

> "Increase prices by 10% for all in-stock fitness products."

This is what the LLM produces. The executor consumes this file directly.

---

### `logs/audit.jsonl`
Append-only JSONL audit log. Each execution appends one line — the file grows across multiple runs and is never overwritten. See [Audit Log Format](#10-audit-log-format) for the per-line schema.

---

### `executions.db`
SQLite database created automatically on first run. Contains one row per completed execution. Used exclusively for idempotency checks.

Schema:
```sql
CREATE TABLE executions (
    execution_id TEXT PRIMARY KEY,
    status       TEXT NOT NULL,   -- 'completed' | 'failed' | 'skipped'
    executed_at  TEXT NOT NULL,
    plan_json    TEXT NOT NULL,
    audit_path   TEXT
);
```

---

## 5. Data Model & Schema

### Action types

| Type | Value type | Effect |
|------|-----------|--------|
| `percent_increase` | number (e.g. `10` = 10%) | `price = price * (1 + value/100)` |
| `percent_decrease` | number | `price = price * (1 - value/100)` |
| `fixed_increase` | number | `price = price + value` |
| `fixed_decrease` | number | `price = price - value` |
| `set_price` | number | `price = value` |
| `set_stock` | boolean | `in_stock = value` |

### Filter fields (all optional, ANDed together)

| Field | Type | Behaviour when omitted |
|-------|------|------------------------|
| `categories` | `list[str]` | Match all categories |
| `in_stock` | `bool \| null` | Match all rows |
| `skus` | `list[str]` | Match all SKUs |
| `price_gte` | `float` | No lower bound |
| `price_lte` | `float` | No upper bound |

### Minimal valid plan

```json
{
  "execution_id": "my-plan-001",
  "created_at": "2024-06-01T10:00:00Z",
  "source_instruction": "Set all yoga products to $15.",
  "operations": [
    {
      "operation_id": "op_01",
      "filter": { "categories": ["yoga"] },
      "action": { "type": "set_price", "value": 15.00 }
    }
  ]
}
```

---

## 6. Setup & Installation

### Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) — fast Python package manager

### Install dependencies

```bash
uv sync
```

This installs all dependencies defined in `pyproject.toml`:

```
google-adk>=0.1.0
pydantic>=2.0.0
jsonschema>=4.0.0
```

### Install dev dependencies (optional, for testing)

```bash
uv sync --dev
```

### Authentication

This project uses **Application Default Credentials (ADC)** via Vertex AI — no API key required.

**One-time setup:**

```bash
# 1. Install Google Cloud CLI if not already installed
# https://cloud.google.com/sdk/docs/install

# 2. Log in with ADC
gcloud auth application-default login

# 3. Set your project
gcloud config set project your-gcp-project-id
```

**Required environment variables:**

```bash
export GOOGLE_CLOUD_PROJECT=your-gcp-project-id
export GOOGLE_GENAI_USE_VERTEXAI=True

# Optional — defaults to us-central1 if not set
export GOOGLE_CLOUD_LOCATION=us-central1
```

`GOOGLE_GENAI_USE_VERTEXAI=True` tells the GenAI SDK to route all calls through Vertex AI using your ADC credentials. The model name is passed directly to `LlmAgent` — no special prefix required.

**The executor never calls any external API** — it is pure Python and runs entirely locally regardless of auth setup.

---


---

## 7. Running the Agent

### End-to-end (planner + executor)

```bash
uv run src/planner.py \
  --instruction "Increase prices by 10% for all in-stock fitness products." \
  --plan-out plans/generated_plan.json

uv run src/executor.py \
  --plan  plans/generated_plan.json \
  --csv   data/products.csv \
  --out   data/products_updated.csv \
  --audit logs/audit.jsonl
```

### Executor only (with a pre-written plan)

```bash
uv run src/executor.py \
  --plan  plans/example_plan.json \
  --csv   data/products.csv \
  --out   data/products_updated.csv \
  --audit logs/audit.jsonl
```

### Expected output for the required example

Input `products.csv`:
```
sku,category,price,in_stock
A101,fitness,29.99,true    ← price increases  → 32.99
A102,fitness,39.99,true    ← price increases  → 43.99
A103,fitness,49.99,false   ← skipped (out of stock)
B201,yoga,19.99,false      ← skipped (wrong category)
B202,yoga,24.99,true       ← skipped (wrong category)
C301,accessories,9.99,true ← skipped (wrong category)
C302,accessories,14.99,true← skipped (wrong category)
```

Rows changed: **2** (A101, A102)  
Rows skipped: **5**

---

## 8. Adding a New Scenario

The executor requires **zero code changes** to support a new instruction. Only a new plan JSON is needed.

**Example: Discount all yoga products by 5%**

```json
{
  "execution_id": "plan_yoga_discount_5pct_v1",
  "created_at": "2024-06-01T11:00:00Z",
  "source_instruction": "Discount all yoga products by 5%.",
  "operations": [
    {
      "operation_id": "op_01",
      "filter": { "categories": ["yoga"] },
      "action": { "type": "percent_decrease", "value": 5 },
      "options": { "round_to": 2 }
    }
  ]
}
```

**Example: Multi-operation plan — increase fitness, mark accessories as out-of-stock**

```json
{
  "execution_id": "plan_mixed_ops_v1",
  "created_at": "2024-06-01T12:00:00Z",
  "source_instruction": "Increase in-stock fitness prices by 15% and mark all accessories as out-of-stock.",
  "operations": [
    {
      "operation_id": "op_01",
      "description": "15% increase for in-stock fitness SKUs.",
      "filter": { "categories": ["fitness"], "in_stock": true },
      "action": { "type": "percent_increase", "value": 15 },
      "options": { "round_to": 2 }
    },
    {
      "operation_id": "op_02",
      "description": "Mark all accessories as out-of-stock.",
      "filter": { "categories": ["accessories"] },
      "action": { "type": "set_stock", "value": false }
    }
  ]
}
```

---

## 9. Idempotency

The executor is safe under retries. The guarantee:

> **If the same `execution_id` is submitted twice, changes are never applied twice.**

### How it works

1. Before any execution, the executor queries SQLite:
   ```sql
   SELECT status FROM executions WHERE execution_id = ?
   ```
2. If a row exists with `status = 'completed'`, execution is skipped immediately. The original CSV is not read. Nothing is written.
3. If no row exists, execution proceeds normally. On success, the `execution_id` is recorded with `status = 'completed'`.
4. A `failed` status does **not** block re-execution — the plan can be retried after fixing the underlying issue.

### What counts as the same execution

The `execution_id` is the idempotency key — not the instruction text, not the CSV content. Two plans with different `execution_id` values will both execute even if they are otherwise identical.

### Generating execution IDs

The planner generates `execution_id` as a slug from the instruction and date, e.g.:  
`plan_fitness_price_increase_10pct_v1`

To intentionally re-run a plan with fresh state, change the `execution_id` (e.g. append `_v2`).

---

## 10. Audit Log Format

Every execution **appends** one JSON line to `logs/audit.jsonl`. The file grows across multiple runs and is never overwritten. Each line is a complete, self-contained audit record.

> **Note:** The submission checklist specifies "Audit log JSON." We use JSONL (newline-delimited JSON) rather than a single JSON file so that multiple executions accumulate in one append-only file without overwriting previous records. Each line is valid JSON and fully satisfies the before/after state requirement.

```bash
# Query examples
grep 'completed' logs/audit.jsonl
jq 'select(.status == "failed")' logs/audit.jsonl
jq '.changes[] | select(.sku == "A101")' logs/audit.jsonl
jq '{id: .execution_id, changed: .rows_changed}' logs/audit.jsonl
```

Each line contains:

| Field | Description |
|-------|-------------|
| `execution_id` | Unique ID from the plan |
| `source_instruction` | Original natural-language instruction |
| `executed_at` | UTC timestamp of execution |
| `status` | `completed` \| `skipped` \| `failed` |
| `error` | Error message if status is `failed`, else `null` |
| `operations_count` | Number of operations in the plan |
| `rows_changed` | Total rows actually modified |
| `skus_changed` | List of SKUs modified |
| `changes` | Per-row before/after state for every changed row |
| `plan_snapshot` | Full copy of the plan as executed — log is forensically self-contained |

---

## 11. LLM Prompts

All prompts are stored in `prompts/`. They are part of the submission.

### `prompts/architecture_prompt.md`
The prompt used to design the overall system — planner/executor separation, schema design, idempotency strategy, and audit log structure.

### `prompts/planner_prompt.md`
The system prompt injected into the Google ADK planner agent. Focuses purely on **intent and reasoning** — not schema specification. Schema enforcement is handled at the framework level via `output_schema=ExecutionPlan`, so the prompt only instructs the LLM on:
- How to generate `execution_id` slugs
- One operation per distinct rule
- How to translate filter conditions from the instruction exactly
- Conservative interpretation rules for ambiguous instructions

### `prompts/executor_prompt.md`
The prompt used to design and verify the executor logic — covering filter AND semantics, atomic rollback strategy, idempotency check ordering, and audit log structure.

---

## 12. Design Decisions

### Why planner/executor separation?
The planner is non-deterministic (LLM output varies). The executor is fully deterministic (same plan → same result, always). Separating them means the executor can be tested independently with any plan JSON, and the LLM is only involved once — at plan generation time.

### Why Pydantic over raw JSON Schema?
Pydantic v2 integrates directly with Google ADK via type hints. The schema is defined once in `models.py` and used everywhere — ADK for tool introspection, the executor for runtime validation, and the planner for output structure.

### Why SQLite over a JSON file for idempotency?
SQLite gives atomic writes (no partial state from a crash mid-write), is queryable for inspection, and handles concurrent access correctly. A JSON file would require manual locking and could corrupt under retry scenarios.

### Why atomic execution?
All CSV mutations happen on an in-memory copy. The updated CSV is only written to disk after all operations succeed. If any operation raises an exception, the original CSV is completely untouched — no partial updates.

### Why `operations` is an array?
A single natural-language instruction can require multiple independent rules (e.g. "increase fitness prices AND mark accessories out-of-stock"). An array of operations lets the planner express this without creating multiple plans. The executor applies them sequentially.

### Why store `plan_snapshot` in the audit log?
The audit log must be self-contained for forensic purposes. If the plan JSON file is later modified or deleted, the audit log still has the complete record of exactly what instructions were executed.

---

## 13. Submission Checklist

| Item | File | Status |
|------|------|--------|
| README | `README.md` | ✅ |
| JSON Schema | `schemas/execution_plan_schema.json` | ✅ |
| Pydantic models | `src/models.py` | ✅ |
| Example plan JSON | `plans/example_plan.json` | ✅ |
| Python executor | `src/executor.py` | ✅ |
| Updated CSV output | `data/products_updated.csv` | ✅ |
| Audit log | `logs/audit.jsonl` | ✅ |
| Planner agent | `src/planner.py` | ✅ |
| Architecture prompt | `prompts/architecture_prompt.md` | ✅ |
| Planner prompt | `prompts/planner_prompt.md` | ✅ |
| Executor prompt | `prompts/executor_prompt.md` | ✅ |