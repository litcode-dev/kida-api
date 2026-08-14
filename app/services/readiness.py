"""What "ready" means for each kind of content, in one place.

A loop carries its own status column, but a drum kit, drone or stem pack is
only as ready as its children: the parent row exists from the moment of upload
while its samples, pads or stems are still being encrypted. Two rules apply to
all three — there must be at least one child, and none of them may be
unfinished. A parent with no children is a half-made upload, not a release.

These predicates decide both what the public catalogue lists and what the daily
digest announces, and those answers must agree: announcing a kit that the
catalogue then hides would send people to a dead link.
"""
from sqlalchemy import and_, select

from app.models.drone_pad import Drone, DronePad
from app.models.drum_kit import DrumKit, DrumSample
from app.models.stem_pack import Stem, StemPack


def _all_children_ready(child, parent_fk, parent_id):
    """At least one child, and no child left unfinished."""
    return and_(
        select(child.id).where(parent_fk == parent_id).exists(),
        ~select(child.id)
        .where(parent_fk == parent_id, child.status != "ready")
        .exists(),
    )


def drum_kit_is_ready():
    return _all_children_ready(DrumSample, DrumSample.drum_kit_id, DrumKit.id)


def drone_is_ready():
    return _all_children_ready(DronePad, DronePad.drone_id, Drone.id)


def stem_pack_is_ready():
    return _all_children_ready(Stem, Stem.stem_pack_id, StemPack.id)
