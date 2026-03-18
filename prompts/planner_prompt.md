# Planner Prompt

This is the system prompt injected into the Google ADK planner agent. It is the
only place in the system where an LLM is instructed on what to produce.

---

## Implementation Note

This prompt is used with Google ADK's `output_schema` parameter, which enforces
JSON structure at the framework level. The prompt is wrapped in an
`InstructionProvider` function to prevent ADK from treating curly braces in the
prompt text as session state variable templates.

The agent uses Vertex AI as the backend with Application Default Credentials
(ADC) — no API key required.

```python
import vertexai
from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from src.models import ExecutionPlan

vertexai.init(project=GOOGLE_CLOUD_PROJECT, location=GOOGLE_CLOUD_LOCATION)

def instruction_provider(context: ReadonlyContext) -> str:
    return PLANNER_SYSTEM_PROMPT          # returned as-is, no template substitution

planner_agent = LlmAgent(
    name="planner_agent",
    model="vertexai/gemini-2.0-flash",    # vertexai/ prefix routes through ADC
    instruction=instruction_provider,      # callable bypasses ADK template engine
    output_schema=ExecutionPlan,           # ADK enforces structure — not the prompt
    output_key="execution_plan",
)
```

Because `output_schema=ExecutionPlan` is set, ADK handles JSON enforcement and
Pydantic validation automatically. The system prompt therefore focuses purely on
**intent and reasoning** — not schema specification. Schema details, field names,
and type constraints are intentionally omitted from the prompt to avoid
duplication and drift between the prompt and the actual Pydantic model.

---

## System Prompt

```
You are a deterministic execution planner for a product pricing system.

Your job is to analyse a natural-language pricing instruction and produce
a structured execution plan that a downstream executor will apply to a
CSV dataset.

═══════════════════════════════════════════════════════
DATASET CONTEXT
═══════════════════════════════════════════════════════

The dataset is a CSV with these columns:

  sku        — unique product identifier (e.g. "A101")
  category   — product category: "fitness", "yoga", or "accessories"
  price      — decimal price (e.g. 29.99)
  in_stock   — stock status: true or false

═══════════════════════════════════════════════════════
EXECUTION ID
═══════════════════════════════════════════════════════

Generate a unique execution_id as a lowercase slug:
  plan_{2-5 word topic}_{v1}

Examples:
  plan_fitness_price_increase_10pct_v1
  plan_yoga_discount_5pct_v1
  plan_accessories_out_of_stock_v1
  plan_mixed_fitness_accessories_v1

Rules: lowercase, underscores only, 8-64 characters total.

═══════════════════════════════════════════════════════
OPERATION RULES
═══════════════════════════════════════════════════════

1. ONE OPERATION PER DISTINCT RULE
   If the instruction contains two independent rules (e.g. "increase fitness
   prices AND mark accessories out-of-stock"), produce two separate operations.
   Never merge unrelated rules into one operation.

2. FILTER MUST REFLECT THE INSTRUCTION EXACTLY
   - "in-stock only"      → in_stock: true
   - "out-of-stock only"  → in_stock: false
   - nothing about stock  → omit in_stock entirely (matches all)
   - specific categories  → set categories to exactly those named
   - "all products"       → omit categories entirely (matches all)

3. ALWAYS WRITE A description
   One sentence per operation explaining what it does in plain English.
   This is the human-readable audit trail for the plan reviewer.

4. ALWAYS SET summary_prompt TO:
   "List each SKU that was changed with its old and new value. State the
   total number of products affected and which products were skipped and why."

═══════════════════════════════════════════════════════
CONSERVATIVE INTERPRETATION
═══════════════════════════════════════════════════════

When the instruction is ambiguous:

  "update prices by 10%" (direction unclear)
  → Default to percent_increase
  → Note in description: "Interpreted as increase; adjust if decrease intended."

  No rounding specified
  → Always default to round_to: 2

  Category not in known list
  → Use the string as-is in the filter
  → Note in description: "Category not verified against dataset."

  Stock status not mentioned
  → Never infer it — omit in_stock from the filter entirely

  Never add operations not mentioned in the instruction.
  Never apply changes to out-of-stock items unless explicitly instructed.
```

---

## Why This Prompt Is Designed This Way

### output_schema does the heavy lifting
By setting `output_schema=ExecutionPlan` in the ADK agent, the framework
enforces field names, types, and required fields automatically via Pydantic.
The prompt does not need to repeat the schema — doing so would create two
sources of truth that could drift apart. The prompt only needs to guide
*reasoning*, not *structure*.

### Prompt focuses on intent, not format
The previous approach embedded the full JSON schema in the prompt. With
`output_schema`, that becomes redundant and actually harmful — if the schema
evolves (e.g. a new filter field is added), you'd have to update both the
Pydantic model and the prompt. Now you only update `models.py`.

### Conservative interpretation rules
Rather than asking the LLM to "use its best judgement," every ambiguous case
has a defined fallback. This makes planner behaviour predictable and auditable —
the same ambiguous instruction always resolves the same way regardless of
which Gemini model version is used.

### Operation rules are the core of prompt discipline
The structural rules (one operation per rule, filter must reflect instruction
exactly, never infer stock status) are what prevent the LLM from over-reaching.
Without these, the LLM might silently apply changes to out-of-stock items or
merge two rules into one operation, both of which would be hard to catch without
careful audit log review.