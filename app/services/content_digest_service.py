"""What goes into the daily new-content email, and how it is claimed.

The old scheme sent one email per item to every user and newsletter subscriber,
at upload time. That cost recipients × items, interrupted people once per
upload, and announced content before the processing job had finished — so a
failed encode still went out to the whole list.

This replaces it with a sweep. Once a day the digest asks "what is live and has
not been announced?", sends one email listing it, and stamps each item. Push
notifications still fire per item the moment it is ready, so nothing here slows
down the signal that something new exists; the email is the roundup.

Readiness differs by type. A loop, drone or drum kit is announceable as soon as
its audio is processed. A stem pack also needs its producer to publish it — a
breakdown pack sitting half-uploaded is not something to email anyone about.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.drone_pad import Drone
from app.models.drum_kit import DrumKit
from app.models.loop import Loop
from app.models.stem_pack import StemPack
from app.services import readiness

# Order the digest lists them in, and the label each group gets.
SECTION_LABELS = [
    ("loop", "Loops"),
    ("stem_pack", "Stem Packs"),
    ("drum_kit", "Drum Kits"),
    ("drone_pad", "Drone Pads"),
]

# How each type is counted in the push notification's one line: "3 loops and
# 1 drum kit". The email has room for the titles; the push has room for this.
SECTION_NOUNS = {
    "loop": ("loop", "loops"),
    "stem_pack": ("stem pack", "stem packs"),
    "drum_kit": ("drum kit", "drum kits"),
    "drone_pad": ("drone pad", "drone pads"),
}

# A single digest will not list more than this per type. Beyond it the email
# stops being readable, so the rest is summarised as a count.
MAX_ITEMS_PER_SECTION = 12


@dataclass
class DigestItem:
    id: uuid.UUID
    title: str
    subtitle: str = ""


@dataclass
class Digest:
    """One digest's worth of content, grouped by type."""

    items: dict[str, list[DigestItem]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.items.values())

    def is_empty(self) -> bool:
        return self.total == 0

    def sections(self) -> list[tuple[str, str, list[DigestItem]]]:
        """(content_type, label, items) for each non-empty group, in order."""
        return [
            (key, label, self.items[key])
            for key, label in SECTION_LABELS
            if self.items.get(key)
        ]


def headline(total: int) -> str:
    """What the digest is called wherever it is announced.

    The email subject and the push heading are the same sentence on purpose:
    somebody who gets both should recognise the second as the first, not read
    it as a second batch of content.
    """
    return "1 new drop on Kida" if total == 1 else f"{total} new drops on Kida"


def _join(parts: list[str]) -> str:
    if len(parts) < 2:
        return parts[0] if parts else ""
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def push_message(sections: list[tuple[str, str, list[DigestItem]]]) -> str:
    """The push's one line of body text.

    A single drop is named — that is the most useful thing one notification can
    say. Several are counted by type instead: a phone shows about two lines, so
    a list of twelve titles arrives as an ellipsis, while "3 loops and 2 drum
    kits" survives the truncation intact.
    """
    items = [item for _key, _label, group in sections for item in group]
    if len(items) == 1:
        return f'"{items[0].title}" just dropped. Open Kida to hear it.'

    counts = [
        f"{len(group)} {SECTION_NOUNS[key][0 if len(group) == 1 else 1]}"
        for key, _label, group in sections
    ]
    return f"{_join(counts)} just dropped. Open Kida to hear them."


async def _ready_loops(db: AsyncSession) -> list[Loop]:
    result = await db.scalars(
        select(Loop)
        .where(Loop.announced_at.is_(None), Loop.status == "ready")
        .order_by(Loop.created_at)
    )
    return list(result.all())


async def _ready_drum_kits(db: AsyncSession) -> list[DrumKit]:
    # Ready means every sample finished processing, and there is at least one.
    result = await db.scalars(
        select(DrumKit)
        .where(DrumKit.announced_at.is_(None), readiness.drum_kit_is_ready())
        .order_by(DrumKit.created_at)
    )
    return list(result.all())


async def _ready_drones(db: AsyncSession) -> list[Drone]:
    result = await db.scalars(
        select(Drone)
        .where(Drone.announced_at.is_(None), readiness.drone_is_ready())
        .order_by(Drone.created_at)
    )
    return list(result.all())


async def _ready_stem_packs(db: AsyncSession) -> list[StemPack]:
    result = await db.scalars(
        select(StemPack)
        .where(
            StemPack.announced_at.is_(None),
            StemPack.published_at.is_not(None),
            readiness.stem_pack_is_ready(),
        )
        .order_by(StemPack.created_at)
    )
    return list(result.all())


async def collect(db: AsyncSession) -> tuple[Digest, dict[str, list[uuid.UUID]]]:
    """Everything awaiting announcement, plus the ids to stamp once it is sent.

    The id lists cover every eligible item, including any trimmed from the
    email by MAX_ITEMS_PER_SECTION — those were still announced, just not
    named individually, and must not reappear tomorrow.
    """
    loops = await _ready_loops(db)
    packs = await _ready_stem_packs(db)
    kits = await _ready_drum_kits(db)
    drones = await _ready_drones(db)

    digest = Digest(items={
        "loop": [
            DigestItem(id=l.id, title=l.title, subtitle=f"{l.genre.value} · {l.bpm} BPM")
            for l in loops[:MAX_ITEMS_PER_SECTION]
        ],
        "stem_pack": [
            DigestItem(
                id=p.id,
                title=p.title,
                subtitle=f"{p.genre.value} · {p.bpm} BPM · {p.pack_type.value.replace('_', '-')}",
            )
            for p in packs[:MAX_ITEMS_PER_SECTION]
        ],
        "drum_kit": [
            DigestItem(id=k.id, title=k.title) for k in kits[:MAX_ITEMS_PER_SECTION]
        ],
        "drone_pad": [
            DigestItem(id=d.id, title=d.title) for d in drones[:MAX_ITEMS_PER_SECTION]
        ],
    })

    claim_ids = {
        "loop": [l.id for l in loops],
        "stem_pack": [p.id for p in packs],
        "drum_kit": [k.id for k in kits],
        "drone_pad": [d.id for d in drones],
    }
    return digest, claim_ids


_MODELS = {
    "loop": Loop,
    "stem_pack": StemPack,
    "drum_kit": DrumKit,
    "drone_pad": Drone,
}


async def claim(db: AsyncSession, claim_ids: dict[str, list[uuid.UUID]]) -> int:
    """Stamp announced_at, before a single email goes out.

    Deliberately ahead of sending. If the send half-fails, these items are not
    mentioned again — whereas stamping afterwards would let a crashed task
    re-send the entire digest to the entire list on the next run, which is the
    expensive mistake. The task logs the ids it claimed so a lost digest can be
    reconstructed by hand.
    """
    now = datetime.now(timezone.utc)
    claimed = 0
    for key, ids in claim_ids.items():
        if not ids:
            continue
        model = _MODELS[key]
        await db.execute(
            update(model)
            .where(model.id.in_(ids), model.announced_at.is_(None))
            .values(announced_at=now)
        )
        claimed += len(ids)
    await db.commit()
    return claimed


async def release(db: AsyncSession, claim_ids: dict[str, list[uuid.UUID]]) -> int:
    """Undo a claim: clear announced_at so the items return to the next digest.

    Stamping happens before the send, which is right — a crashed send must not
    re-blast the list. But when the send delivered nothing at all, the stamp is
    simply wrong, and without this the content would be marked announced and
    never mentioned again.
    """
    released = 0
    for key, ids in claim_ids.items():
        if not ids:
            continue
        model = _MODELS[key]
        result = await db.execute(
            update(model)
            .where(model.id.in_(ids), model.announced_at.is_not(None))
            .values(announced_at=None)
        )
        released += result.rowcount or 0
    await db.commit()
    return released
