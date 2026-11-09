"""Per-route model and effort settings, plus a cost table.

Each pipeline step (route) can use its own model and effort, so evaluations
can decide, per route, whether a cheaper setting holds quality. Defaults:
one strong model, with effort tuned per route. Override with environment
variables:

    MEDCHRON_MODEL=claude-opus-5-5           # default for all routes
    MEDCHRON_MODEL_CLASSIFY=claude-haiku-5-5 # per-route override
    MEDCHRON_EFFORT_EXTRACT=high
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from medchron.llm.types import Usage

DEFAULT_MODEL = "claude-opus-5-5"

DEFAULT_EFFORT: dict[str, str] = {
    "classify": "low",
    "extract": "medium",
    "analyze": "high",
    "verify": "medium",
    "draft": "high",
}


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens. Check current pricing before relying on totals."""

    input: float
    output: float
    cache_read: float
    cache_write: float


# First-party API list prices when this was written. Cache writes are assumed
# at 1.25x input (5-minute TTL). The Haiku cache-read rate is an estimate at
# 0.1x input. Verify current pricing before quoting any cost figures.
PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125),
}


def model_for(route: str) -> str:
    return os.environ.get(
        f"MEDCHRON_MODEL_{route.upper()}", os.environ.get("MEDCHRON_MODEL", DEFAULT_MODEL)
    )


def effort_for(route: str) -> str:
    return os.environ.get(f"MEDCHRON_EFFORT_{route.upper()}", DEFAULT_EFFORT.get(route, "medium"))


def cost_usd(model: str, usage: Usage) -> float:
    price = PRICES.get(model)
    if price is None:
        return 0.0
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_input_tokens * price.cache_read
        + usage.cache_creation_input_tokens * price.cache_write
    ) / 1_000_000
