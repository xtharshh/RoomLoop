# Six-minute walkthrough

For the live interview (or for whoever reviews the repo). Every step is a real
click against the running service — nothing is staged.

```bash
uvicorn roomloop.api:app --reload
open http://localhost:8000/console
```

---

### 0:00 — Frame it (30s)

> "I built an HTTP API plus a console, because the two hard requirements are
> interactions: skipping a clashing week, and killing a whole series. The console
> is served by the same process, no build step, and it calls the live API."

Point at the two office clocks in the header. They are the whole timezone story
in one glance: Berlin is `+02:00`, Denver is `-06:00` **on the same day**.

### 0:30 — The calendar (60s)

- Today's grid: rooms as rows, working hours as columns, series instances marked `↻`.
- Click a booking → drawer shows three representations of the same moment:
  wall clock (`2026-11-02T09:00:00`), exact instant (`08:00Z`), and offset (`+01:00`).
- "That triple is the C1 constraint. The legacy naive string is preserved; the
  instant is what we actually store."

### 1:30 — Book back-to-back (45s)

`New booking` → Aurora → start `10:00`, duration `1h` → create.
Then book `11:00`–`12:00` in the same room. It succeeds.

> "Half-open intervals: a booking ends at 10:00, it does not occupy 10:00. This
> is the boundary almost everyone gets wrong."

Now try `09:30`–`10:30`: rejected with `SLOT_UNAVAILABLE` and the blocking
booking named in the drawer.

### 2:15 — A series that partially clashes (90s)

`New series` → pick **Basalt** (room 2), a Monday at `09:00`, six weeks, conflict
policy **Skip it, save the rest**.

> "Basalt already has a Monday 09:00 series and one board booking, so some weeks
> will clash. The result screen reports created vs skipped per occurrence, and
> names the blocker. That's R2."

Open **Recurring series** → the same series shows 6 bars, one dashed. Click it:
every instance with its `utc_offset`. After the DST change, the offset moves and
the wall clock does not.

### 3:45 — All-or-nothing (45s)

Same form, but switch to **All-or-nothing**. Submit.

> "Now the whole thing fails and nothing is written. That's R1. Both policies run
> inside one `BEGIN IMMEDIATE` transaction — the difference is only whether a
> clash raises or is collected."

Switch to **Calendar** and show the weeks are still free. Then
`pytest -q tests/test_scenarios.py -v` and point at
*“Strict mode rolls the whole series back on one clash”*.

### 4:30 — Cancellation that frees the room (45s)

Series drawer → **Cancel future instances**. Past instances stay confirmed and
in the calendar; the upcoming ones disappear. Immediately create a booking in one
of the freed slots to prove the room is genuinely available.

> "The office manager's complaint was rooms sitting busy forever. Cancellation is
> a soft state — rows stay for reporting, every conflict query filters them out —
> so the room frees up instantly and history survives."

### 5:15 — Close on the risk list (45s)

Open **Design notes**.

> "R1 and R2 contradict each other, so I made the policy explicit rather than
> guessing. The Denver hour-off bug is naive timestamps plus two offices: we store
> epochs and derive the legacy string. And here's what I left out — auth,
> notifications, series editing — with the order I'd do them in."

---

## Questions they will probably ask, with short answers

**"Why SQLite?"**
`BEGIN IMMEDIATE` serialises writers, which is right for 200 people and zero
infra. `tests/test_concurrency.py` races eight bookings at one slot and asserts
exactly one wins. On Postgres I'd add an exclusion constraint on
`(room_id, tstzrange(start, end))` so the database enforces overlap too.

**"How do you know the DST logic is right?"**
Five domain tests plus two data-driven scenarios: Berlin falls back on 2026-10-25,
Denver on 2026-11-01. Same wall clock, different UTC instants, asserted
explicitly. The spring-forward gap is also tested: 02:30 does not exist, so it
shifts to 03:30 and the response says `DST_GAP_SHIFTED`.

**"What if two requests race?"**
Conflict check and insert are inside one write transaction, so the second sees
the first's row. Also `Idempotency-Key` on both create endpoints, with a body
fingerprint so a reused key with different data is a 409 rather than a duplicate.

**"Why is `start` still naive?"**
C1: the nightly report parses naive local ISO and cannot change this quarter.
Changing the format would be the technically clean choice and the wrong product
decision. I added `start_utc` instead and flagged it as a question for the PM.

**"The sample `/rooms` had ids 3, 4, 9, 17. Yours are 1–5."**
R5 says rooms are numbered sequentially and availability iterates `1..N`, so I
kept sequential ids and the same names/capacities. If the gaps are real
production data, that's a question for the PM — and `/rooms` still works, only
the iteration assumption changes.

**"What's the weakest part?"**
No auth: `user` is a free string and anyone can cancel anyone's series. That is
the first thing I would add, and it also unlocks the "who may cancel" rules the
office manager will ask for next.

**"What would you do with another day?"**
Series editing with "this and future" semantics, calendar invites, and a
`/availability/week` endpoint so the dashboard stops making seven calls.
