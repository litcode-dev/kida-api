import re

# Store SKU convention: kida.loop.<slug>, kida.kit.<slug>, kida.drone.<slug>.
# SKUs are immutable once assigned and must never be deleted or reused —
# both stores treat the product ID as permanent.
SKU_PREFIXES = {
    "loop": "kida.loop",
    "kit": "kida.kit",
    "drone": "kida.drone",
}

# Both stores accept lowercase letters, digits, dots and underscores.
_SKU_RE = re.compile(r"^kida\.(loop|kit|drone)\.[a-z0-9_.]+$")


def _sku_slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return slug or "item"


def generate_sku(kind: str, title: str, uid: str) -> str:
    """Build a store SKU for a sellable item, e.g. kida.loop.amapiano_groove_1a2b3c4d.

    The item UUID prefix keeps SKUs unique across items with the same title.
    """
    if kind not in SKU_PREFIXES:
        raise ValueError(f"Unknown SKU kind: {kind}")
    return f"{SKU_PREFIXES[kind]}.{_sku_slug(title)}_{uid[:8].lower()}"


def is_valid_sku(sku: str) -> bool:
    return bool(_SKU_RE.match(sku))
