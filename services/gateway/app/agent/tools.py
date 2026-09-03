"""
Agent tools.

A `Tool` is a name + description + JSON-schema for its inputs + a function that
runs it. The description and schema are what we send to the model (so it knows
when and how to call the tool); the function is what *we* execute when the model
asks. This separation — the model decides, our code executes — is the core of
safe tool use.

Stage 3 ships one deterministic, sandbox-free tool: a **calculator** that
evaluates a basic arithmetic expression via a restricted AST walk (NOT Python's
`eval`, which would let the model run arbitrary code). Keeping tools deterministic
and side-effect-free also makes the agent loop easy to test.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    _run: Callable[[dict], Awaitable[str] | str]

    def definition(self) -> dict:
        """The shape the model sees (Anthropic tool definition)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    async def run(self, tool_input: dict) -> str:
        result = self._run(tool_input)
        if isinstance(result, Awaitable):
            return await result
        return result


# ---- calculator (safe arithmetic) --------------------------------------

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node: ast.AST) -> float:
    """Recursively evaluate ONLY numbers and arithmetic operators.

    Anything else (names, calls, attributes, ...) raises — so the model can't
    smuggle code through the calculator.
    """
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_safe_eval(node.operand))
    raise ValueError("unsupported expression")


def _calculator(tool_input: dict) -> str:
    expression = str(tool_input.get("expression", ""))
    try:
        value = _safe_eval(ast.parse(expression, mode="eval"))
    except Exception:
        return f"Error: '{expression}' is not a valid arithmetic expression."
    return str(value)


calculator = Tool(
    name="calculator",
    description=(
        "Evaluate a basic arithmetic expression and return the numeric result. "
        "Supports + - * / // % ** and parentheses. Use this for any math."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "e.g. '2 + 2 * (3 - 1)'",
            }
        },
        "required": ["expression"],
    },
    _run=_calculator,
)


# The default toolset the worker hands to the agent.
DEFAULT_TOOLS: list[Tool] = [calculator]
