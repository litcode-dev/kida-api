# Registration Email Template — Design Spec
**Date:** 2026-05-21
**Status:** Approved

## Overview

Redesign the `registration_html()` function in `app/services/email_service.py` to use a bold, editorial layout inspired by the Northwind Press reference. Dark hero section, cream body, no external dependencies — all inline styles.

## Layout

### Header (`#0a0a0a` background)
- Top bar: `LITMUSIC` (left, small-caps, white) — `NO. 0001 / WELCOME` (right, small-caps, muted grey)
- Purple label: `YOU'RE IN.` (`#6c3bdb`, small-caps, bold)
- Large bold headline: `Make your` (white) on one line, `sound.` (purple `#6c3bdb`, italic) on the next
- Metadata strip: `EST. 2024 · PREMIUM LOOPS · STEM PACKS` (small, muted, letter-spaced)

### Body (`#f2ede4` cream background)
- Greeting: `Hi {full_name},` bold
- 2-sentence welcome copy
- Dark CTA button (`#0a0a0a` fill, white text): `Browse Loops →`
- Stats row (3 columns, large bold numbers): `500+` Loops · `10` Genres · `0` Subscriptions

### Footer (`#0a0a0a` background)
- `LM` square logo block (purple background, white text)
- Brand line: `LITMUSIC · PROFESSIONAL MUSIC PRODUCTION SAMPLES`
- Unsubscribe / preferences links (muted)

## Constraints
- All styles inline (email client compatibility)
- No images or external fonts — system font stack only
- Must render acceptably in Gmail, Apple Mail, Outlook
- Single function, no new files
