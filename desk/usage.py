"""What a model call costs, for the log. Prices are dollars per million tokens, input first and output second, from the
model's page. Only Claude Haiku 5.5 is used, by the owner's order, so it is the only price here. A model that is not in the
table is logged with a warning and a cost of 0.0, so a configured model that is not priced shows up instead of looking free."""
from __future__ import annotations

import logging

log = logging.getLogger("desk.usage")

PRICES = {"claude-haiku-5-5": (0.10, 0.50)}


def cost_usd(model: str, usage) -> float:
    if model not in PRICES:
        log.warning("no price is recorded for %s, so this call is logged at a cost of 0.0", model)
        return 0.0
    price_in, price_out = PRICES[model]
    return (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000
