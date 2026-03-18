"""
planner.py — Google ADK Planner Agent
======================================
Converts a natural-language pricing instruction into a validated
ExecutionPlan using Google ADK's LlmAgent with output_schema enforcement.

Usage:
    python src/planner.py \
        --instruction "Increase prices by 10% for all in-stock fitness products." \
        --plan-out plans/generated_plan.json

    # Or import and call directly:
    from src.planner import run_planner
    plan = run_planner("Discount all yoga products by 5%.")
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import vertexai
from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.runners import InMemoryRunner
from google.genai import types

from models import Action, ExecutionPlan, Filter, Operation, Options

# ---------------------------------------------------------------------------
# System prompt — intent and reasoning only.
# Schema enforcement is handled by output_schema=ExecutionPlan (ADK + Pydantic).
# ---------------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = """
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
""".strip()


# ---------------------------------------------------------------------------
# Build the ADK agent
# ---------------------------------------------------------------------------

def build_planner_agent(model: str = "gemini-2.5-flash") -> LlmAgent:
    """
    Build and return the planner LlmAgent.

    Uses Vertex AI backend with Application Default Credentials (ADC).
    Set GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION env vars, then
    authenticate with: gcloud auth application-default login

    output_schema=ExecutionPlan enforces JSON structure via Pydantic.
    output_key="execution_plan" stores the result in session state.

    The instruction is wrapped in an InstructionProvider function to prevent
    ADK from treating curly braces in the prompt as state variable templates.
    """
    # Initialise Vertex AI with ADC — reads GOOGLE_CLOUD_PROJECT and
    # GOOGLE_CLOUD_LOCATION from environment, falls back to sensible defaults
    project  = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    vertexai.init(project=project, location=location)

    def instruction_provider(context: ReadonlyContext) -> str:
        return PLANNER_SYSTEM_PROMPT

    # Prefix model with "vertexai/" so ADK routes through Vertex AI (ADC)
    # instead of the Gemini API (API key)
    return LlmAgent(
        name="planner_agent",
        model=f"{model}",
        description="Converts a natural-language pricing instruction into a structured ExecutionPlan.",
        instruction=instruction_provider,
        output_schema=ExecutionPlan,
        output_key="execution_plan",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_planner(
    instruction: str,
    model: str = "gemini-2.5-flash",
) -> ExecutionPlan:
    """
    Run the planner agent against a natural-language instruction.

    Returns a validated ExecutionPlan instance.
    Raises ValueError if the agent returns an empty or unparseable response.

    Args:
        instruction: Natural-language pricing instruction.
        model:       Gemini model string to use (default: gemini-2.5-flash).

    Returns:
        ExecutionPlan — fully validated Pydantic model instance.
    """
    agent = build_planner_agent(model=model)

    runner = InMemoryRunner(agent=agent, app_name="mini_execution_agent")

    # create_session is async
    session = await runner.session_service.create_session(
        app_name="mini_execution_agent",
        user_id="planner",
    )

    # Inject current timestamp so the agent can populate created_at
    timestamped_instruction = (
        f"Current UTC time: {datetime.now(timezone.utc).isoformat()}\n\n"
        f"Instruction: {instruction}"
    )

    # Collect events asynchronously
    final_response = None
    async for event in runner.run_async(
        user_id="planner",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part(text=timestamped_instruction)],
        ),
    ):
        if event.is_final_response() and event.content:
            final_response = event

    if final_response is None:
        raise ValueError("Planner agent returned no response.")

    # ADK stores output_schema result in session state under output_key
    updated_session = await runner.session_service.get_session(
        app_name="mini_execution_agent",
        user_id="planner",
        session_id=session.id,
    )

    raw_plan = updated_session.state.get("execution_plan")
    if raw_plan is None:
        raise ValueError(
            "Planner agent did not produce an execution_plan in session state. "
            "Check that output_schema and output_key are set correctly on the agent."
        )

    # Validate and return — handles both dict and JSON string from ADK state
    if isinstance(raw_plan, str):
        return ExecutionPlan.model_validate_json(raw_plan)
    return ExecutionPlan.model_validate(raw_plan)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Planner agent: converts a natural-language instruction into an execution plan JSON."
    )
    parser.add_argument(
        "--instruction",
        required=True,
        help='Natural-language pricing instruction (e.g. "Increase fitness prices by 10%.")',
    )
    parser.add_argument(
        "--plan-out",
        required=True,
        help="Path to write the generated plan JSON (e.g. plans/generated_plan.json)",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-flash",
        help="Gemini model to use (default: gemini-2.5-flash)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="Pretty-print the output JSON (default: true)",
    )
    args = parser.parse_args()

    print(f"[Planner] Instruction : {args.instruction}")
    print(f"[Planner] Model       : {args.model}")
    print(f"[Planner] Output      : {args.plan_out}")
    print()

    try:
        plan = asyncio.run(run_planner(instruction=args.instruction, model=args.model))
    except Exception as e:
        print(f"[ERROR] Planner failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Write plan to disk
    output_path = Path(args.plan_out)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_path.write_text(
        plan.model_dump_json(
            indent=2 if args.pretty else None,
            exclude_none=True           # ← key change
        )
    )

    print(f"[OK] Plan generated successfully.")
    print(f"     execution_id : {plan.execution_id}")
    print(f"     operations   : {len(plan.operations)}")
    for op in plan.operations:
        print(f"       {op.operation_id}: {op.description}")
    print(f"     Written to   : {args.plan_out}")


if __name__ == "__main__":
    main()