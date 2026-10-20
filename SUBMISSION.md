# Submission — RoomLoop booking service

Take-home for Junior Software Development Engineer, Round 2.

| | |
| --- | --- |
| **Repository** | https://github.com/xtharshh/RoomLoop |
| **Language / stack** | Python 3.11+, FastAPI, SQLite (`sqlite3` + `zoneinfo` from stdlib), zero-build UI |
| **Tests** | 95, `python -m pytest -q` (~2s) |
| **Runtime dependencies** | 2 (`fastapi`, `uvicorn`) plus `tzdata` for Windows |

## Where the three deliverables are

1. **Code + README with run instructions** → [`README.md`](README.md)
   Quick start for macOS/Linux and Windows, API table, and a requirement-by-requirement
   table mapping R1–R5 / C1–C2 to the code and to the test that pins each one.

2. **Own test data** → [`data/scenarios.json`](data/scenarios.json)
   12 acceptance scenarios, each with a `why` field naming the behaviour at risk.
   Executed by `tests/test_scenarios.py` against a fresh database per scenario.
   Demo seed data (also used by the console's reset button) lives in
   `roomloop/demo.py`.

3. **DECISIONS.md** →
   - (a) decisions the brief left open → §1–§9
   - (b) questions for the PM → *Questions I would ask the PM before this ships*
   - (c) where AI helped and what it got wrong → §10 (table of six defects, each with
     the fix and the test that pins it)
   - (d) deliberately left out, and what comes next → *Deliberately left out…*

## Verify it in three commands

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest -q                                            # 95 passed
```

Then, optionally:

```bash
python -m roomloop.seed --reset                      # demo data
python -m uvicorn roomloop.api:app --reload
```

- Console: <http://localhost:8000/console>
- Swagger: <http://localhost:8000/docs>
- Health: `curl -s localhost:8000/health` → `{"status":"ok",...,"timezone_data":"ok"}`

## Where to look first

If you only read three things:

1. **`DECISIONS.md` §1** — R1 and R2 contradict each other. Both are implemented; the
   policy is explicit per request (`conflict_policy: "skip" | "fail"`) rather than a
   silent default.
2. **`docs/dst.svg`** — why the wall clock stays at 09:00 while the stored instant moves
   from `07:00Z` to `08:00Z`. This is the "Denver was an hour off" bug, and the reason
   storage is UTC epoch seconds.
3. **`roomloop/service.py::create_recurring`** — conflict detection and every insert
   share one `BEGIN IMMEDIATE` transaction, so "nothing was saved" is literally true.

`docs/WALKTHROUGH.md` is a six-minute script for demoing this live, including short
answers to the questions I would expect (why SQLite, why naive timestamps stay, what is
the weakest part).

## Scope notes

- Single bookings, weekly recurring bookings, cancellation, and availability are
  implemented. Editing an existing series is not (see DECISIONS.md, *left out*).
- No authentication: `user` is a free string and anyone may cancel anything. Known,
  documented, first item on the "what I would do next" list.
- Working hours 08:00–19:00 are an assumption, exposed as query parameters and marked
  `assumed: true` in responses.
