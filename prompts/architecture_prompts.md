# Architecture Prompt

This is the prompt used to design the overall system architecture for the Mini Execution Agent.

---

## Prompt

```
You are a senior software architect. I need to design a local execution agent that:

1. Accepts a natural-language instruction (e.g. "Increase prices by 10% for all in-stock
   fitness products")
2. Converts it into a structured, validated execution plan
3. Applies that plan deterministically to a CSV dataset
4. Produces a full audit log of every change (before/after state per row)
5. Is safe under retries — running the same instruction twice must never apply changes twice

Design the architecture for this system. I want you to think carefully about:

SEPARATION OF CONCERNS
- How should the system be split so that the non-deterministic part (LLM) is isolated
  from the deterministic part (execution)?
- What is the minimal interface between the planner and the executor?
- How do we ensure the executor can be tested independently without an LLM?

SCHEMA DESIGN
- What should the execution plan JSON look like?
- It must support multiple pricing change scenarios without rewriting core logic:
    - Percentage increase/decrease
    - Fixed amount increase/decrease
    - Set absolute price
    - Mark items in_stock true/false
- It must support filtering rows by: category, in_stock status, specific SKUs, price range
- It must support batching multiple operations in a single plan
- How do we make the schema strict enough to prevent LLM hallucinations, but flexible
  enough to cover new scenarios without code changes?

IDEMPOTENCY
- The executor must be safe under retries
- If the same execution_id is submitted twice, changes must not be applied twice
- What is the right persistence mechanism for tracking execution state?
- Should a failed execution block retries?

ATOMICITY
- What happens if the plan fails halfway through (e.g. row 3 of 5 errors)?
- How do we ensure the source CSV is never left in a partial state?

AUDITABILITY
- What must the audit log capture to be useful for forensic purposes?
- How do we ensure the audit log is self-contained (not dependent on external files)?

EXTENSIBILITY
- A new instruction should require only a new plan JSON — zero executor code changes
- How does the schema design enforce this?

The dataset is a CSV with columns: sku, category, price, in_stock.

Please design the full architecture with component responsibilities, interfaces,
data flow, and the execution plan JSON schema.
```

---

## Key Decisions Produced by This Prompt

### 1. Planner / executor split
The LLM is involved exactly once — at plan generation time. The executor is pure Python with no LLM dependency. This means the executor is fully testable with any hand-written plan JSON, and LLM non-determinism cannot affect execution outcomes.

### 2. JSON plan as the interface contract
The execution plan is the only thing that crosses the boundary between planner and executor. Both components are independently replaceable as long as they speak the same schema. The planner could be swapped from Google ADK to any other LLM framework without touching the executor.

### 3. Strict schema with enum-constrained action types
Action types are a closed enum (`percent_increase`, `percent_decrease`, `fixed_increase`, `fixed_decrease`, `set_price`, `set_stock`). The LLM cannot invent new action types. This prevents hallucinated operations that the executor doesn't know how to handle.

### 4. SQLite for idempotency state
SQLite provides atomic writes, concurrent access safety, and queryability — advantages over a plain JSON file. Failed executions do not block retries (only `completed` status blocks re-execution).

### 5. In-memory execution with atomic commit
All CSV mutations happen on a deep copy in memory. The updated CSV is written to disk only after all operations succeed. If any operation raises, the source CSV is untouched.

### 6. Self-contained audit log
The audit log stores a full `plan_snapshot` field — a copy of the plan JSON as executed. This means the audit log is forensically complete even if the plan file is later modified or deleted.

### 7. Operations as an array
A single instruction can produce multiple independent rules (e.g. "increase fitness prices AND mark accessories out-of-stock"). An array of operations handles this without requiring multiple plan files or multiple executor invocations.