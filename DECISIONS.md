# Decisions

## TL;DR — the short version

1. **R1 vs R2 conflict** → both are implemented; `conflict_policy: "skip"` is the default (R2), `"fail"` gives strict all-or-nothing (R1). A series where every occurrence clashes always fails; an empty series is never saved.
2. **Timestamps** → stored as UTC epoch seconds, rendered back to the exact naive local ISO the reporting job expects (C1). The legacy string means *wall clock at the room's office*.
3. **Weekly repeats the wall clock, not the elapsed time** → expanded on naive datetimes, resolved per occurrence in the office timezone. Berlin and Denver each cross DST on their own date.
4. **DST edges are decided, not inherited** → non-existent spring-forward times shift forward with a `DST_GAP_SHIFTED` warning; ambiguous fall-back times use the first instant.
5. **`GET /rooms` did not gain a single field** (C2) → richer data is `?detail=true`. Room ids are sequential `1..5` because R5 iterates `1..N`, even though the sample showed `3, 4, 9, 17`.
6. **Cancelling a series is soft and immediate** → future instances marked cancelled, past kept for reporting, every conflict query ignores cancelled rows so the room frees at once. Idempotent, with a `scope=single` exception hatch.
7. **Racing writers are safe by construction** → conflict check plus inserts inside one `BEGIN IMMEDIATE`; proven by racing eight requests at one slot.
8. **PM questions I would ask first** → cancel-from-now vs cancel-from-occurrence; which timezone the nightly report renders; whether the `3, 4, 9, 17` gaps are real; weekly-only vs biweekly; who is notified when a week is skipped.
9. **AI was dependable for structure, unreliable for semantics** → it produced six wrong contracts (UTC leaking into the legacy format, implicit transactions, an impossible 02:30, one shared error code, the sample ids, and assuming the OS ships a timezone database). Each is pinned by a named test; the table is in §10.
10. **Deliberately left out** → auth, notifications, series editing, Postgres exclusion constraints, pagination. Next, in order: SSO identity, "this and future" edits, Postgres with a range exclusion constraint.
11. **Portability is a correctness issue, not a chore** → `zoneinfo` silently depends on the host's IANA database; Windows has none. It is now a pinned dependency, a distinct error code, and a `/health` field, because "works on my machine" for a timezone-heavy service means the code is wrong somewhere.

---

## The longer version

Ten decisions, in the order they mattered. Each is something the brief either
left open or made contradictory, plus what I did and why.

---

### 1. R1 and R2 contradict each other, so the policy is a request parameter

R1 says a recurring booking is all-or-nothing; R2 says one or two clashes must
be skipped so the rest can be created. Both cannot be the default.

I implemented both: **`conflict_policy: "skip"` is the default** (it satisfies
R2 and the PM's complaint about teams being blocked by a single clash), and
**`conflict_policy: "fail"`** gives strict R1 semantics. Either way the write is
genuinely atomic inside one `BEGIN IMMEDIATE` transaction. A series whose every
occurrence clashes is rejected with `SERIES_FULLY_CONFLICTED` in both modes —
saving an empty series would look like success and hide the problem.

The alternative I rejected — "atomic unless skipping is needed" — makes the
guarantee untestable and impossible to explain in a runbook.

### 2. Timestamps are stored as UTC epoch seconds; the naive format is a rendering

Constraint C1 fixes the wire format: naive local ISO, no offset. That format is
also, almost certainly, why Denver bookings were an hour off: 09:00 MDT and
09:00 MST are different instants with the same string.

So the database stores `INTEGER` epoch seconds plus each room's IANA timezone,
and `start`/`end` are *derived* from a real instant at the edge. Overlap becomes
integer comparison, DST can never corrupt stored data, and reporting keeps
exactly the contract it has today. Additive fields (`start_utc`, `end_utc`,
`utc_offset`, `timezone`) let new clients opt into unambiguous data without
breaking the old ones.

I did **not** add an offset to `start`: that would break C1, and "fixing" a
format the reporting team cannot change this quarter is how you break a
nightly job. This is in the questions for the PM below.

### 3. A naive timestamp means the wall clock of the room's office

The brief gives no rule for the naive values. I had to pin one, otherwise every
consumer guesses differently:

- `POST` bodies: naive = wall clock at the target room's office. So
  `2026-11-02T09:00:00` in room 1 is Berlin, in room 4 it is Denver. An explicit
  offset is honoured as an exact instant.
- `GET /rooms` has no clock, so `start`/`end` are the office wall clock and
  `start_utc` carries the instant.
- Window *filters* (`GET /bookings`, `GET /availability`) span offices and have
  no single wall clock, so naive bounds there are read as **UTC**. This is the
  one place the rule differs, and it is documented in `/meta` and the OpenAPI
  descriptions rather than left to be discovered.

### 4. Weekly means "same wall clock", not "same elapsed time"

The series is expanded on naive local datetimes (`+7 days`), then each
occurrence is resolved against the office timezone independently. That gives
09:00 every Monday in Berlin across the 2026-10-25 fall-back — the UTC instant
moves from 07:00Z to 08:00Z, the meeting does not move. Denver crosses on
2026-11-01, a week later, and gets its own treatment.

Two DST edges are decided explicitly rather than left to `datetime`:
a **non-existent** time (spring-forward gap) is shifted forward by the gap and
reported as a `DST_GAP_SHIFTED` warning — the meeting still happens; an
**ambiguous** time (fall-back) uses the first of the two instants. Both are
tested.

### 5. `GET /rooms` did not grow a single field

The dashboard "renders whatever comes back" (C2) and the sample response is
`{id, name, capacity}`. The default response is exactly that — no `office`, no
`timezone`, nothing. Extra data lives behind `?detail=true` and on `/rooms/{id}`.
A dashboard build cannot break because of us.

The sample also shows ids `3, 4, 9, 17`, which contradicts R5 ("rooms are
numbered sequentially"). I treated the sample as illustrative of *shape* and
made room ids sequential `1..5`, with the same names and capacities, because
availability iterates `1..N`. If the gaps are real production data, `/rooms`
still works — only the iteration assumption in R5 is affected, which is why
this is a question for the PM.

### 6. Cancelling a series means "from now on", and it is undo-able in spirit

`DELETE /bookings/{id}` on a series instance cancels that and every future
instance, keeping already-started ones — the office manager's "get rid of a
whole series easily". `?scope=single` is the exception hatch for one week.

A booking that has already *started* is not cancelled; a meeting in progress was
presumably happening. Cancelled rows are retained (soft state) because the
reporting job needs history, and they are excluded from every conflict query, so
the room frees up immediately. Cancels are idempotent — a double-click returns
`already_cancelled: true` rather than a 404.

`POST /bookings` and `POST /bookings/recurring` honour an `Idempotency-Key`
header, with a stored request fingerprint: a retry with the same key and body
returns the original result (`200` instead of `201`), a retry with a different
body is `409 IDEMPOTENCY_KEY_REUSED`. A booking request is exactly the thing
mobile clients retry.

### 7. Availability is a first-class read, not a client-side loop

R5 suggests the client iterates rooms. That pushes N HTTP requests and the
conflict rule into every caller, so the server does it: `GET /availability`
returns free rooms for an exact window, and `GET /availability/day` returns a
per-room busy list plus bookable slots. Both walk room ids `1..N`.

Working hours (08:00–19:00) are an **assumption** — the brief never states them.
It is a query parameter with those defaults, surfaced in `/meta` and labelled
`assumed: true` in responses, so nobody mistakes it for a policy.

### 8. Bounded series and a 24-hour cap

A weekly series is capped at 260 occurrences (~5 years) and a single booking at
24 hours; both return a specific error code rather than a stack trace. Without a
bound, `repeat_until: 3000-01-01` is a one-request denial of service. The cap is
a constant, not a config file, because inventing a settings system for one number
would be noise.

### 9. SQLite, deliberately, with the concurrency story tested

`BEGIN IMMEDIATE` plus `busy_timeout` serialises writers, which is enough for a
200-person company and lets the whole thing run with zero infrastructure. The
trade-off is concrete and tested: `tests/test_concurrency.py` races eight
bookings at the same slot and asserts exactly one wins, and races four series
and asserts none interleave. On Postgres I would add an exclusion constraint on
`(room_id, tstzrange(start, end))` so the database enforces the invariant too.

### 10. Where AI helped, and what it got wrong

I used AI as a pair programmer: to scaffold FastAPI + SQLite, to draft the
console's markup and CSS, and to brainstorm DST edge cases I might have missed.
It was fast at boilerplate and generous at generating test matrices.

It was unreliable about **contracts** — the places where "looks reasonable" and
"is correct" diverge. Six things it produced were wrong. Each is now pinned by a
test, so none of them can come back:

| What it produced | Why it would have hurt | What I did instead | Pinned by |
| --- | --- | --- | --- |
| Returned UTC naive strings as the legacy `start`/`end` | Silently shifts every reported booking time and breaks C1 for the reporting team | Legacy fields render the office wall clock; UTC moved to additive `start_utc` | `test_create_returns_legacy_naive_local_timestamps` |
| Used implicit SQLite transactions | Explicit `BEGIN IMMEDIATE` raised *"cannot start a transaction within a transaction"*, so R1's atomicity claim was never actually exercised | Autocommit connections with explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` and a `busy_timeout` | `tests/test_concurrency.py` |
| Stored 03:30 for a spring-forward gap but reported 02:30 | The API contradicted its own data, and a client would have booked a time that does not exist | The resolved time is authoritative; the requested one is reported as `DST_GAP_SHIFTED` | `test_berlin_spring_forward_gap_is_shifted_and_flagged` |
| Reused one error code for unrelated failures | A single-booking clash looked like a series rollback; callers cannot branch on it | `SLOT_UNAVAILABLE`, `SERIES_CONFLICT_FAIL_FAST`, `SERIES_FULLY_CONFLICTED` | `test_overlap_is_rejected_with_409_and_the_blocker` |
| Treated the sample room ids `3, 4, 9, 17` as the table | Contradicts R5's `1..N` iteration; availability silently skips gaps | Sequential seed ids `1..5`, sample treated as shape only, gap question raised with the PM | `test_room_ids_are_sequential_for_iteration` |
| Assumed the IANA timezone database always exists | It does on Linux, not on Windows. The first Windows run returned 422 `Unknown timezone 'Europe/Berlin'` — a missing OS package reported as a bad request | `tzdata` is a dependency, missing data is `TZ_DATA_MISSING` (500) with the install command, `/health` reports `timezone_data: "ok"` or `"missing"` | `tests/test_environment.py` |

The pattern is the useful part: AI was dependable for **structure** (routes,
schema, layout) and unreliable for **semantics** (what a timestamp means, where a
transaction begins and ends, which error is which). So that is where my review
time went — every line that touched time, transaction boundaries or error
mapping, with a failing test written before any of it was trusted.

Two of those defects were invisible from reading. The SQLite transaction bug only
appeared under concurrent writes, and the timezone gap only appeared on a host
without the OS database. Both were found by running the service, not reviewing it
— which is the argument for running it.

### Questions I would ask the PM before this ships

1. **Cancel semantics:** is "from now on" right, or should cancelling a future
   occurrence cancel from *that* occurrence onward? And should an in-progress
   meeting be cancellable at all?
2. **Reporting timezone:** now that we know the stored string is wall clock at
   the office, does the nightly job know which office? Should we add a
   `room_timezone` column to its input, or switch it to `start_utc`?
3. **Room ids:** are `3, 4, 9, 17` historical gaps we must preserve, or is
   sequential numbering authoritative as R5 implies?
4. **Series shape:** is weekly-only really enough, or do we need biweekly and
   "second Tuesday"? What is the real maximum series length — 6 months is the
   complaint, 5 years is my cap.
5. **Skipped occurrences:** who is told when a week is skipped, and is the
   requester expected to fix it manually? Right now the API reports it and
   nobody is notified.
6. **Working hours:** is 08:00–19:00 correct for both offices, and are there
   rooms that are bookable outside it (or on weekends)?
7. **Identity:** is `user` an email or an employee id, and which SSO system
   should own authorisation — specifically, who may cancel someone else's
   series?

### (d) Deliberately left out, and what I'd do next

Left out, with reasoning:

- **Authentication / authorisation.** Out of scope for a booking core, but it is
  the first thing production needs. `user` is currently a free string.
- **Notifications.** No email, Slack or calendar invites. Real recurring
  bookings need at least an invite and a cancellation notice.
- **Editing a series.** Create and cancel only. "This and future occurrences"
  edits need the same atomicity guarantees and would double the surface.
- **Cross-office booking rules**, e.g. can a Berlin employee book Denver's room
  at 03:00 Berlin time? Currently yes — availability is per office.
- **Pagination, rate limiting, ETags, audit retention.** A 200-person tool does
  not need them yet; `GET /bookings` is windowed and capped at 2000 rows.
- **Attendee counts vs room capacity.** Capacity is exposed but not enforced,
  because the brief's booking shape has no attendee field.

Next, in order: (1) SSO identity + permission rules, (2) series editing with
atomic "this and future" updates, (3) Postgres with an exclusion constraint on
room/time and a migration of the SQLite data, (4) calendar invites and
cancellation notices, (5) a `GET /availability/week` view so the dashboard can
stop doing 7 round trips.
