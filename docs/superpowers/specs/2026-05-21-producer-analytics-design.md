# Producer Analytics — Design Spec

**Date:** 2026-05-21
**Status:** Approved

---

## Overview

Producers need visibility into how their uploaded content is performing — both financially (earnings from purchases) and in terms of reach (download counts). This feature adds a single analytics endpoint that returns aggregate totals and per-item breakdowns for loops, drones, and drum kits, all filterable by time period.

---

## Endpoint

```
GET /api/v1/producer/analytics
```

**Auth:** `require_producer` — producers only see data for content where `created_by = producer.id`.

### Query Parameters

| Param | Type | Default | Description |
|---|---|---|---|
| `period` | `7d \| 30d \| 90d \| all` | `all` | Preset time window |
| `from` | ISO date string | — | Start of custom range (overrides `period` when both set) |
| `to` | ISO date string | — | End of custom range (overrides `period` when both set) |
| `loops_page` | int | `1` | Page index for loops section |
| `drones_page` | int | `1` | Page index for drones section |
| `drum_kits_page` | int | `1` | Page index for drum kits section |
| `page_size` | int | `20` | Shared page size across all sections |

**Period resolution rules:**
- If `from` and `to` are both provided, they take precedence over `period`.
- `period=all` applies no date filter.
- Preset windows are computed relative to the request timestamp (e.g. `7d` = now minus 7 days).

---

## Response Shape

```json
{
  "status": "success",
  "data": {
    "period": { "from": "2026-04-21", "to": "2026-05-21" },
    "summary": {
      "total_earnings": "45000.00",
      "total_sales": 95,
      "total_downloads": 832,
      "by_type": {
        "loops":     { "earnings": "20000.00", "sales": 50, "downloads": 400 },
        "drones":    { "earnings": "15000.00", "sales": 30, "downloads": 232 },
        "drum_kits": { "earnings": "10000.00", "sales": 15, "downloads": 200 }
      }
    },
    "loops": {
      "items": [
        {
          "id": "uuid",
          "title": "Midnight Afro",
          "thumbnail_url": "https://cdn.example.com/thumbnails/abc.jpg",
          "earnings": "4000.00",
          "sales": 5,
          "downloads": 120
        }
      ],
      "total": 12,
      "page": 1,
      "page_size": 20
    },
    "drones": {
      "items": [
        {
          "id": "uuid",
          "title": "Storm Drone",
          "thumbnail_url": null,
          "earnings": "3000.00",
          "sales": 3,
          "downloads": 45
        }
      ],
      "total": 5,
      "page": 1,
      "page_size": 20
    },
    "drum_kits": {
      "items": [
        {
          "id": "uuid",
          "title": "808 Kit Vol. 1",
          "thumbnail_url": "https://cdn.example.com/thumbnails/xyz.jpg",
          "earnings": "10000.00",
          "sales": 15,
          "downloads": 200
        }
      ],
      "total": 3,
      "page": 1,
      "page_size": 20
    }
  }
}
```

**`period` in response:**
- `from` and `to` are always returned as ISO date strings reflecting the actual window used.
- For `period=all`, both are `null`.

---

## Schema Migration

Add `drone_pad_id` to the `downloads` table so drone download counts are time-filterable consistently with loops and drum kits.

```
downloads.drone_pad_id  UUID  nullable  FK → drone_pads.id  ON DELETE SET NULL
```

Migration file: `alembic/versions/..._add_drone_pad_id_to_downloads.py`

The `Download` model is updated to include:
```python
drone_pad_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True), ForeignKey("drone_pads.id", ondelete="SET NULL"), nullable=True
)
```

---

## New Files

| File | Purpose |
|---|---|
| `app/routers/producer.py` | Single endpoint, mounts at `/producer` |
| `app/services/producer_analytics_service.py` | All DB queries |
| `app/schemas/producer_analytics.py` | Pydantic request params + response models |
| `alembic/versions/..._add_drone_pad_id_to_downloads.py` | Migration |

The router is registered in `app/main.py` under the `/api/v1` prefix.

---

## Data Query Strategy

All six aggregate queries run concurrently via `asyncio.gather`. Period filtering is applied via `WHERE created_at BETWEEN :from AND :to` on the `purchases` table and `WHERE downloaded_at BETWEEN :from AND :to` on the `downloads` table.

### Earnings & Sales (from `purchases`)

| Content type | Join path |
|---|---|
| Loop | `purchases.loop_id → loops.id WHERE loops.created_by = me` |
| Drone | `purchases.drone_pad_id → drone_pads.id → drone_pads.drone_id → drones.id WHERE drones.created_by = me` |
| DrumKit | `purchases.drum_kit_id → drum_kits.id WHERE drum_kits.created_by = me` |

Per-item: group by item ID, `SUM(amount_paid)` for earnings, `COUNT(*)` for sales.

For drones, purchases are stored at the `drone_pad` level. Analytics aggregate across all pads belonging to the same `drone_id`, so each row in `drones.items` represents one drone entity (not one pad). Grouping: `purchases.drone_pad_id → drone_pads.drone_id`, then group by `drone_id`.

### Downloads (from `downloads`)

| Content type | Column |
|---|---|
| Loop | `downloads.loop_id` |
| Drone | `downloads.drone_pad_id` |
| DrumKit | `downloads.drum_kit_id` |

Same join pattern as above to scope to the producer's items. Per-item: group by item ID, `COUNT(*)`.

For drones, downloads are aggregated to the `drone_id` level the same way as purchases: `downloads.drone_pad_id → drone_pads.drone_id`, grouped by `drone_id`.

### Per-Item Assembly

After aggregates are computed, item metadata (title, thumbnail S3 key → CloudFront URL) is fetched from the respective model table and merged. Items with zero earnings and zero downloads within the selected period are still returned if the producer owns them.

---

## Error Handling

| Case | Behaviour |
|---|---|
| `from` provided without `to` (or vice versa) | 422 — both must be present if either is set |
| `from` > `to` | 422 — invalid date range |
| `period` is not one of the valid enum values | 422 — FastAPI enum validation |
| Producer has no uploads | Returns empty item lists, all totals zero |

---

## Files Modified

- `app/models/download.py` — add `drone_pad_id` column
- `app/main.py` — register `producer` router
