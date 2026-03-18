"""
models.py — Pydantic models for the Execution Plan schema
==========================================================
Mirrors execution_plan_schema.json exactly.
Compatible with Google ADK (which uses Pydantic v2 under the hood).

Usage:
    from models import ExecutionPlan

    plan = ExecutionPlan.model_validate(json.loads(plan_json))
    # or
    plan = ExecutionPlan.model_validate_json(plan_json_string)
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    percent_increase = "percent_increase"
    percent_decrease = "percent_decrease"
    fixed_increase   = "fixed_increase"
    fixed_decrease   = "fixed_decrease"
    set_price        = "set_price"
    set_stock        = "set_stock"


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

class Filter(BaseModel):
    """
    Selects which CSV rows an operation applies to.
    All specified conditions are ANDed together.
    Omitting a field means 'match all' for that dimension.
    """

    categories: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        description="Match rows whose 'category' is in this list.",
    )
    in_stock: Optional[bool] = Field(
        default=None,
        description="True = in-stock only, False = out-of-stock only, None = all rows.",
    )
    skus: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        description="If provided, match only these exact SKUs (takes precedence).",
    )
    price_gte: Optional[float] = Field(
        default=None,
        ge=0,
        description="Match rows where price >= this value.",
    )
    price_lte: Optional[float] = Field(
        default=None,
        ge=0,
        description="Match rows where price <= this value.",
    )

    @model_validator(mode="after")
    def price_range_check(self) -> Filter:
        if (
            self.price_gte is not None
            and self.price_lte is not None
            and self.price_gte > self.price_lte
        ):
            raise ValueError("price_gte must be <= price_lte")
        return self


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

class Action(BaseModel):
    """
    The transformation to apply to matched rows.

    - percent_increase / percent_decrease : value is a percentage (e.g. 10 = 10%)
    - fixed_increase / fixed_decrease     : value is an absolute amount
    - set_price                           : value overrides price directly
    - set_stock                           : value is a boolean (True/False)
    """

    type: ActionType
    value: float | bool = Field(
        description="Number for price actions; boolean for set_stock.",
    )

    @model_validator(mode="after")
    def validate_value_type(self) -> Action:
        if self.type == ActionType.set_stock:
            if not isinstance(self.value, bool):
                raise ValueError("value must be a boolean when action type is set_stock")
        else:
            if isinstance(self.value, bool):
                raise ValueError(f"value must be a number when action type is {self.type}")
            if self.value < 0:
                raise ValueError("value must be >= 0 for price actions")
        return self


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

class Options(BaseModel):
    """Optional execution controls applied after the action calculation."""

    round_to: int = Field(
        default=2,
        ge=0,
        le=6,
        description="Decimal places to round the result to (default: 2).",
    )
    min_price: Optional[float] = Field(
        default=None,
        ge=0,
        description="Floor: result price will never go below this.",
    )
    max_price: Optional[float] = Field(
        default=None,
        ge=0,
        description="Ceiling: result price will never exceed this.",
    )

    @model_validator(mode="after")
    def price_bounds_check(self) -> Options:
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price must be <= max_price")
        return self


# ---------------------------------------------------------------------------
# Operation
# ---------------------------------------------------------------------------

class Operation(BaseModel):
    """A single rule: select rows with filter, apply action, use options."""

    operation_id: str = Field(
        pattern=r"^[a-zA-Z0-9_-]{1,32}$",
        description="Unique ID for this operation within the plan.",
    )
    description: Optional[str] = Field(
        default=None,
        description="Human-readable explanation of what this operation does.",
    )
    filter: Filter
    action: Action
    options: Options = Field(default_factory=Options)


# ---------------------------------------------------------------------------
# ExecutionPlan (root)
# ---------------------------------------------------------------------------

class ExecutionPlan(BaseModel):
    """
    Top-level execution plan produced by the planner and consumed by the executor.
    Pass this directly to Google ADK tools or validate from JSON with model_validate_json().
    """

    execution_id: str = Field(
        pattern=r"^[a-zA-Z0-9_-]{8,64}$",
        description="Unique plan ID. Used for idempotency — re-running the same ID is a no-op.",
    )
    created_at: str = Field(
        description="ISO 8601 timestamp of when this plan was generated.",
    )
    source_instruction: str = Field(
        min_length=5,
        description="The original natural-language instruction that produced this plan.",
    )
    operations: list[Operation] = Field(
        min_length=1,
        description="Ordered list of operations to apply sequentially.",
    )
    summary_prompt: Optional[str] = Field(
        default=None,
        description="Instruction for the executor to produce a human-readable summary.",
    )


# ---------------------------------------------------------------------------
# Google ADK — usage note
# ---------------------------------------------------------------------------
# Pass the Pydantic class directly as a tool function type hint.
# ADK introspects it and generates the JSON schema automatically.
#
#   from google.adk.agents import Agent
#   from models import ExecutionPlan
#
#   def generate_execution_plan(plan: ExecutionPlan) -> dict:
#       """Convert a natural language instruction into an execution plan."""
#       return plan.model_dump()
#
#   agent = Agent(
#       name="planner_agent",
#       model="gemini-2.0-flash",
#       tools=[generate_execution_plan],
#   )


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample = {
        "execution_id": "plan_fitness_price_increase_10pct_v1",
        "created_at": "2024-06-01T10:00:00Z",
        "source_instruction": (
            "Increase prices by 10% for all in-stock fitness products. "
            "Do not change prices for out-of-stock items."
        ),
        "operations": [
            {
                "operation_id": "op_01",
                "description": "10% increase for in-stock fitness SKUs only.",
                "filter": {"categories": ["fitness"], "in_stock": True},
                "action": {"type": "percent_increase", "value": 10},
                "options": {"round_to": 2},
            }
        ],
        "summary_prompt": "Summarise what changed.",
    }

    plan = ExecutionPlan.model_validate(sample)
    print("✓ Validation passed")
    print(f"  execution_id : {plan.execution_id}")
    print(f"  operations   : {len(plan.operations)}")
    print(f"  action type  : {plan.operations[0].action.type}")
    print(f"  filter       : {plan.operations[0].filter}")