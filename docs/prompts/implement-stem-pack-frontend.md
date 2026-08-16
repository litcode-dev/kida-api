# Implementation prompt — producer/admin stem pack UI (Next.js)

Paste everything below into a session working in the frontend repo. It carries
the domain, the full API contract, the screens to build, and the traps that will
otherwise cost a day.

---

## Task

Build the **producer and admin management UI for stem packs** against the Kida
API. Producers manage packs they created; admins manage anyone's. The two
surfaces are the same screens against two path prefixes, so build one set of
components parameterised by scope — not two copies.

## Domain — read this before designing anything

A stem pack is a song delivered as separate instrument tracks. Every stem names
its instrument: `drums`, `piano`, `guitar`, `bass_guitar`, `vocal`, `cue`,
`metronome`, `others`. (`cue` is the spoken count-in, `metronome` the click —
both ship so a live band can play to them.)

A pack is one of two kinds, chosen at creation and **fixed forever after**:

**`long_form`** — one continuous take per instrument. The whole song, one file
each. Create the pack, upload the instruments, done.

**`breakdown`** — the song cut into **parts** (intro, verse, chorus, bridge,
vamp, interlude, outro…), each part holding one stem per instrument. Parts
alone are fragments. An **arrangement** names the order they play in — intro,
verse, chorus ×2, outro — and the server **renders** it by stitching each
instrument's parts into one continuous file. That rendered set is what buyers
download.

The mental model to put on screen is a grid: parts across, instruments down.
Not every instrument plays in every part — the guitar sits out the intro, the
cue track only counts the band in. An arrangement reads across that grid in
playing order, and the renderer pads absent instruments with silence so every
track stays in sync.

Two consequences that drive the whole UI:

- A breakdown pack is **not shippable until every part has stems and every stem
  has finished processing**. The UI's job is making the gaps visible.
- Rendering is asynchronous and can go **stale** when the audio underneath
  changes. The UI must show that state and offer the re-render.

## API contract

- Base URL `/api/v1`. Every request carries `Authorization: Bearer <token>`.
- Producer prefix `/producer/stem-packs`, admin prefix `/admin/stem-packs` —
  identical routes and payloads.
- Every response is the envelope `{ status, data, message }`, and creates return
  **200**, not 201. Errors are `{ status: "error", data: null, message: "..." }`.
- Public read endpoints (`/stem-packs/...`) exist too and are what the buyer app
  uses; the management screens should use the producer/admin prefix so ownership
  is enforced server-side.

### Types

```ts
type PackType = "long_form" | "breakdown";

type Instrument =
  | "drums" | "piano" | "guitar" | "bass_guitar"
  | "vocal" | "cue" | "metronome" | "others";

type Section =
  | "intro" | "verse" | "pre_chorus" | "chorus" | "hook" | "refrain"
  | "bridge" | "vamp" | "interlude" | "instrumental" | "solo" | "outro" | "other";

// Genre is the one enum whose wire value is NOT the same as its internal name:
// send "Afrobeat Worship", not afrobeat_worship. Spaces, hyphen, ampersand and
// capitalisation exactly as below.
type Genre =
  | "Afrobeat" | "Amapiano" | "Trap" | "Boom Bap" | "Lo-fi" | "Gospel"
  | "Afrobeat Worship" | "Contemporary Worship" | "Dancehall" | "Afrohouse"
  | "Highlife Gospel" | "African Praise" | "Drill" | "Seben" | "Reggae"
  | "Highlife" | "Soukous" | "Rumba" | "Afro Pop" | "Hip Hop" | "R&B"
  | "Kompa" | "Fuji" | "Jazz" | "Blues" | "Country"
  | "Hausa Groove" | "Ariaria";

type StemStatus = "processing" | "ready" | "failed";
type ArrangementStatus = "rendering" | "ready" | "stale" | "failed" | "draft";
type PackUploadStatus = "empty" | "processing" | "ready" | "failed";

interface Envelope<T> { status: "success" | "error"; data: T | null; message: string }

interface Stem {
  id: string;
  instrument: Instrument;
  label: string;              // "Rhythm Gtr" — defaults to the instrument's display name
  part_id: string | null;     // null on long-form packs
  duration: number;           // seconds; 0 until processed
  status: StemStatus;
  preview_url: string | null; // 15s mp3; null until processed
  created_at: string;
}

interface Part {
  id: string;
  section: Section;
  name: string;               // "Verse", "Verse 2" — unique within the pack
  position: number;           // display order as uploaded, NOT playing order
  bars: number | null;
  stems: Stem[];
  created_at: string;
}

interface ArrangementItem {
  id: string;
  part_id: string;
  position: number;           // assigned by the server from array order
  repeat_count: number;       // 1..16
  part_name: string | null;   // server-filled, use it for display
  section: Section | null;
}

interface ArrangementTrack {
  id: string;
  instrument: Instrument;
  duration: number;
  status: string;
  preview_url: string | null;
}

interface Arrangement {
  id: string;
  stem_pack_id: string;
  name: string;
  description: string | null;
  is_default: boolean;
  status: ArrangementStatus;
  duration: number;           // rendered length in seconds
  items: ArrangementItem[];
  tracks: ArrangementTrack[];
  created_at: string;
  rendered_at: string | null;
}

interface StemPack {
  id: string;
  title: string;
  slug: string;
  loop_id: string | null;
  genre: Genre;
  pack_type: PackType;
  bpm: number;
  key: string;
  tags: string[];
  price: string;              // decimal as a string — never parse to float for display
  description: string | null;
  stems: Stem[];              // populated for long_form; empty for breakdown
  parts: Part[];              // populated for breakdown
  arrangements: Arrangement[];
  created_at: string;
}

interface UploadStatus {
  id: string;
  pack_type: PackType;
  status: PackUploadStatus;   // rolled up from the stems
  stems: Array<{
    id: string; instrument: Instrument; label: string;
    part_id: string | null; status: StemStatus;
  }>;
}
```

### Endpoints

`{scope}` is `producer` or `admin`.

| Method | Path | Rate | Notes |
| --- | --- | --- | --- |
| GET | `/{scope}/stem-packs` | 60/min | `search`, `pack_type`, `tags` (csv), `page`, `page_size` |
| POST | `/{scope}/stem-packs` | 30/min | JSON; `title`, `genre`, `bpm`, `key`, `price` required |
| PUT | `/{scope}/stem-packs/{id}` | 20/min | JSON, all optional. **`pack_type` is not accepted.** |
| DELETE | `/{scope}/stem-packs/{id}` | 20/min | Destroys everything under it |
| GET | `/{scope}/stem-packs/{id}/status` | 60/min | The polling endpoint |
| POST | `/{scope}/stem-packs/{id}/parts` | 60/min | `{ section, name?, position?, bars? }` |
| GET | `/{scope}/stem-packs/{id}/parts` | 60/min | |
| PUT | `/{scope}/stem-packs/{id}/parts/{partId}` | 30/min | |
| DELETE | `/{scope}/stem-packs/{id}/parts/{partId}` | 20/min | Marks arrangements stale |
| POST | `/{scope}/stem-packs/{id}/stems` | 20/min | multipart: `file`, `instrument`, `label?`, `part_id?` |
| POST | `/{scope}/stem-packs/{id}/stems/bulk` | 10/min | multipart: `files[]`, `instruments` (csv), `labels?` (csv), `part_id?` |
| PATCH | `/{scope}/stem-packs/{id}/stems/{stemId}` | 20/min | multipart: `file` — replaces audio |
| DELETE | `/{scope}/stem-packs/{id}/stems/{stemId}` | 20/min | |
| POST | `/{scope}/stem-packs/{id}/publish` | 5/min | Emails subscribers; requires status `ready` |
| POST | `/{scope}/stem-packs/{id}/arrangements` | 20/min | `{ name, description?, is_default?, items:[{part_id, repeat_count?}] }` |
| GET | `/{scope}/stem-packs/{id}/arrangements` | 60/min | |
| PUT | `/{scope}/stem-packs/{id}/arrangements/{arrId}` | 20/min | Sending `items` re-renders; omitting it does not |
| POST | `/{scope}/stem-packs/{id}/arrangements/{arrId}/render` | 10/min | Re-render, order unchanged |
| DELETE | `/{scope}/stem-packs/{id}/arrangements/{arrId}` | 20/min | |

### Server-side limits the UI must respect

| Thing | Limit |
| --- | --- |
| File | WAV, 44 100 or 48 000 Hz, ≤ 30 MB |
| Stems per bulk call | 8 (one per instrument) |
| Parts per pack | 40 |
| Arrangements per pack | 20 |
| Entries per arrangement | 100 |
| `repeat_count` | 1–16 |

## Screens

### 1. Pack list — `/{scope}/stem-packs`

Table or card grid with search, `pack_type` filter, tag filter, pagination.
Each row shows title, kind, bpm/key, price, and a **readiness indicator** —
derive it from the pack payload: number of parts, number of stems, whether any
arrangement is `ready`. Kind is a permanent property, so label it clearly
(a chip reading `long-form` / `breakdown`), not buried in a detail view.

Primary action: **New pack**.

### 2. Create pack

A form, but the `pack_type` choice is the important moment: it is irreversible
and changes every screen that follows. Present it as two explained options
(short description of each, as in the domain section above), not a bare select.
Everything else is `title`, `genre`, `bpm`, `key`, `price`, `tags`,
`description`, optional `loop_id`.

On success route to the pack detail screen for that kind.

### 3. Pack detail — long-form

- Header: metadata, edit, delete, publish.
- **Instruments panel**: one row per uploaded stem — instrument, label, duration,
  status, an audio preview player when `preview_url` exists, replace and delete
  actions.
- **Upload panel**: a multi-file drop zone. The user picks files and assigns an
  instrument to each; submit as one `stems/bulk` call. Each instrument may
  appear only once in the pack — filter already-used instruments out of the
  picker and disable the submit if a duplicate remains.

### 4. Pack detail — breakdown

This is the screen that matters. Three regions:

**a. Parts list.** Ordered by `position`. Each part shows its section, name,
bars, and how many of its instruments are uploaded. Add / rename / delete.
`name` is optional on create — tell the user it will be derived ("Verse", then
"Verse 2") rather than making them invent one. Names are unique within a pack;
surface a clash as a field error, not a toast.

**b. Coverage grid.** Parts across the top, instruments down the side, a filled
cell where a stem exists and an outlined cell where it does not. This is the
single most useful component on the screen — it makes "the chorus has no bass"
visible at a glance, and it mirrors how the renderer thinks. Cells carry state:
filled/ready, filled/processing (pulsing), filled/failed (error), empty. Clicking
an empty cell opens the upload flow pre-filled with that part and instrument;
clicking a filled cell offers preview / replace / delete.

**c. Arrangements panel.** See the builder spec below.

### 5. Upload flow

Two entry points — one file into one slot, or a whole part at once. The bulk
call is the normal path: the user drops 6 files for the chorus and assigns
instruments.

- Validate **before** uploading: extension/MIME is WAV, size ≤ 30 MB. Sample
  rate can only be checked by decoding client-side (`AudioContext.decodeAudioData`)
  — worth doing, since a 22 kHz file is otherwise a wasted 30 MB round trip and a
  422 at the end.
- `instruments` must have exactly as many entries as `files`, in the same order.
  Same for `labels` if sent. Enforce it in the form; the server 422s otherwise.
- Show per-file progress. The response returns rows with `status: "processing"`,
  `duration: 0`, `preview_url: null` — render them immediately in that state
  rather than waiting.
- After a successful upload, start polling (below).

### 6. Status polling

`GET /{scope}/stem-packs/{id}/status` returns per-stem state plus a rolled-up
`status`. Poll while anything is `processing`:

- interval ~3 s, backing off to ~10 s after a minute; stop at `ready` or
  `failed`, and stop when the tab is hidden (`visibilitychange`).
- the endpoint allows 60/min — never poll faster than 1 s, and share one poller
  per pack rather than one per stem row.
- on `failed`, show which stems failed and offer replace-audio on those rows.

Arrangements need the same treatment while any is `rendering` — poll
`GET .../arrangements` and stop when none are `rendering`.

### 7. Arrangement builder

The centrepiece. Two panes:

**Left — available parts.** The pack's parts, each with its section, name, bars,
and instrument count. A part with **zero stems is not usable**: the server
rejects an arrangement containing it (422, `No stems uploaded yet for: Chorus`).
Mark those parts as blocked and explain why rather than letting the user compose
a running order that will be refused.

**Right — the running order.** An ordered, reorderable list. Each entry is a
part plus a `repeat_count` stepper (1–16), displayed as `Chorus ×2`. Adding the
same part several times is normal and expected. Max 100 entries.

Requirements:

- Drag to reorder, **and** keyboard reorder (move up / move down buttons or
  arrow-key handling on a focused row). Drag-only is not accessible.
- `position` is derived from array order and assigned by the server — never send
  it, never trust a local one after a save.
- Show a running total: number of parts played, and the rendered length once
  known.
- Name is required and unique within the pack (409 on clash → field error).
- `is_default`: the first arrangement becomes default automatically whether or
  not the box was ticked, and setting it on another clears the previous one.
  Reflect that in the UI after a save instead of assuming your local flag.

**Saving.** Create posts `{ name, description?, is_default?, items }` and the
response comes back `status: "rendering"` — switch that arrangement's card into
a rendering state and start polling.

Editing has two distinct behaviours the UI must make obvious:

- changing the **order** (sending `items`) re-renders — warn that the current
  files will be replaced;
- changing only **name/description/default** does not.

**Stale arrangements.** When a stem is replaced or a part deleted, every `ready`
arrangement on the pack flips to `stale`. Show it as a warning state with a
one-click **Re-render** (`POST .../render`), and explain it in a sentence: the
rendered files no longer match the pack's audio. Do not hide the old tracks —
they still play and still download.

**Rendered tracks.** When `ready`, list one row per instrument with its duration
and a preview player. All instruments have the same duration by design; if they
do not, that is a bug worth surfacing rather than hiding.

### 8. Publish

`POST .../publish` emails users and newsletter subscribers — an outward-facing,
irreversible action. Gate it behind a confirmation, and disable it unless the
pack's rolled-up status is `ready` (the server 409s otherwise). Say what it will
do in the confirm copy: "Emails every subscriber that this pack is live."

## Error handling

Read `message` from the envelope and map the ones users will actually hit;
everything else falls through to the message text, which is written to be shown.

| Status | Meaning | UI |
| --- | --- | --- |
| 401 | Token missing/expired | Refresh or bounce to login |
| 403 | Producer touching someone else's pack | "This pack belongs to another producer" |
| 404 | Gone or wrong id | Refetch the list; the pack may have been deleted elsewhere |
| 409 | Conflict — duplicate instrument, duplicate part or arrangement name, stems still processing, nothing ready, publish too early | Field-level error where it maps to a field; otherwise an inline warning with the server's sentence |
| 413 | File > 30 MB | Should never reach the server — catch client-side |
| 422 | Placement/shape | See below |

422s worth mapping explicitly, quoted as the server returns them:

- `part_id is required for a breakdown pack — every stem belongs to a song part`
- `part_id is not accepted for a long-form pack — its stems are one continuous take per instrument`
- `Song parts belong to breakdown packs — a long-form pack holds one continuous stem per instrument`
- `Only breakdown packs are arranged — a long-form pack is already one continuous take per instrument`
- `Got 3 file(s) but 2 instrument(s); counts must match`
- `Unknown instrument in 'kazoo'. Allowed: ...`
- `No stems uploaded yet for: Chorus, Verse 2`
- `Drums appears twice in this upload`

The first four indicate the UI offered something it should not have — treat them
as bugs to prevent, not errors to display. The last one and the count mismatch
belong to form validation.

## Next.js architecture

**Do not proxy audio uploads through a Server Action or a route handler.**
Server Actions default to a 1 MB body limit, and raising it buffers a 30 MB file
in the Node process before it ever reaches the API. Upload from a client
component straight to the API with `fetch` + `FormData`, so the browser streams
it once. This also gives real upload progress (via `XMLHttpRequest.upload` —
`fetch` still has no upload progress event).

Everything else:

- **Reads in server components** where they are not interactive: pack list, pack
  header, initial parts and arrangements. Pass the bearer token from the session
  on the server; do not ship it to the client for those.
- **Client components** for the coverage grid, the upload panels, the arrangement
  builder, and anything that polls.
- **Never let authorised responses be cached by accident.** Use `cache: "no-store"`
  for per-user reads, or tag them (`next: { tags: ["stem-pack", id] }`) and
  `revalidateTag` after mutations. A cached pack list leaking between two
  producers is the failure mode to design against.
- Mutations that only change server state (create part, delete stem, save
  arrangement) are fine as Server Actions with `revalidateTag`; keep the file
  ones on the client.
- Wrap the reorder save in `useTransition` and update optimistically — the list
  should not jump while the request is in flight.
- Respect `prefers-reduced-motion` in the grid's processing animation.

## Definition of done

- Both scopes work from one component set: `/producer/stem-packs/...` and
  `/admin/stem-packs/...`.
- A long-form pack can be created, filled with instruments in one bulk upload,
  polled to `ready`, and published.
- A breakdown pack can be created, given parts, filled part by part, arranged
  with a repeated chorus, polled through `rendering` to `ready`, and its
  rendered tracks previewed.
- Replacing a stem visibly flips its arrangements to `stale` and the re-render
  brings them back to `ready`.
- A part with no stems cannot be dragged into an arrangement, and the reason is
  on screen.
- Every list has an empty state, every async region has a loading state, and
  every destructive action has a confirm naming what it destroys.
- The arrangement order can be changed entirely by keyboard.
- No upload path buffers a file on the Next server.
