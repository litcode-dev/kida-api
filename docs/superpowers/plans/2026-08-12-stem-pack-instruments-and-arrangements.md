# Stem packs: instruments, long-form vs breakdown, and arrangements

## What changed

A stem pack used to be a bag of labelled audio files. It now has a shape.

**Every stem names its instrument** — `drums`, `piano`, `guitar`, `bass_guitar`,
`vocal`, `cue`, `metronome`, `others`. `cue` is the spoken guide track and
`metronome` the click, so a live band can play to the pack.

**A pack is one of two kinds**, chosen at creation and fixed thereafter:

- `long_form` — one continuous take per instrument. The whole song, eight files.
- `breakdown` — the song cut into parts (intro, verse, chorus, bridge, vamp,
  interlude, outro…), each part holding one stem per instrument.

**Breakdown packs are arranged before they are useful.** An arrangement is a
running order — intro, verse, chorus, chorus, bridge, outro — written as a list
of parts with repeat counts. Rendering it stitches each instrument's parts into
one continuous file, and that rendered set is what a buyer downloads.

## Data model

```
stem_packs        + pack_type (long_form | breakdown)
stem_parts          one row per song section; unique name within the pack
stems             + instrument, part_id (NULL = long-form), status
stem_arrangements   a named running order, with a status and a default flag
stem_arrangement_items    part + position + repeat_count
stem_arrangement_tracks   the rendered output, one row per instrument
```

`part_id` is the hinge: NULL means the stem is an instrument's whole-song take,
set means it is that instrument during one part. One stem per instrument per
part is enforced by a unique constraint, and one per instrument per pack for
long-form stems by a partial unique index on `part_id IS NULL`.

Arrangement tracks carry the same fields as stems — encrypted key, preview key,
AES key/IV — so clients consume them through exactly the same download path.

## Rendering

`app/tasks/render_tasks.render_stem_arrangement` walks the running order once
per instrument:

1. Flatten the items to one part per play-through (`repeat_count` expanded).
2. Measure each part's exact duration from one of its stems. Whole-second
   durations would drift a beat over a long song.
3. For each instrument, fetch and decrypt that instrument's parts, substituting
   silence of the part's length wherever the instrument sits out — otherwise
   every later part in that track lands early.
4. Concatenate with ffmpeg's concat *filter*, which resamples segments that do
   not share a sample rate (parts are independent uploads).
5. Encrypt, upload, cut a preview, and record the track.

Only one instrument's audio is held in memory at a time.

## Lifecycle

Stems are uploaded raw and processed in the background, exactly like drum
samples: `processing` → `ready` | `failed`. An arrangement may only be rendered
once every stem it references is `ready`, or instruments would silently vanish
from the finished song.

Arrangements go `rendering` → `ready`, and a ready arrangement is marked
`stale` when the audio underneath it changes — a stem replaced, a part deleted.
Stale keeps the old tracks downloadable while flagging that they no longer match
the pack; `POST .../render` produces fresh ones.

## Endpoints

Public (`/stem-packs`):

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/stem-packs` | List, filterable by `pack_type`, genre, tags, search |
| GET | `/stem-packs/{id}` | Detail: stems, parts, arrangements |
| GET | `/stem-packs/{id}/arrangements` | Running orders and rendered tracks |
| GET | `/stem-packs/{id}/download` | Every ready stem, tagged with its part |
| GET | `/stem-packs/{id}/arrangements/{id}/download` | The stitched song |

Management, mounted twice from `app/routers/stem_pack_management.py` — under
`/producer` (own packs only) and `/admin` (any pack): pack CRUD, `/status`,
parts CRUD, `/stems`, `/stems/bulk`, stem replace/delete, `/publish`, and
arrangement create/list/update/render/delete.

## Compatibility

Existing packs become `long_form` and their stems `others`; stems that already
have audio are marked `ready`. Nothing is re-uploaded. `POST /stem-packs` still
works unchanged for clients that never send `pack_type`.

Two breaking changes for producer clients: uploading a stem now takes
`instrument` instead of `duration` (duration is measured from the file), and
the pack's `PUT` body no longer accepts `pack_type` — switching a pack's kind
would orphan everything already uploaded under the old layout.

The legacy stem rows all land on `(pack, others, NULL)`, so a database with
several stems in one pack cannot take the long-form partial unique index; the
migration notices and skips it rather than failing. The service-level check
still refuses duplicate instruments in that case.
