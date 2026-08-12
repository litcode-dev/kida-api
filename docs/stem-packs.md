# Stem Packs

**Audience:** app and web clients consuming the API, and producers' tooling.
**Base URL:** `/api/v1` — every path below is relative to it.
**Auth:** every endpoint requires `Authorization: Bearer <access token>`.

---

## 1. What a stem pack is

A stem pack is a song delivered as separate instrument tracks. Every stem names
its instrument, and every pack is one of two kinds — decided when the pack is
created and fixed thereafter.

### Long-form

One continuous take per instrument. The whole song, one file each for drums,
piano, bass and so on. Buyers download the set and drop it into a session.

```
Sunday Morning (long_form)
├── drums.wav        3:32
├── piano.wav        3:32
├── bass_guitar.wav  3:32
└── vocal.wav        3:32
```

### Breakdown

The same song cut into its parts, with one stem per instrument inside each
part. Parts on their own are fragments; an **arrangement** names the order they
play in, and rendering it stitches each instrument's parts back into one
continuous file. That rendered set is what the buyer downloads.

```
Sunday Morning (breakdown)
├── Intro      ── drums, piano, bass_guitar, vocal, cue, metronome
├── Verse      ── drums, piano, guitar
├── Chorus     ── drums, piano, guitar
└── Verse 2    ── drums, piano, guitar

Arrangement "Full Song":  Intro → Verse → Chorus ×2 → Verse 2
   renders to →  drums.wav 3:32, piano.wav 3:32, guitar.wav 3:32
```

An instrument that sits a part out is padded with silence for exactly that
part's length, so every rendered track lines up.

---

## 2. Vocabulary

| Term | Meaning |
| --- | --- |
| **Pack** | The product. Carries title, genre, bpm, key, price, `pack_type`. |
| **Part** | One section of a breakdown pack — "Intro", "Verse 2". Breakdown only. |
| **Stem** | One audio file: one instrument, either the whole song (long-form) or one part of it (breakdown). |
| **Arrangement** | A running order over a pack's parts. Breakdown only. |
| **Track** | A rendered arrangement, one file per instrument. What buyers download. |

The hinge is `part_id` on a stem: `null` means the instrument's whole-song take,
set means that instrument during that one part.

---

## 3. Enumerations

**`pack_type`** — `long_form`, `breakdown`

**`instrument`** — `drums`, `piano`, `guitar`, `bass_guitar`, `vocal`, `cue`,
`metronome`, `others`

`cue` is the spoken guide track and `metronome` the click; both ship alongside
the musical parts so a live band can play to them.

**`section`** — `intro`, `verse`, `pre_chorus`, `chorus`, `hook`, `refrain`,
`bridge`, `vamp`, `interlude`, `instrumental`, `solo`, `outro`, `other`

**Stem `status`** — `processing` → `ready` | `failed`

**Arrangement `status`**

| Value | Meaning |
| --- | --- |
| `rendering` | Queued or in progress. No tracks to download yet. |
| `ready` | Rendered. `tracks` are downloadable. |
| `stale` | Was ready, but the audio underneath changed. Old tracks still download; re-render to refresh. |
| `failed` | Rendering gave up after retries. |
| `draft` | Reserved; arrangements are created straight into `rendering`. |

---

## 4. Producer workflow

### 4.1 Long-form pack

**Step 1 — create the pack.** `pack_type` defaults to `long_form`, so clients
that predate this feature keep working unchanged.

```http
POST /producer/stem-packs
Content-Type: application/json

{
  "title": "Midnight Drive",
  "genre": "Amapiano",
  "pack_type": "long_form",
  "bpm": 112,
  "key": "Am",
  "tags": ["amapiano", "night"],
  "price": "30.00",
  "description": "Full band take."
}
```

**Step 2 — upload every instrument in one call.** `instruments` is a
comma-separated list matching the order of `files`. Do **not** send `part_id`.

```http
POST /producer/stem-packs/{pack_id}/stems/bulk
Content-Type: multipart/form-data

files:       drums.wav, piano.wav, bass.wav, vox.wav
instruments: drums,piano,bass_guitar,vocal
labels:      Drums,Rhodes,Sub Bass,Lead Vox     ← optional
```

**Step 3 — poll until processed**, then announce it:

```http
GET  /producer/stem-packs/{pack_id}/status
POST /producer/stem-packs/{pack_id}/publish
```

That is the whole flow. Buyers use `GET /stem-packs/{id}/download`.

### 4.2 Breakdown pack

**Step 1 — create the pack** with `"pack_type": "breakdown"`.

```json
{
  "status": "success",
  "data": {
    "id": "440139ab-d503-47f0-94c8-86d774b88e91",
    "title": "Sunday Morning",
    "slug": "sunday-morning-440139ab",
    "loop_id": null,
    "genre": "Afrobeat Worship",
    "pack_type": "breakdown",
    "bpm": 104,
    "key": "Bb",
    "tags": ["worship", "live"],
    "price": "35.00",
    "description": "Full band, cut into parts.",
    "stems": [],
    "parts": [],
    "arrangements": [],
    "created_at": "2026-08-12T12:08:42.078811Z"
  },
  "message": "StemPack created"
}
```

**Step 2 — add the song parts**, one call each:

```http
POST /producer/stem-packs/{pack_id}/parts
{ "section": "verse", "bars": 8 }
```

`name` is optional. Left out, it is derived from the section and numbered when
the section repeats — `"Verse"`, then `"Verse 2"`. Names are unique within a
pack, because arrangements are read by name.

```json
{
  "status": "success",
  "data": {
    "id": "3648e141-b39f-46b6-853e-1d428e41cbad",
    "section": "verse",
    "name": "Verse 2",
    "position": 3,
    "bars": 8,
    "stems": [],
    "created_at": "2026-08-12T12:08:42.433350Z"
  },
  "message": "Part created"
}
```

`position` is display order as uploaded — **not** the order the parts play in.
That is what an arrangement decides.

**Step 3 — fill each part with instruments.** Same bulk endpoint, now with
`part_id`:

```http
POST /producer/stem-packs/{pack_id}/stems/bulk
Content-Type: multipart/form-data

files:       6 wav files
instruments: drums,piano,bass_guitar,vocal,cue,metronome
part_id:     76692dcf-147b-41f9-9045-6d50cfb0228c
```

```json
{
  "status": "success",
  "data": [
    {
      "id": "be291b00-8e9f-4c83-a955-a724bfcd7910",
      "instrument": "drums",
      "label": "Drums",
      "part_id": "76692dcf-147b-41f9-9045-6d50cfb0228c",
      "duration": 0,
      "status": "processing",
      "preview_url": null,
      "created_at": "2026-08-12T12:08:42.530066Z"
    }
  ],
  "message": "6 stem(s) upload queued"
}
```

`duration` is `0` and `preview_url` is `null` until processing finishes —
duration is measured from the file, never declared by the client.

**Step 4 — wait for processing.**

```http
GET /producer/stem-packs/{pack_id}/status
```

```json
{
  "status": "success",
  "data": {
    "id": "f09739ad-7c96-4d2b-8ee6-edaa221507b2",
    "pack_type": "breakdown",
    "status": "processing",
    "stems": [
      {
        "id": "be291b00-8e9f-4c83-a955-a724bfcd7910",
        "instrument": "drums",
        "label": "Drums",
        "part_id": "76692dcf-147b-41f9-9045-6d50cfb0228c",
        "status": "processing"
      }
    ]
  },
  "message": "OK"
}
```

The top-level `status` rolls the stems up: `empty` (nothing uploaded),
`processing`, `failed` (any stem failed), or `ready` (all ready).

**Step 5 — arrange.** A chorus played twice is one entry with
`repeat_count: 2`, so the running order reads the way a chart does.

```http
POST /producer/stem-packs/{pack_id}/arrangements
{
  "name": "Full Song",
  "description": "Sunday set order",
  "items": [
    { "part_id": "…Intro…" },
    { "part_id": "…Verse…" },
    { "part_id": "…Chorus…", "repeat_count": 2 },
    { "part_id": "…Verse 2…" }
  ]
}
```

```json
{
  "status": "success",
  "data": {
    "id": "7c0abab7-1121-42e5-9332-b3641f2441ad",
    "stem_pack_id": "440139ab-d503-47f0-94c8-86d774b88e91",
    "name": "Full Song",
    "description": "Sunday set order",
    "is_default": true,
    "status": "rendering",
    "duration": 0,
    "items": [
      { "id": "e5a066ef-…", "part_id": "73e96a30-…", "position": 0, "repeat_count": 1,
        "part_name": "Intro",   "section": "intro" },
      { "id": "a33a106c-…", "part_id": "b5dde792-…", "position": 1, "repeat_count": 1,
        "part_name": "Verse",   "section": "verse" },
      { "id": "352833f2-…", "part_id": "7f571c13-…", "position": 2, "repeat_count": 2,
        "part_name": "Chorus",  "section": "chorus" },
      { "id": "b3e86836-…", "part_id": "cf67f883-…", "position": 3, "repeat_count": 1,
        "part_name": "Verse 2", "section": "verse" }
    ],
    "tracks": [],
    "created_at": "2026-08-12T12:11:22.858021Z",
    "rendered_at": null
  },
  "message": "Arrangement created, render queued"
}
```

Each slot carries `part_name` and `section` so the running order can be shown
without a second lookup. `position` is assigned by the server from the order of
`items` — send them in playing order and ignore any position you might hold.

The first arrangement on a pack becomes its default whether or not
`is_default` was sent, so a pack always resolves to something. Setting
`is_default` on another clears the previous one.

**Step 6 — poll for the render**, via `GET .../arrangements` or the pack
detail. When `status` is `ready`, `tracks` is populated and `duration` is the
finished song's length.

### 4.3 Editing after the fact

| Intent | Call |
| --- | --- |
| Change the running order | `PUT .../arrangements/{id}` **with** `items` → rewrites the order and re-renders |
| Rename / re-describe / change default | `PUT .../arrangements/{id}` **without** `items` → no re-render |
| Refresh a `stale` arrangement | `POST .../arrangements/{id}/render` |
| Swap a stem's audio | `PATCH .../stems/{stem_id}` (multipart, `file`) |
| Remove a stem or a part | `DELETE .../stems/{id}` · `DELETE .../parts/{id}` |

Replacing a stem or deleting a part marks every `ready` arrangement on that
pack `stale`: the old tracks keep working, but they no longer match the pack
and want re-rendering.

`pack_type` cannot be changed after creation — switching it would orphan every
stem already uploaded under the old layout.

---

## 5. Buyer workflow

### Browse

```http
GET /stem-packs?pack_type=breakdown&genre=Afrobeat%20Worship&search=sunday&page=1&page_size=20
GET /stem-packs/{pack_id}
```

A breakdown pack returns its stems inside `parts` and leaves the top-level
`stems` empty; a long-form pack does the opposite. `preview_url` on each stem
is a 15-second MP3, playable without a purchase.

```json
{
  "id": "440139ab-d503-47f0-94c8-86d774b88e91",
  "pack_type": "breakdown",
  "stems": [],
  "parts": [
    {
      "id": "73e96a30-2c36-46de-848b-3eccd58c2503",
      "section": "intro",
      "name": "Intro",
      "position": 0,
      "bars": 8,
      "stems": [
        {
          "id": "9052e646-6003-4180-9668-276403b9a47d",
          "instrument": "drums",
          "label": "Drums",
          "part_id": "73e96a30-2c36-46de-848b-3eccd58c2503",
          "duration": 8,
          "status": "ready",
          "preview_url": "https://cdn.kida.audio/stems/previews/…_preview.mp3",
          "created_at": "2026-08-12T12:11:21.770482Z"
        }
      ],
      "created_at": "2026-08-12T12:11:21.658000Z"
    }
  ],
  "arrangements": [ … ]
}
```

### Download — the finished song

For a breakdown pack this is the endpoint you want:

```http
GET /stem-packs/{pack_id}/arrangements/{arrangement_id}/download
```

```json
{
  "status": "success",
  "data": {
    "pack_id": "594d1c06-2ce4-4751-9836-739957555f98",
    "arrangement_id": "5d0eb0d7-5d19-40e0-b474-94a58ccb9858",
    "name": "Full Song",
    "duration": 212,
    "tracks": [
      {
        "id": "a412dfa9-4dae-4b00-92f4-110db206cd26",
        "instrument": "drums",
        "signed_url": "https://cdn.kida.audio/stems/arrangements/encrypted/….wav.enc",
        "aes_key": "BASE64_KEY",
        "aes_iv": "BASE64_IV",
        "duration": 212,
        "expires_in_seconds": 900
      }
    ],
    "expires_in_seconds": 900
  },
  "message": "OK"
}
```

### Download — the raw stems

```http
GET /stem-packs/{pack_id}/download
```

Long-form packs return one entry per instrument. Breakdown packs return every
part's stems, each tagged with `part_id` and `part_name`, for buyers who want
to arrange the song themselves.

```json
{
  "pack_id": "0822d315-9e15-4391-b2ef-11b1efb58fc9",
  "title": "Sunday Morning",
  "pack_type": "breakdown",
  "stems": [
    {
      "id": "70791323-474d-4be0-854f-a7ee6761e609",
      "instrument": "drums",
      "label": "Drums",
      "signed_url": "https://cdn.kida.audio/stems/encrypted/….wav.enc",
      "aes_key": "BASE64_KEY",
      "aes_iv": "BASE64_IV",
      "duration": 8,
      "part_id": "7568cfaa-43af-4da1-af32-74a23303027d",
      "part_name": "Intro",
      "expires_in_seconds": 900
    }
  ],
  "expires_in_seconds": 900
}
```

Both download endpoints require a `Purchase` for the pack and return **403**
without one. Stems still processing are skipped; if nothing is ready the call
returns **409** rather than an empty list.

### Decrypting

Downloaded files are AES-256-GCM ciphertext, same scheme as loops and drum
kits. `aes_key` and `aes_iv` are base64. The **first 16 bytes of the payload
are the GCM tag**; the rest is the ciphertext.

```python
key = base64.b64decode(aes_key)
iv  = base64.b64decode(aes_iv)
tag, ciphertext = payload[:16], payload[16:]
wav = AES.new(key, AES.MODE_GCM, nonce=iv).decrypt_and_verify(ciphertext, tag)
```

Signed URLs last 15 minutes (`expires_in_seconds: 900`). Re-request the
endpoint for fresh ones; re-downloading is not charged again.

---

## 6. Endpoint reference

### Public — `/stem-packs`

| Method | Path | Rate | Notes |
| --- | --- | --- | --- |
| GET | `/stem-packs` | — | `search`, `genre`, `pack_type`, `tags`, `page`, `page_size` (≤100) |
| GET | `/stem-packs/{pack_id}` | — | Stems, parts, arrangements |
| GET | `/stem-packs/{pack_id}/arrangements` | — | With rendered tracks |
| GET | `/stem-packs/{pack_id}/download` | — | Purchase required |
| GET | `/stem-packs/{pack_id}/arrangements/{id}/download` | — | Purchase required |
| POST/DELETE | `/stem-packs/{pack_id}/like` | — | Unchanged |

### Management — `/producer/stem-packs` and `/admin/stem-packs`

Identical routes on both prefixes. Producers may only touch packs they created
(**403** otherwise); admins may touch any.

| Method | Path | Rate | Body |
| --- | --- | --- | --- |
| GET | `` | 60/min | `search`, `pack_type`, `tags`, `page`, `page_size` |
| POST | `` | 30/min | JSON — `title`, `genre`, `bpm`, `key`, `price` required |
| PUT | `/{pack_id}` | 20/min | JSON, all optional. No `pack_type`. |
| DELETE | `/{pack_id}` | 20/min | Removes stems, parts, arrangements and their files |
| GET | `/{pack_id}/status` | 60/min | Rolled-up upload state |
| POST | `/{pack_id}/parts` | 60/min | JSON — `section` required; `name`, `position`, `bars` optional |
| GET | `/{pack_id}/parts` | 60/min | |
| PUT | `/{pack_id}/parts/{part_id}` | 30/min | JSON, all optional |
| DELETE | `/{pack_id}/parts/{part_id}` | 20/min | Marks arrangements `stale` |
| POST | `/{pack_id}/stems` | 20/min | multipart — `file`, `instrument`; `label`, `part_id` optional |
| POST | `/{pack_id}/stems/bulk` | 10/min | multipart — `files`, `instruments`; `labels`, `part_id` optional |
| PATCH | `/{pack_id}/stems/{stem_id}` | 20/min | multipart — `file` |
| DELETE | `/{pack_id}/stems/{stem_id}` | 20/min | |
| POST | `/{pack_id}/publish` | 5/min | Requires status `ready` |
| POST | `/{pack_id}/arrangements` | 20/min | JSON — `name`, `items` required |
| GET | `/{pack_id}/arrangements` | 60/min | |
| PUT | `/{pack_id}/arrangements/{id}` | 20/min | JSON; `items` triggers a re-render |
| POST | `/{pack_id}/arrangements/{id}/render` | 10/min | Re-render, order unchanged |
| DELETE | `/{pack_id}/arrangements/{id}` | 20/min | Deletes rendered tracks too |

Every response uses the standard envelope:

```json
{ "status": "success", "data": { … }, "message": "…" }
{ "status": "error",   "data": null,  "message": "…" }
```

### Limits

| Thing | Limit |
| --- | --- |
| Upload size | 30 MB per file |
| Sample rate | 44 100 or 48 000 Hz, WAV |
| Stems per bulk call | 8 (one per instrument) |
| Parts per pack | 40 |
| Arrangements per pack | 20 |
| Entries per arrangement | 100 |
| Repeats per entry | 16 |
| Signed URL lifetime | 15 minutes |

---

## 7. Errors

| Status | When |
| --- | --- |
| 401 | Missing or invalid token |
| 403 | Producer editing a pack they do not own; downloading without a purchase |
| 404 | Unknown pack, part, stem or arrangement — e.g. `Arrangement … not found in pack …` |
| 409 | `… already has a stem in this part` · duplicate part or arrangement name · `… stem(s) … still processing` · nothing ready to download · publishing before processing finishes |
| 413 | File over 30 MB |
| 422 | Placement and shape problems — see below |

Common 422s, quoted as returned:

- `part_id is required for a breakdown pack — every stem belongs to a song part`
- `part_id is not accepted for a long-form pack — its stems are one continuous take per instrument`
- `Song parts belong to breakdown packs — a long-form pack holds one continuous stem per instrument`
- `Only breakdown packs are arranged — a long-form pack is already one continuous take per instrument`
- `Got 3 file(s) but 2 instrument(s); counts must match`
- `Unknown instrument in 'kazoo'. Allowed: drums, piano, guitar, bass_guitar, vocal, cue, metronome, others`
- `Part(s) not in this pack: …`
- `No stems uploaded yet for: Chorus, Verse 2`
- `Drums appears twice in this upload`

The last two are worth handling explicitly in a producer UI: an arrangement
referencing a part with no stems would silently vanish from the finished song,
so it is refused rather than rendered.

---

## 8. How it works behind the API

**Uploads are asynchronous.** A stem's WAV goes to S3 raw, and a Celery job
(`process_stem_upload`) encrypts it, cuts a 15-second MP3 preview, measures the
duration and flips the row to `ready`. This is the same pipeline loops and drum
samples use.

**Rendering is asynchronous.** `render_stem_arrangement` walks the running
order once per instrument:

1. Expand `repeat_count` into one entry per play-through.
2. Measure each part's exact duration from one of its stems — whole-second
   values would drift a beat over a long song.
3. Fetch and decrypt that instrument's parts, substituting silence of the
   part's length wherever the instrument does not play.
4. Concatenate with ffmpeg's concat *filter*, which resamples segments that do
   not share a sample rate, since parts are independent uploads.
5. Encrypt, upload, cut a preview, record the track.

Only one instrument's audio is held in memory at a time.

An arrangement will not render while any referenced stem is still processing —
its encrypted file does not exist yet, so the instrument would drop out of the
finished track without any error.

**Storage layout**

```
stems/raw/{stem_id}.wav                             deleted once processed
stems/encrypted/{stem_id}.wav.enc
stems/previews/{stem_id}_preview.mp3
stems/arrangements/encrypted/{track_id}.wav.enc
stems/arrangements/previews/{track_id}_preview.mp3
```

---

## 9. Migrating existing integrations

Packs created before this feature are `long_form`, their stems are `others`,
and stems that already had audio are `ready`. Nothing needs re-uploading.

Two changes producer clients must make:

1. **`POST .../stems` takes `instrument` instead of `duration`.** Duration is
   measured from the file. `label` is optional and defaults to the instrument's
   display name.
2. **`PUT .../stem-packs/{id}` no longer accepts `pack_type`.** It is fixed at
   creation.

Buyer-side clients keep working: `GET /stem-packs/{id}/download` still returns a
list of stems with `signed_url`, `aes_key` and `aes_iv`. The response gained
`pack_type`, and each stem gained `instrument`, `part_id` and `part_name`.

---

## See also

- `docs/superpowers/plans/2026-08-12-stem-pack-instruments-and-arrangements.md`
  — design notes and the data model
- `/docs` — live OpenAPI reference for exact schemas
