# RoomLoop

The booking core for our internal meeting-room service: single bookings, weekly
recurring bookings, cancellation, and availability — with an animated console
you can actually click through.

**Python 3.11+ · FastAPI · SQLite · no build step for the UI · 84 tests**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m roomloop.seed --reset          # demo data (optional but recommended)
uvicorn roomloop.api:app --reload
```

Then open **http://localhost:8000/console** for the console and
**http://localhost:8000/docs** for the generated OpenAPI docs.

```bash
pytest -q                                # 84 tests, ~2s
pytest -q tests/test_scenarios.py -v     # the data-driven edge cases
```

---

## Why an HTTP API and also a UI

The brief allows a REST API *or* a CLI. A REST API is the right call here: the
booking core exists to be consumed by other internal tools (the facilities
dashboard already calls `GET /rooms`), and the two hardest requirements — "skip
just the clashing occurrences" and "cancel the whole series easily" — are
interactions, not commands you want to express as flags. A reviewer or the
office manager can verify both by clicking, which is worth more than a tidy CLI.

The console is served by the same process, has no build step, and calls the real
API — nothing is mocked. If you only want the API, ignore `/console`.

## What the console gives you

| Area | What it shows |
| --- | --- |
| **Calendar** | One day at a time, rooms as rows, office hours as columns. Series instances carry a `↻`, a "now" line shows real progress, and free slots are one click from booking. |
| **Booking drawer** | Wall-clock entry, duration chips, live preview of *local → stored → exact UTC instant*, one-off or weekly, and a conflict-policy switch (`skip` vs `all-or-nothing`). Results report created vs skipped counts per occurrence. |
| **Recurring series** | Every series with its instance strip, live/cancelled counts, and a drawer listing all instances with their UTC offsets — so the DST shift is visible, not theoretical. Cancel the whole future in one action, or a single occurrence. |
| **API explorer** | Ten curated calls you can fire at the running server, with status codes, latency and highlighted JSON. |
| **Design notes** | R1–R5 and C1–C2 rendered as cards in the app, for whoever opens it without reading the repo. |

Keyboard: `1`–`4` switch views, `←`/`→` change day, `n` new booking, `Esc` closes.

## API in one screen

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/rooms` | Legacy payload: exactly `id`, `name`, `capacity` (C2). `?detail=true` adds office/timezone. |
| `GET` | `/rooms/{id}` | One room. |
| `GET` | `/availability` | Which rooms are free for an exact window; iterates room ids `1..N` (R5). |
| `GET` | `/availability/day` | Free/busy grid + 30-minute bookable slots for one local day. |
| `POST` | `/bookings` | Create one booking. |
| `POST` | `/bookings/recurring` | Create a weekly series up front. |
| `GET` | `/bookings` | Active bookings overlapping a UTC window. |
| `GET` | `/bookings/{id}` | One booking, including `status`. |
| `DELETE` | `/bookings/{id}?scope=series\|single` | Cancel a booking; series-aware by default. |
| `POST` | `/bookings/{id}/cancel` | Same, for clients that cannot send `DELETE`. |
| `GET` | `/series`, `GET /series/{id}`, `DELETE /series/{id}` | List, inspect, cancel series. |
| `GET` | `/meta`, `/health`, `/audit` | Policy constants, liveness, activity trail. |

Full request/response shapes and error codes: [`docs/API.md`](docs/API.md).
A six-minute demo script for the live round: [`docs/WALKTHROUGH.md`](docs/WALKTHROUGH.md).

### Creating a recurring booking

```bash
curl -s localhost:8000/bookings/recurring \
  -H 'Content-Type: application/json' \
  -d '{
    "room_id": 1,
    "user": "platform@example.com",
    "start": "2026-11-02T09:00:00",
    "end":   "2026-11-02T10:00:00",
    "repeat_until": "2027-01-25",
    "conflict_policy": "skip"
  }' | jq
```

```jsonc
{
  "series_id": 7,
  "created_count": 12,
  "skipped_count": 1,
  "skipped": [
    {
      "occurrence_index": 6,
      "start": "2026-12-14T09:00:00",
      "end":   "2026-12-14T10:00:00",
      "reason": "ROOM_BUSY",
      "conflicts_with": [{"id": 41, "user": "board@example.com",
                          "start": "2026-12-14T09:30:00", "end": "2026-12-14T10:30:00"}]
    }
  ],
  "warnings": []
}
```

## How each requirement is satisfied

| # | Requirement | Implementation | Proof |
| --- | --- | --- | --- |
| R1 | Recurring create is all-or-nothing | One `BEGIN IMMEDIATE` transaction around conflict checks and every insert; any exception rolls back (`roomloop/db.py::transaction`) | `test_strict_policy_saves_nothing`, `test_concurrency.py`, scenario *Strict mode rolls the whole series back* |
| R2 | Skip only clashing instances | Conflicts are collected per occurrence, survivors inserted, every skip returned with its blocker (`service.create_recurring`) | `test_clashing_occurrence_is_skipped_and_reported`, scenario *One clashing occurrence…* |
| R3 | Same wall-clock time, two offices | Weekday expansion steps naive local datetimes by 7 days, then resolves each in the office zone (`domain.expand_weekly`, `resolve_wall_clock`) | 5 domain tests + scenarios for Berlin **and** Denver fall-back, plus the spring-forward gap |
| R4 | Overlap in the same room; back-to-back is fine | Half-open intervals `start_a < end_b and start_b < end_a` | `TestOverlap`, both boundary scenarios |
| R5 | Iterate rooms by id 1..N | `service.iter_rooms()` walks ids rather than trusting a list | `test_room_ids_are_sequential_for_iteration`, `test_capacity_filter_uses_sequential_room_ids` |
| C1 | Naive local ISO timestamps must not change | `start`/`end` are still naive, second-precision, offset-free; extras are additive (`start_utc`, `utc_offset`, `timezone`) | `test_create_returns_legacy_naive_local_timestamps` |
| C2 | Dashboard renders whatever comes back | Default `GET /rooms` payload is byte-for-byte the legacy shape; detail is opt-in | `test_rooms_payload_keeps_the_legacy_shape` |

### The parts that are actually tricky

**R1 and R2 genuinely conflict.** Resolved by making the policy explicit and
per-request: `skip` (default, satisfies R2) and `fail` (strict rollback,
satisfies R1). A series where *every* occurrence clashes always fails with
`SERIES_FULLY_CONFLICTED` — silently creating an empty series would be worse
than an error. Details in [DECISIONS.md](DECISIONS.md).

**DST is the real bug.** "Denver bookings were an hour off" happens when you
store naive local times: 09:00 MDT is 15:00Z, 09:00 MST is 16:00Z. This service
stores UTC epoch seconds and derives the legacy naive string from a real
instant. Recurrence is expanded on naive wall clocks so 09:00 stays 09:00, and
each occurrence resolves its own offset. Ambiguous fall-back times take the
first instant; non-existent spring-forward times shift forward with a
`DST_GAP_SHIFTED` warning instead of being silently mangled.

**Cancelling must free the room.** Cancellation marks rows, and every conflict
query filters `cancelled_at IS NULL`, so a cancelled series can be re-booked
immediately. Past instances stay `confirmed` — that is what reporting expects.

## Test data

[`data/scenarios.json`](data/scenarios.json) is a reviewable, data-driven suite
of 12 acceptance scenarios, each with a `why` field explaining what could go
wrong. `tests/test_scenarios.py` runs each against a fresh database. It covers:
half-open boundaries, one-minute overlaps, mixed offsets, Berlin's and Denver's
*different* DST dates, the spring-forward gap, skip-vs-rollback, fully
conflicted series, room scoping, inclusive `repeat_until`, and re-booking a
cancelled slot.

`python -m roomloop.seed --reset` loads demo data aimed at the same risks: a
Berlin series crossing its fall-back, a Denver series crossing a different
fall-back, a series with exactly one blocked week, a back-to-back pair, and
past instances that must survive a series cancellation.

## Project layout

```
roomloop/
  domain.py       pure rules: timestamps, DST, overlap, weekly expansion
  db.py           SQLite schema, UTC-epoch storage, transactions, projections
  service.py      use cases: create / create series / cancel / availability
  api.py          FastAPI routes, error envelope, static console
  demo.py         demo dataset (also used by the reset button)
  seed.py         CLI: python -m roomloop.seed
  static/         console (index.html, styles.css, app.js) — no build step
data/scenarios.json   acceptance scenarios with rationale
tests/          84 tests: domain, API, scenarios, concurrency, UI shell
docs/API.md      endpoint reference and error codes
DECISIONS.md     trade-offs, open questions, AI usage, what is missing
```

## What I would do next

Auth and ownership, editing a series ("this and future"), a Postgres exclusion
constraint so the database enforces overlap too, and calendar invites. The full
list, including deliberate omissions, is in
[DECISIONS.md](DECISIONS.md#d-deliberately-left-out-and-what-id-do-next).
