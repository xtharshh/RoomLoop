# RoomLoop API

Base URL `http://localhost:8000`. Interactive docs at `/docs`, schema at
`/openapi.json`. This file covers the semantics OpenAPI cannot express.

## Timestamps

| Where | Rule |
| --- | --- |
| `POST` bodies (`start`, `end`, `repeat_until`) | Naive means **wall clock at the target room's office**. An explicit offset or `Z` is honoured as an exact instant. |
| Responses (`start`, `end`) | Legacy contract: naive, second precision, **no offset** — the office wall clock. Unchanged from the prototype (C1). |
| Responses (`start_utc`, `end_utc`) | Additive: the exact instant, `...Z`. |
| Responses (`utc_offset`) | Additive: the office's offset for that instant, e.g. `+02:00`. |
| `GET /bookings`, `GET /availability` filters | Windows span offices, so naive bounds are interpreted as **UTC**. |

`repeat_until` is a **local date**, inclusive: an occurrence may start on that
date but not after it.

## Errors

Every error — including framework validation failures — uses one envelope:

```json
{
  "error": {
    "code": "SLOT_UNAVAILABLE",
    "message": "That room is already booked for part of this window",
    "details": {"conflicts": [{"id": 41, "user": "mira@example.com",
                               "start": "2026-12-14T09:30:00", "end": "2026-12-14T10:30:00"}]}
  }
}
```

| Code | HTTP | Meaning |
| --- | --- | --- |
| `REQUEST_VALIDATION_ERROR` | 422 | Body did not match the schema; `details.problems[]` names the fields. |
| `INVALID_TIMESTAMP` | 422 | Timestamp is not ISO-8601. |
| `INVALID_INTERVAL` | 422 | `end` not after `start`, or longer than 24 hours. |
| `INVALID_REPEAT_UNTIL` | 422 | `repeat_until` is before the first occurrence. |
| `RECURRENCE_TOO_LONG` | 422 | More than 260 occurrences. |
| `VALIDATION_ERROR` | 422 | Other rule violation (unknown policy, demo endpoints disabled…). |
| `NOT_FOUND` | 404 | Room, booking or series does not exist. |
| `SLOT_UNAVAILABLE` | 409 | A single booking clashes. `details.conflicts[]` names the blocker. |
| `SERIES_CONFLICT_FAIL_FAST` | 409 | `conflict_policy=fail` hit a clash; nothing was written. |
| `SERIES_FULLY_CONFLICTED` | 409 | Every occurrence clashes; nothing was written. |
| `IDEMPOTENCY_KEY_REUSED` | 409 | Same `Idempotency-Key`, different body. |

Conflict responses carry `X-RoomLoop-Retryable: false` — retrying the same
request will fail identically.

## Endpoints

### `GET /rooms`

Exactly the legacy payload (C2). Additive data is opt-in.

```bash
curl -s localhost:8000/rooms
# [{"id":1,"name":"Aurora","capacity":8}, …]

curl -s 'localhost:8000/rooms?detail=true&office=Denver'
# [{"id":4,"name":"Dune","capacity":6,"office":"Denver","timezone":"America/Denver"}, …]
```

Room ids are sequential from 1 (R5). The seed is:

| id | name | capacity | office | timezone |
| --- | --- | --- | --- | --- |
| 1 | Aurora | 8 | Berlin | Europe/Berlin |
| 2 | Basalt | 4 | Berlin | Europe/Berlin |
| 3 | Cinder | 12 | Berlin | Europe/Berlin |
| 4 | Dune | 6 | Denver | America/Denver |
| 5 | Ember | 10 | Denver | America/Denver |

### `POST /bookings`

```bash
curl -s localhost:8000/bookings -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 7c1f' -d '{
    "room_id": 1, "user": "mira@example.com",
    "start": "2026-11-02T09:00:00", "end": "2026-11-02T10:00:00"
  }'
```

```jsonc
{
  "id": 12, "room_id": 1, "room_name": "Aurora", "office": "Berlin",
  "user": "mira@example.com",
  "start": "2026-11-02T09:00:00",          // legacy: wall clock, no offset
  "end":   "2026-11-02T10:00:00",
  "timezone": "Europe/Berlin",
  "start_utc": "2026-11-02T08:00:00Z",
  "end_utc":   "2026-11-02T09:00:00Z",
  "utc_offset": "+01:00",
  "status": "confirmed",
  "warnings": ["DST_AMBIGUOUS_FIRST_INSTANT"]   // only when relevant
}
```

`201` on create, `200` with `idempotent_replay: true` on a keyed retry.
Back-to-back windows (`10:00` after `09:00–10:00`) are accepted (R4).

### `POST /bookings/recurring`

Creates every instance up front. Weekly, same wall clock (R3).

```json
{
  "room_id": 1,
  "user": "platform@example.com",
  "start": "2026-11-02T09:00:00",
  "end":   "2026-11-02T10:00:00",
  "repeat_until": "2027-01-25",
  "conflict_policy": "skip"
}
```

| Field | Default | Notes |
| --- | --- | --- |
| `repeat_until` | required | Inclusive local date. |
| `conflict_policy` | `skip` | `skip` = R2 (default). `fail` = strict R1 rollback. |

Response:

```jsonc
{
  "series_id": 7, "room_id": 1, "room_name": "Aurora", "office": "Berlin",
  "user": "platform@example.com", "timezone": "Europe/Berlin",
  "recurrence": {
    "frequency": "weekly", "interval": 1,
    "local_start": "2026-11-02T09:00:00", "local_end": "2026-11-02T10:00:00",
    "duration_minutes": 60, "repeat_until": "2027-01-25"
  },
  "requested_count": 13, "created_count": 12, "skipped_count": 1,
  "created": [ { "id": 13, "start": "...", "start_utc": "...", "utc_offset": "+01:00",
                 "series_id": 7, "occurrence_index": 0 } ],
  "skipped": [ { "occurrence_index": 6, "start": "2026-12-14T09:00:00",
                 "reason": "ROOM_BUSY",
                 "conflicts_with": [ { "id": 41, "user": "board@example.com",
                                       "start": "2026-12-14T09:30:00",
                                       "end": "2026-12-14T10:30:00" } ] } ],
  "warnings": [ { "code": "DST_GAP_SHIFTED", "message": "...",
                  "occurrence_indexes": [8] } ]
}
```

`skipped[].reason` is `ROOM_BUSY` or `OVERLAPS_SIBLING_INSTANCE` (the latter is
defensive — weekly occurrences at most 24h long cannot overlap each other).

### `GET /bookings`, `GET /bookings/{id}`

`GET /bookings?start=…&end=…&room_id=…&user=…&limit=…` returns active bookings
overlapping a UTC window, plus the resolved window and the naive-is-UTC note.

### `DELETE /bookings/{id}?scope=series|single`

```bash
curl -s -X DELETE 'localhost:8000/bookings/13?scope=series'
```

```jsonc
{
  "booking_id": 13, "series_id": 7, "scope": "series",
  "cancelled_count": 11,   // future instances cancelled
  "kept_instances": 1,     // already started, left intact
  "message": "Series cancelled from now on; past occurrences were kept"
}
```

- `scope=series` (default) cancels this and every **future** instance.
- `scope=single` cancels only that occurrence and reports how many remain.
- Already cancelled → `200` with `already_cancelled: true` (idempotent).
- Unknown id → `404`.

`POST /bookings/{id}/cancel` with `{"scope": "single"}` is the same operation
for clients that cannot send `DELETE`.

### `GET /series`, `GET /series/{id}`, `DELETE /series/{id}`

`GET /series/{id}` lists every instance with its `status`, `start`, `end` and
`utc_offset` — the clearest way to see DST handled correctly.

```bash
curl -s localhost:8000/series/4 | jq '.instances[] | {start, utc_offset, status}'
# { "start": "2026-10-06T09:00:00", "utc_offset": "+02:00", "status": "confirmed" }
# { "start": "2026-10-13T09:00:00", "utc_offset": "+02:00", "status": "confirmed" }
# { "start": "2026-10-20T09:00:00", "utc_offset": "+02:00", "status": "confirmed" }
# { "start": "2026-10-27T09:00:00", "utc_offset": "+01:00", "status": "confirmed" }   ← after fall-back
```

### `GET /availability`

```bash
curl -s 'localhost:8000/availability?start=2026-11-02T09:00:00Z&end=2026-11-02T10:00:00Z&capacity=8'
```

```jsonc
{
  "window": {"start": "2026-11-02T09:00:00", "end": "2026-11-02T10:00:00"},
  "available_rooms": [2, 3, 5],
  "rooms": [
    {"room_id": 1, "name": "Aurora", "capacity": 8, "office": "Berlin",
     "available": false,
     "conflicts": [{"id": 12, "start": "2026-11-02T09:00:00", "end": "2026-11-02T10:00:00"}]}
  ]
}
```

### `GET /availability/day`

`?day=2026-11-02&office=Berlin&day_start=08:00&day_end=19:00&slot_minutes=30`

Per room: `busy[]` (with `series_id`), `free_slots[]`, `utilised_minutes`,
`available_minutes`, the day's `utc_offset` and the assumed `working_hours`.
This is what the console renders.

### Ops

| Endpoint | Notes |
| --- | --- |
| `GET /health` | `{"status":"ok","rooms":5,"database":"file"}` |
| `GET /meta` | Policy constants: caps, working hours, policies, timestamp contract, offices. |
| `GET /audit?limit=20` | Recent `booking.created`, `series.created`, `booking.cancelled`, `series.cancelled` events. |
| `POST /admin/reset-demo` | Wipes bookings and re-seeds the demo set. 422 when `ROOMLOOP_DEMO=0`. |
