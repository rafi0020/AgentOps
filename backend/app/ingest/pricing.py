"""Model prices, per organisation, and the cost of one span. Costs are estimates from these prices."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MILLION = Decimal(1_000_000)
Prices = dict[str, tuple[Decimal, Decimal]]


async def load_prices(db: AsyncSession, org_id: UUID) -> Prices:
    rows = await db.execute(text("SELECT model, input_per_mtok, output_per_mtok FROM prices"))
    return {r.model: (r.input_per_mtok, r.output_per_mtok) for r in rows}


def cost_for(prices: Prices, model: str | None, input_tokens: int | None, output_tokens: int | None) -> Decimal | None:
    """None when the model has no price set: an unknown cost is not a cost of zero."""
    if not model or model not in prices or (input_tokens is None and output_tokens is None):
        return None
    price_in, price_out = prices[model]
    return ((input_tokens or 0) * price_in + (output_tokens or 0) * price_out) / MILLION
