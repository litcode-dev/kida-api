# Admin Analytics Endpoint — Design Spec

**Date:** 2026-05-22  
**Status:** Approved

---

## Overview

Add `GET /admin/analytics` — a platform-wide analytics endpoint for admins. Returns global revenue, user growth, top-selling content, and top producers for a configurable time window. Protected by `require_admin`. Follows the same patterns as the existing `GET /producer/analytics`.

---

## Endpoint

```
GET /admin/analytics
Authorization: Bearer <admin token>
```

**Query params** (all optional, reuse `AnalyticsPeriod` from `app/schemas/producer_analytics.py`):

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `period` | `7d \| 30d \| 90d \| all` | `all` | Preset window |
| `from_date` | `date` | — | Custom window start; must pair with `to_date` |
| `to_date` | `date` | — | Custom window end; must pair with `from_date` |

Custom `from_date`/`to_date` override `period`. Validation rules match `AnalyticsParams`.

---

## Response Shape

Standard envelope: `{"status": "success", "data": {...}}`

```json
{
  "period": { "from": "2026-04-22", "to": "2026-05-22" },
  "revenue": {
    "total_earnings": "1234.50",
    "total_sales": 87,
    "by_type": {
      "loops":     { "earnings": "600.00", "sales": 40, "downloads": 120 },
      "drones":    { "earnings": "400.50", "sales": 30, "downloads": 80 },
      "drum_kits": { "earnings": "234.00", "sales": 17, "downloads": 55 }
    },
    "by_provider": {
      "flutterwave": "900.00",
      "paystack": "334.50"
    }
  },
  "users": {
    "total_users": 540,
    "new_users": 32,
    "by_role": { "user": 510, "producer": 28, "admin": 2 }
  },
  "top_content": [
    {
      "id": "uuid",
      "title": "Dark Trap Loop",
      "content_type": "loop",
      "thumbnail_url": "https://cdn.example.com/...",
      "earnings": "200.00",
      "sales": 14
    }
  ],
  "top_producers": [
    {
      "id": "uuid",
      "full_name": "Jane Doe",
      "email": "jane@example.com",
      "total_earnings": "600.00",
      "total_sales": 40
    }
  ]
}
```

`top_content` and `top_producers` each return the top 10 ranked by `earnings` descending.

---

## New Files

### `app/schemas/admin_analytics.py`

Pydantic models:

- `PlatformRevenueSummary` — `total_earnings`, `total_sales`, `by_type: dict[str, TypeStats]` (reuses `TypeStats` from `producer_analytics`), `by_provider: dict[str, Decimal]`
- `UserGrowthStats` — `total_users`, `new_users`, `by_role: dict[str, int]`
- `TopContentItem` — `id`, `title`, `content_type: str`, `thumbnail_url`, `earnings`, `sales`
- `TopProducerItem` — `id`, `full_name`, `email`, `total_earnings`, `total_sales`

### `app/services/admin_analytics_service.py`

Single public function `get_platform_analytics(db, params) -> dict`. Internally runs five queries (all filtered by the resolved time window from `params.resolve_window()`):

1. **Revenue by type** — three `SELECT SUM(amount_paid), COUNT(id)` on `purchases`, filtered `WHERE loop_id IS NOT NULL` / `drone_pad_id IS NOT NULL` / `drum_kit_id IS NOT NULL`. Downloads counted from the `downloads` table in the same pattern.
2. **Revenue by provider** — `SELECT payment_provider, SUM(amount_paid) FROM purchases GROUP BY payment_provider` (time-filtered).
3. **User counts** — `SELECT COUNT(*) FROM users` (total, not time-filtered); `SELECT COUNT(*) FROM users WHERE created_at >= from_dt` (new users, time-filtered); `SELECT role, COUNT(*) FROM users GROUP BY role` (total by role, not time-filtered).
4. **Top content** — three separate ranked queries (loops / drones / drum kits joined with purchases), merged in Python as a list of `TopContentItem`, sorted by earnings descending, top 10 sliced. Thumbnail URLs built with CloudFront base the same way as `producer_analytics_service`.
5. **Top producers** — three separate queries `SELECT created_by, SUM(amount_paid), COUNT(id) FROM purchases JOIN <content_table>`, merged in Python keyed by `producer_id`, totals summed across types, joined with a single `SELECT id, full_name, email FROM users WHERE id IN (...)`, sorted by total earnings descending, top 10 sliced.

---

## Modified Files

### `app/routers/admin.py`

Add at the bottom:

```python
@router.get("/analytics")
async def platform_analytics(
    period: AnalyticsPeriod = Query(AnalyticsPeriod.all),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    ...
```

Validation + error handling follows the same pattern as `producer.py` (catch `ValidationError`, raise `AppError` 422).

---

## Auth

`require_admin` dependency — same as all other admin-only endpoints in this router. No new auth needed.

---

## Out of Scope

- Caching (not used on analytics endpoints)
- Per-producer drill-down (a separate task if ever needed)
- Download counts in `top_content` (not included; only `earnings` and `sales` rank items)
- Pagination for `top_content` / `top_producers` (fixed top-10 lists)
