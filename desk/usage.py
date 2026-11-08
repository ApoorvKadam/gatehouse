"""What a model call costs, for the log. Prices are dollars per million tokens, input first and output second, from the
model's page. Only Claude Haiku 5.5 is used, by the owner's order, so it is the only price here: a model that is not in
the table logs a cost of 0.0, which is easy to see."""
from __future__ import annotations

PRICES = {"claude-haiku-5-5": (0.10, 0.50)}


def cost_usd(model: str, usage) -> float:
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    return (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000
