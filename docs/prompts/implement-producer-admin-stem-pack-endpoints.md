# Implementation prompt — producer/admin stem pack endpoints

Paste everything below the line into a fresh coding session. It is written to be
self-contained: it states the domain, the data model, every endpoint's contract,
the rules that are easy to get wrong, and how to verify the result.

---

## Task

Implement the **producer and admin management surface for stem packs** in this
FastAPI + SQLAlchemy(async) + Alembic + Celery codebase.

A stem pack is a song delivered as separate instrument tracks. Today a pack is
an unstructured bag of labelled audio files. Give it a shape:

1. **Every stem names its instrument** — `drums`, `piano`, `guitar`,
   `bass_guitar`, `vocal`, `cue`, `metronome`, `others`. (`cue` is the spoken
   guide track, `metronome` the click; both ship so a live band can play along.)
2. **A pack is one of two kinds**, chosen at creation and fixed afterwards:
   - `long_form` — one continuous take per instrument. The whole song, one file
     each.
   - `breakdown` — the song cut into **parts** (intro, verse, chorus, bridge,
     vamp, interlude, outro…), each part holding one stem per instrument.
3. **Breakdown packs are arranged before they are useful.** An **arrangement**
   is a running order over the pack's parts, with repeat counts — intro, verse,
   chorus ×2, outro. **Rendering** an arrangement stitches each instrument's
   parts into one continuous file; that rendered set is what a buyer downloads.

## Data model

Add to the existing `stem_packs` / `stems` tables and create four more.

```
stem_packs        + pack_type            long_form | breakdown, default long_form, indexed

stem_parts          id, stem_pack_id → stem_packs ON DELETE CASCADE,
                    section (enum), name varchar(100), position int, bars int null,
                    created_at
                    UNIQUE (stem_pack_id, name)          -- arrangements read by name
                    INDEX (stem_pack_id, position)

stems             + instrument           enum, NOT NULL, default 'others', indexed
                  + part_id              → stem_parts ON DELETE CASCADE, NULL for long-form
                  + status               varchar(20) default 'processing'
                    duration gets a default of 0 (it is measured, not declared)
                    UNIQUE (part_id, instrument)
                    UNIQUE INDEX (stem_pack_id, instrument) WHERE part_id IS NULL

stem_arrangements   id, stem_pack_id → CASCADE, name varchar(150), description text,
                    is_default bool default false, status varchar(20) default 'draft',
                    duration int default 0, created_by → users, created_at, rendered_at
                    UNIQUE (stem_pack_id, name)

stem_arrangement_items
                    id, arrangement_id → CASCADE, part_id → stem_parts CASCADE,
                    position int, repeat_count int default 1, created_at
                    UNIQUE (arrangement_id, position)

stem_arrangement_tracks
                    id, arrangement_id → CASCADE, instrument (enum),
                    file_s3_key, preview_s3_key, aes_key, aes_iv,
                    duration int default 0, status varchar(20) default 'rendering',
                    created_at
                    UNIQUE (arrangement_id, instrument)
```

`part_id` is the hinge: `NULL` means the stem is an instrument's whole-song
take, set means it is that instrument during that one part.

**Enums** (Postgres enum types, storing the member *values*):

- `stem_pack_type`: `long_form`, `breakdown`
- `stem_instrument`: `drums`, `piano`, `guitar`, `bass_guitar`, `vocal`, `cue`,
  `metronome`, `others`
- `song_section`: `intro`, `verse`, `pre_chorus`, `chorus`, `hook`, `refrain`,
  `bridge`, `vamp`, `interlude`, `instrumental`, `solo`, `outro`, `other`

Statuses stay plain `varchar(20)`, matching how loops and drum samples do it —
no new enum types to ALTER later.

- stem: `processing` → `ready` | `failed`
- arrangement: `rendering` → `ready`, plus `stale` and `failed`

## Endpoints

All of these exist **twice** — under `/producer/stem-packs` and
`/admin/stem-packs`. Do not write them twice. Build them once in a factory
(`build_stem_pack_router(actor_dependency, enforce_ownership: bool)`) and mount
it from both routers, with ownership enforced for producers (403 when
`pack.created_by != actor.id`) and not for admins.

| Method | Path | Rate | Body |
| --- | --- | --- | --- |
| GET | `` | 60/min | `search`, `pack_type`, `tags`, `page`, `page_size`; producers see only their own |
| POST | `` | 30/min | JSON — `title`, `genre`, `bpm`, `key`, `price` required; `pack_type` defaults to `long_form` |
| PUT | `/{pack_id}` | 20/min | JSON, all optional. **Must not accept `pack_type`.** |
| DELETE | `/{pack_id}` | 20/min | Cascades to stems, parts, arrangements, tracks; deletes their S3 objects |
| GET | `/{pack_id}/status` | 60/min | Per-stem state + one rolled-up verdict |
| POST | `/{pack_id}/parts` | 60/min | JSON — `section` required; `name`, `position`, `bars` optional |
| GET | `/{pack_id}/parts` | 60/min | — |
| PUT | `/{pack_id}/parts/{part_id}` | 30/min | JSON, all optional |
| DELETE | `/{pack_id}/parts/{part_id}` | 20/min | Also deletes its stems and arrangement slots |
| POST | `/{pack_id}/stems` | 20/min | multipart — `file`, `instrument`; `label`, `part_id` optional |
| POST | `/{pack_id}/stems/bulk` | 10/min | multipart — `files`, `instruments` (csv); `labels` (csv), `part_id` optional |
| PATCH | `/{pack_id}/stems/{stem_id}` | 20/min | multipart — `file`; replaces audio, keeps instrument and part |
| DELETE | `/{pack_id}/stems/{stem_id}` | 20/min | — |
| POST | `/{pack_id}/publish` | 5/min | Announce by email; requires rolled-up status `ready` |
| POST | `/{pack_id}/arrangements` | 20/min | JSON — `name`, `items: [{part_id, repeat_count}]` |
| GET | `/{pack_id}/arrangements` | 60/min | — |
| PUT | `/{pack_id}/arrangements/{id}` | 20/min | JSON; sending `items` rewrites the order and re-renders |
| POST | `/{pack_id}/arrangements/{id}/render` | 10/min | Re-render the same order |
| DELETE | `/{pack_id}/arrangements/{id}` | 20/min | Deletes rendered tracks and their S3 objects |

Responses use the project's existing envelope, `{ status, data, message }`, and
return **200** — not 201 — because that is what the rest of this API does.

## Rules that are easy to get wrong

**Placement.** A breakdown pack's stem *requires* `part_id`, and the part must
belong to that pack. A long-form pack's stem *rejects* `part_id`. Both are 422
with a message that explains the distinction, not just "invalid".

**One stem per instrument per slot.** Adding a second `drums` to the same part
(or to the same long-form pack) is a 409 telling the caller to replace the
audio instead. Check it in the service as well as the constraint, so the error
is legible.

**Duration is measured, never declared.** The old endpoint took a `duration`
form field; delete it. Upload the raw WAV to S3, create the row as
`processing`, and let a Celery job (mirroring the existing
`process_drum_sample_upload`) encrypt it, cut a 15-second MP3 preview, read the
duration with `soundfile`, flip the row to `ready` and delete the raw object.

**Part names.** Optional on input. Left out, derive from the section and number
repeats: `"Verse"`, then `"Verse 2"`. Unique within a pack — arrangements are
read by name, so a clash is a 409.

**`position` on a part is display order as uploaded, not playing order.** Only
an arrangement decides what plays when.

**Arrangements only exist for breakdown packs.** Arranging a long-form pack is
422.

**Refuse to render what would come out wrong.** Two checks before queuing:
- any referenced part with *no stems at all* → 422 naming the parts. It would
  contribute no audio and silently vanish from the song.
- any referenced stem not yet `ready` → 409. Its encrypted file does not exist,
  so that instrument would drop out of the finished track without any error.

**Defaults.** The first arrangement on a pack becomes `is_default` whether or
not it was asked for, so a pack always resolves to something. Setting
`is_default` on another clears the previous one. Deleting the default promotes
the oldest survivor.

**Staleness.** Replacing a stem's audio or deleting a part marks every `ready`
arrangement on the pack `stale`. Old tracks stay downloadable — they are still
valid audio for what they were rendered from — but they no longer match.

**Session semantics.** This project uses `expire_on_commit=False`. Attach child
rows through the relationship (`arrangement.items = [...]`), not as loose
`db.add()` rows with a foreign key — the session keeps the collection it already
has for a new object, so rows inserted behind its back never appear in the
response. For the same reason, never "hide" a loaded collection by assigning
`[]` to it when the relationship has `delete-orphan`: the session reads that as
orphaning and the next autoflush deletes the rows. Do that kind of shaping in
the response schema instead.

**Eager loading.** Anything a response touches must be loaded in the query.
Arrangement items need their `part` (`selectinload(...).selectinload(...)`),
or the running order comes back with null names.

## Rendering job

A Celery task, `render_stem_arrangement(arrangement_id)`:

1. Flatten the running order — expand `repeat_count` into one entry per
   play-through, ordered by `position`.
2. Measure each distinct part's **exact** duration from one of its stems.
   Whole-second `duration` columns are too coarse; a tenth of a second lost per
   part walks the click track off the beat by the end of a song.
3. For each instrument present in the pack, walk the flattened order: use that
   instrument's stem for the part when it exists, otherwise insert **silence of
   that part's exact length** — an instrument that sits a part out still has to
   occupy its time or every later part in that track lands early.
4. Concatenate with ffmpeg's concat **filter** (`-filter_complex ... concat=n=N:v=0:a=1`),
   not the concat demuxer: parts are independent uploads and may differ in
   sample rate or channel count, which the demuxer refuses and the filter
   resamples.
5. Encrypt (AES-256-GCM, per-file key + IV, tag prepended — same as the rest of
   this codebase), upload, cut a preview, upsert the track row, mark it `ready`.
6. When every instrument is done set the arrangement `ready`, record its
   duration and `rendered_at`. Delete tracks for instruments that no longer
   exist in the pack. On repeated failure, mark it `failed`.

Hold only one instrument's audio in memory at a time.

Add S3 key helpers alongside the existing ones:

```
stems/raw/{stem_id}.wav
stems/encrypted/{stem_id}.wav.enc
stems/previews/{stem_id}_preview.mp3
stems/arrangements/encrypted/{track_id}.wav.enc
stems/arrangements/previews/{track_id}_preview.mp3
```

## Migration

One Alembic revision, chained to the current head, following this repo's
`op.execute(sa.text(...))` + `IF NOT EXISTS` style.

Existing rows must survive untouched: packs become `long_form`, stems become
`others`, and stems that already have a `file_s3_key` are marked `ready` rather
than `processing`. Nothing is re-uploaded.

One trap: every legacy stem lands on `(pack, others, NULL)`, so a database with
two or more stems in one pack cannot take the long-form partial unique index.
Create it inside a `DO $$ … EXCEPTION WHEN unique_violation THEN RAISE NOTICE …`
block so the migration skips it instead of failing; the service-level duplicate
check still holds the line. Write a working `downgrade()`.

## Housekeeping

- Account deletion (`auth_service.delete_user`) must clear the new tables and
  collect the new S3 keys — including raw uploads for stems stuck mid-processing
  and rendered arrangement tracks. Arrangements carry their own `created_by`, so
  one created on someone else's pack will otherwise block the users delete.
- Add a `"stem_pack"` label to the push-notification type map.
- Register the new task module in the Celery `include` list.
- Export the new models from `app/models/__init__.py`.

## Definition of done

- The full existing test suite passes untouched.
- New tests cover, at minimum: `pack_type` defaulting to `long_form`; part-name
  defaulting and its 409 on clash; breakdown-requires-part and
  long-form-rejects-part; duplicate instrument 409; bulk count mismatch and
  unknown instrument 422; producer 403 on another producer's pack while admin
  succeeds; arrangement refused for a part with no stems and for stems still
  processing; arrangement create returning items in order with names and repeat
  counts; reordering re-rendering while a rename does not; stale marking on stem
  replace and part delete; and the download paths.
- One end-to-end test of the renderer with S3 faked in memory: a two-part pack
  where an instrument plays in only one part must produce tracks of equal
  length for every instrument. That is the test that proves silence padding
  works — everything else can pass while the audio is wrong.
- `alembic upgrade head` and `downgrade` both run clean against a real
  Postgres, and the upgrade is also exercised against seeded legacy rows.
