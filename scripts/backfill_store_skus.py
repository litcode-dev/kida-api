"""
Backfill store SKUs (store_product_id) for existing paid sellable items.

Assigns the immutable SKU convention kida.loop.<slug> / kida.kit.<slug> /
kida.drone.<slug> to every paid loop, drum kit, and drone that doesn't already
have one. Items that already have a SKU are left untouched — SKUs are never
regenerated or reused. Does NOT set desired_price_usd or trigger a store sync;
run it once before the reconcile worker starts pushing prices.

Run: python -m scripts.backfill_store_skus
"""
import asyncio

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.drone_pad import Drone
from app.models.drum_kit import DrumKit
from app.models.loop import Loop
from app.services import store_sku

# (model, SKU kind) for each paid item type
TARGETS = [
    (Loop, "loop"),
    (DrumKit, "kit"),
    (Drone, "drone"),
]


async def backfill() -> dict:
    counts: dict[str, int] = {}
    async with AsyncSessionLocal() as db:
        for model, kind in TARGETS:
            result = await db.scalars(
                select(model).where(
                    model.is_free.is_(False),
                    model.store_product_id.is_(None),
                )
            )
            assigned = 0
            for item in result.all():
                item.store_product_id = store_sku.generate_sku(kind, item.title, str(item.id))
                assigned += 1
                print(f"  {kind}: {item.id} -> {item.store_product_id}")
            counts[kind] = assigned
        await db.commit()
    return counts


async def main() -> None:
    counts = await backfill()
    total = sum(counts.values())
    print(f"Backfilled {total} SKU(s): {counts}")


if __name__ == "__main__":
    asyncio.run(main())
