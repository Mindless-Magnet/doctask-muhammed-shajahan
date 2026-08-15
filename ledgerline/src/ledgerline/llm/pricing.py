"""Bedrock on-demand prices, in USD per 1000 tokens.

Bedrock returns token usage but not cost, so cost is computed here from a committed table rather
than estimated by a model. The pull date is part of the record: any cost figure this system reports
is only as current as PRICES_PULLED_AT, and the run report says so.

Update procedure: re-read the AWS Bedrock pricing page, edit this table, bump the date, and note the
change in PROGRESS.md. An unknown model id costs nothing and is reported as unpriced rather than
silently guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICES_PULLED_AT = "2026-08-15"
PRICING_SOURCE = "https://aws.amazon.com/bedrock/pricing/ (us-east-1, on-demand)"


@dataclass(frozen=True, slots=True)
class Price:
    input_per_1k: float
    output_per_1k: float


PRICES: dict[str, Price] = {
    "us.amazon.nova-lite-v1:0": Price(0.00006, 0.00024),
    "us.amazon.nova-pro-v1:0": Price(0.0008, 0.0032),
    "us.meta.llama4-maverick-17b-instruct-v1:0": Price(0.00024, 0.00097),
    "us.mistral.pixtral-large-2502-v1:0": Price(0.002, 0.006),
}


def cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    """Cost in USD, or None when the model is not in the table.

    None means unpriced, and the run report prints it as unpriced. It never means zero.
    """
    price = PRICES.get(model_id)
    if price is None:
        return None
    return (input_tokens / 1000) * price.input_per_1k + (
        output_tokens / 1000
    ) * price.output_per_1k
