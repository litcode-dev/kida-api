# The daily new-content email

One email a day listing everything that went live since the last one, and one
push notification saying the same thing. Per-item pushes still fire the moment
something is ready — this is the roundup, not the alert.

## What sends it

The digest is due once a day at `CONTENT_DIGEST_HOUR_UTC` (default 17:00 UTC,
18:00 in Lagos). Three things can start it, and exactly one of them can send it:

| Trigger | Where it runs | When |
| --- | --- | --- |
| `beat` | the celery beat process | at the scheduled hour |
| `api` | inside the API process | every `CONTENT_DIGEST_SCHEDULER_INTERVAL_SECONDS`, if the due slot has not been claimed |
| `manual` | `POST /api/v1/admin/email/digest/run`, or `--send-now` | when somebody asks |

The `api` trigger exists because beat is a separate process that serves no
traffic: a deployment that never started one — or started one that later died —
looks completely healthy while no daily mail is ever sent. The API checks the
same schedule and sends the digest itself if nothing else has, up to
`CONTENT_DIGEST_CATCH_UP_HOURS` late (beyond that the run waits for the next
day's slot, because "today's drops" at 4am is not a fix).

Running the digest means claiming its slot: a unique `run_key` row in
`digest_runs`. Beat, several API replicas and an admin pressing the button can
all fire at once and the list still receives one email. A manual run takes its
own timestamped key, so it never consumes the day's scheduled digest.

Set `CONTENT_DIGEST_SCHEDULER_ENABLED=false` on deployments that do run beat and
want it to be the only sender.

## The push that goes with it

The same digest goes out as a single OneSignal broadcast to every subscribed
device, immediately after the mail. It reaches the people who are on the app
rather than in an inbox, and it is the only announcement a stem pack gets when
its producer publishes it days after upload — by then its per-item push has long
since fired.

The heading is the email's subject line ("3 new drops on Kida"), so somebody who
gets both reads the second as the first rather than as a second batch. The body
names the item when there is one and counts by type when there are several
("2 loops and 1 drum kit just dropped") — a phone shows about two lines, and a
list of twelve titles arrives as an ellipsis. The payload carries
`{"type": "content_digest"}` for the app to route on.

The mail is what a run succeeds or fails on. The push happens after the send and
its outcome only ever lands on the run row, in `push_status`:

| `push_status` | Meaning |
| --- | --- |
| `sent` | OneSignal accepted the broadcast; `push_detail` says for how many devices |
| `disabled` | `CONTENT_DIGEST_PUSH_ENABLED` is false — the mail still went |
| `not_configured` | `ONESIGNAL_APP_ID`/`ONESIGNAL_API_KEY` is empty |
| `no_devices` | accepted, but nobody is subscribed |
| `failed` | OneSignal rejected it, or the call blew up |
| *empty* | no push was attempted, because no mail was sent either |

A failed push never releases the content. The items are already in thousands of
inboxes; re-announcing them tomorrow to make a notification work would be the
more expensive mistake, so the run stays `sent` and the failure is recorded
beside it.

## What each run records

Every attempt writes a `digest_runs` row, which is what makes "did it even fire"
answerable:

| Status | Meaning |
| --- | --- |
| `sent` | mail was handed to the provider — `sent`/`failed` count the addresses |
| `empty` | nothing new was ready; no mail, nothing claimed |
| `no_recipients` | nobody to send to; nothing claimed |
| `not_configured` | the mail backend has no credentials, so the run refused to start |
| `failed` | the send failed — see `detail` and `claimed_ids` |
| `running` | claimed and still going, or the process died mid-run |

Two of these used to be silent losses:

* **`not_configured`.** `send_email` skips and returns when its backend has no
  credentials, and bulk sending counts a skip as sent — so a missing key
  produced a run reporting `sent=N, failed=0` while delivering nothing, with
  every item stamped as announced and therefore never mentioned again. The run
  now stops before claiming anything: set the credentials and the next digest
  sends the backlog.
A rejected provider *batch* is no longer written off either: bulk sending falls
back to one message at a time for that chunk, because the batch endpoint accepts
a narrower payload than the single one (per-message headers among them), and a
chunk it refuses is not necessarily mail that cannot be sent. Skipped messages
are counted as failures rather than sends, which is what makes the `sent` number
on a run mean something.

* **`failed` with every message rejected.** Items are stamped *before* the send
  (a crashed send must not re-blast the whole list tomorrow), so a send that
  delivered nothing at all used to bury the day's content permanently. When
  nothing was delivered the claim is released and the content returns to the
  next digest. A *partial* send keeps its claim — some people already have it.

## When no mail arrived

From an admin token, without shell access:

    GET  /api/v1/admin/email/digest       # schedule, credentials, queue, last runs
                                          # including push_enabled/push_problem
    POST /api/v1/admin/email/digest/run   # send it now, and report what happened

From a shell on the service:

    python -m scripts.digest_status                       # the same, plus what is blocked
    python -m scripts.digest_status --send-now --yes      # send it now
    python -m scripts.digest_status --release loop:<uuid> # put an item back in the queue

Read the verdict at the bottom of `digest_status`. The usual causes, in the
order it checks them: the feature is off, the migration has not run, the mail
backend has no credentials, no recipients resolve, nothing triggered the run, the
run failed, or the content simply is not ready yet (a drum kit, drone or stem
pack is only announceable once every one of its files has finished processing,
and a stem pack also needs its producer to publish it).

Anything that existed before the digest was first deployed was backfilled as
already announced and will not be re-sent; `--release` is how you send it
deliberately.
