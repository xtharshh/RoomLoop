# Decisions

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

I used AI to scaffold FastAPI + SQLite, to draft the console, and to enumerate
DST edge cases. It was genuinely faster at boilerplate and at suggesting test
matrices. It was confidently wrong about several things that mattered, and each
one is now covered by a test:

1. **Returned UTC naive strings as the legacy format.** That silently changes
   every reported booking time and breaks C1. Corrected to office wall clock,
   with additive UTC fields instead.
2. **Used implicit SQLite transactions** (`isolation_level` default), so an
   explicit `BEGIN IMMEDIATE` raised "cannot start a transaction within a
   transaction". Corrected to autocommit connections with explicit
   `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`, plus `busy_timeout`.
3. **Kept the impossible wall clock in DST-gap occurrences** — it stored 03:30
   but reported 02:30, so the API lied about its own data. Corrected so the
   resolved time is reported and the warning names the occurrences.
4. **Reused one error code for everything.** A single-booking clash returned the
   series error. Split into `SLOT_UNAVAILABLE`, `SERIES_CONFLICT_FAIL_FAST`,
   `SERIES_FULLY_CONFLICTED`.
5. **Trusted the sample ids** `3, 4, 9, 17` as the room table while also
   implementing R5's `1..N` iteration. Caught by writing the availability test.

The pattern: AI was reliable for structure and unreliable about *contracts* —
timestamp meaning, transaction boundaries, error taxonomy. Those are the parts I
reviewed line by line and pinned with tests.

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
