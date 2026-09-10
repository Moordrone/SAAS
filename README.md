# EasyEM SaaS — backend foundation

Sprint 0 + accounts. One deployable FastAPI unit, organised in modules with
explicit boundaries (ADR-02). Everything here is finished and tested — there
are no placeholder screens.

```bash
cp .env.example .env
make up            # builds, starts db + redis + api + worker, migrates
open http://localhost:8000/docs
```

In dev, `EMAIL_BACKEND=console`: verification links are printed in the API logs
(`make logs`). That is where you get your first token — simulation requires a
verified address.

**Working in Jupyter?** `notebooks/getting-started.ipynb` runs the whole product
without a server: `TestClient` exercises the app in-process, and the worker's
unit of work is called by hand rather than looping.

```python
from easyem.notebook import bootstrap
nb = bootstrap()
auth = nb.signup("you@lab.example")       # signs up, verifies, logs in
nb.work()                                  # drains the job queue
```

## What is implemented

| Area | Status |
|---|---|
| `accounts` + `memberships` with roles | done |
| Signup, email verification, login, password reset | done |
| Rotating refresh tokens with reuse detection | done |
| Argon2id hashing, lockout after repeated failures | done |
| Immutable credit ledger, reservations, reaper, reconciliation | done |
| `problem+json` errors with request ids, `/v1` prefix | done |
| Alembic migrations, Docker, CI on SQLite **and** PostgreSQL | done |
| Unit engine — SI canonical, dimension-checked conversion | done |
| Material library — FR4, Rogers, alumina, with reference frequencies | done |
| Component registry with bounds and derivable parameters | done |
| Analytical models — patch (cavity), microstrip (Hammerstad) | done |
| Validation with physical plausibility rules | done |
| Projects, immutable versions, canonical content hash | done |
| Published JSON Schema, generated from the registry | done |
| Solver contract + three backends (`mock`, `analytical`, `openems`) | done |
| Contract test suite, run against every backend | done |
| openEMS script generation, mesh planning, FDTD cost model | done |
| Jobs: quote → reserve → run → settle, with a stalled-worker reaper | done |
| Per-error-code billing policy | done |

| Worker process, atomic claim, short transactions | done |
| Transactional email (console / memory / SMTP) | done |
| CORS, rate limiting, JSON logging | done |
| Account deletion, session purge | done |

Not yet started: the copilot, billing.

```
make up            # postgres + redis + api + worker
make test          # 234 tests, 46 of them the solver contract
```

The loop closes end to end today:

```
1. quote     ceiling=1.0  balance=50.0
2. reserve   queued / reserved   held=1.0
3. run       succeeded   actual=0.1   cpu=0.02s
4. replay    same job = True
5. balance   49.900000
6. results   S11 min = -48.8 dB at 2.4500 GHz
             patch   = 41.34 x 33.10 mm
             inset   = 12.00 mm -> 49.6 ohm
             gain    = 5.23 dBi   Q=108   BW=0.65%
7. ledger    consistent
```

## The Engineering Engine

`easyem/engineering/` is the part that is worth owning. The copilot is a
front-end to it; the solver is a backend behind it. Both are replaceable, this
is not.

**Units are SI internally, always.** `Quantity` knows its dimension, so
assigning `2.45 GHz` to a length parameter raises rather than silently
producing a patch 2.45 metres wide. Display units are a presentation concern and
never reach the database.

**The registry decides, the copilot proposes.** `components.py` states which
parameters exist, their dimension, their physical bounds and which ones the
engine can derive rather than ask for. A language model may suggest a value; it
may not invent a parameter or exceed a bound.

**The analytical models are real.** Rectangular patch by the cavity /
transmission-line model (Balanis §14.2), microstrip by Hammerstad's closed forms
with Wheeler synthesis. They are checked in CI against published worked
examples, not against themselves:

| Case | EasyEM | Reference |
|---|---|---|
| Patch, er=2.2, h=1.588 mm, 10 GHz | W = 11.85 mm, L = 9.05 mm | Balanis: 11.86, 9.06 mm |
| 50 Ω microstrip, FR-4, h=1.6 mm | W = 3.083 mm | tables: 3.0–3.1 mm |
| Patch directivity, thin substrate | 6.8 dBi | published: 6–9 dBi |

Accuracy is roughly 1–2 % for microstrip in 0.05 < W/h < 20, and ~5 % for a
patch on a thin substrate. Outside those ranges the result carries a warning
saying so. This matters commercially: an approximate answer grounded in physics
can be checked against a full-wave run, whereas a plausible fabrication is
quietly contradicted by one — in front of an engineer who will notice.

Warnings never block. Telling someone their FR-4 design at 5.8 GHz will be lossy
is useful; refusing to simulate it would be presumptuous.

## Projects and the content hash

Versions are immutable and append-only. Editing writes a new version, so a
simulation launched an hour ago still points at exactly the inputs it ran on.
Reverting writes a *new* version holding the old content — history is never
rewritten.

A new version is only written when the physics actually changed. That judgement
comes from `content_hash`, which is taken over the canonical form: SI-normalised,
key-sorted, with annotations stripped. So:

- `2.45 GHz` and `2450 MHz` produce the **same** hash — same design, no new version
- changing `provenance` from `user` to `ai_suggested` produces the same hash
- changing the frequency produces a different one

The same property gives result caching, deduplication, and a reproducibility
token a researcher can quote in a paper.

## The worker

The HTTP request reserves credits, queues the job and returns — 51 ms. A worker
picks it up.

That split is not a deployment detail, it is a correctness fix. Previously the
whole solve ran inside the request's transaction, which held a row lock on the
wallet from `reserve` until the response. Two runs on one account serialised
completely: with openEMS, the second user waited 167 seconds and their browser
gave up first. The connection pool drained at about twenty concurrent runs.

```
HTTP      reserve, queue, commit          milliseconds
claim     one guarded UPDATE, commit      atomic across workers
execute   no transaction held             progress committed per heartbeat
settle    one short transaction
```

Claiming uses `UPDATE ... WHERE id = ? AND execution_status = 'queued'` and
checks the rowcount. Whoever lands the write first wins; everyone else gets zero
rows and moves on. It behaves identically on PostgreSQL and SQLite, unlike
`FOR UPDATE SKIP LOCKED`.

`run_maintenance` runs on a timer inside the worker and calls the three things
that already existed and that nothing was calling: `recover_stalled_jobs`,
`expire_stale_reservations` and `reconcile`. A ledger divergence logs at ERROR,
because it means money is being counted wrong.

## Hardening

Four things the audit found missing, all now tested rather than assumed:

- **Email actually sends.** `verify_email` gated simulation, the token was
  emailed by nobody, so no account could ever run anything — while 203 tests
  passed. The suite now reads tokens out of a `MemoryEmailBackend`, so tests go
  through the same door as users. Production refuses to start without
  `EMAIL_BACKEND=smtp`.
- **CORS**, with an explicit origin list. No wildcard.
- **Rate limiting** on the authentication endpoints. Account lockout already
  stopped brute force against one account; this stops spraying across thousands.
  In-memory by default, which is per-process and therefore leaky with several
  workers — the store is behind an interface for Redis.
- **JSON logging.** The middleware was already passing `request_id` and
  `duration_ms` as extra fields; without a formatter they vanished.

## The solver contract

`easyem/solver/base.py` is the only surface between EasyEM and a solver, and it
was written against **two** implementations from the start. That was deliberate:
an interface shaped by one implementation takes the shape of that
implementation. The mock is instant, deterministic and has no failure modes;
EMG-TLM will be slow, resource-hungry and will fail in a dozen ways. A contract
shaped only by the mock would not survive meeting it.

`tests/solver_contract/` runs the same 22 tests against every registered
backend. It asserts behaviour, never numbers — a backend returning different
physics is still valid, a backend whose progress goes backwards is not:

- progress stays in 0–100 and never decreases
- submitting the same `job_id` twice does not run it twice
- success reports measured `SolverUsage`, or settlement can only ever debit the
  reserved amount and the estimator can never be calibrated
- failures use an enumerated `error_code`, because free-form strings cannot
  drive a billing policy
- `|S11| <= 1` — a passive structure cannot reflect more than it receives; the
  cheapest physics check there is, and it catches sign errors in any new backend
- cancel is safe on a finished job and on an unknown one
- the resonance lands within 10 % of the design frequency, and S11 is not flat —
  every structural check above passes for a backend returning a straight line
- an asynchronous backend declares durable state, and a *fresh adapter instance*
  can find its jobs. The suite runs in one process, which hid exactly this bug:
  `analytical` and `mock` keep state in a dict, which is fine only because they
  finish inside `submit()`. `openems` keeps it on disk, and that is the pattern
  EMG-TLM must follow.

**EMG-TLM is integrated the day it goes green here.** That turns phase 9 from a
gamble into an acceptance test. `docs/adding-a-solver-backend.md` lists what a
research solver typically has to grow first — cancellation, progress, measured
usage, an error taxonomy, job identity and durable state. That gap, not the
numerics, is where solver integrations fail.

### Three backends

`analytical` wraps the Engineering Engine into frequency sweeps. The patch S11
comes from a parallel RLC whose Q is the estimated bandwidth; the line comes
from an ABCD matrix. Real physics, milliseconds, stated validity limits.

`mock` exists to develop the frontend and to exercise failure paths the
analytical backend cannot reach on demand. Every result it produces carries
`demonstration_only: true` and a NOT PHYSICAL notice, set by the backend so
nothing downstream can drop it. `registry.py` makes it unreachable by a
non-admin in production.

`openems` is full-wave FDTD. EasyEM generates a standalone openEMS script from
the Project JSON, runs it as a subprocess and reads back `results.json`.

**On the GPL.** openEMS is GPL v3. Running it as a separate process
communicating through files is the standard arm's-length boundary; linking its
library in would put the licence question squarely in play. There is no openEMS
import anywhere in `easyem/solver/openems/`, and a test walks the AST of every
module in that package to keep it that way. Have a lawyer confirm the position
before selling, not after.

The generated script (see `docs/example_openems_run.py`) is plain readable
Python that a customer can download and run themselves. That is a feature: what
sells a simulation tool to sceptical engineers is that they can check it.

The script generation is what EasyEM actually adds. Anyone can install openEMS;
what costs an engineer an afternoon is writing the geometry, mesh and port
script for each new design.

### The FDTD cost model

`openems/mesh.py` sizes the grid and the run before anything is committed, which
is what makes an honest quote possible. Structured time-domain solvers can be
estimated from geometry alone — a frequency-domain solver with adaptive meshing
cannot.

| Case | Cells | Timesteps | Runtime | Credits (exp/cap) |
|---|---|---|---|---|
| Patch, RO4003C 0.813 mm, 2.45 GHz | 100k | 42k | ~167 s | 8.4 / 20.9 |
| Patch, FR-4 1.6 mm, 2.45 GHz | 150k | 13k | ~76 s | 3.8 / 9.5 |
| Patch, RT5880 1.575 mm, 5.8 GHz | 86k | 3k | ~10 s | 1.0 / 2.5 |
| 50 Ω line, RT5880, 10 GHz | 98k | 3k | ~12 s | 1.0 / 2.5 |

These match openEMS tutorial runs (100k–500k cells, 1–3 minutes). The first
version of the planner did not: it applied the substrate cell size to the whole
domain, giving 79 million cells and a 28,000-credit quote for a job that costs
about eight. A real mesh is graded — fine through the dielectric, coarse through
the air. A test now pins the cell count to the range a patch actually needs.

The thin-substrate case is worth noting: Courant is limited by the *smallest*
cell, so a 0.813 mm substrate needs 42k timesteps where a 1.6 mm one needs 13k.
Thin substrates are expensive in time domain, and missing that understates
runtime and breaches the quoted ceiling.

## Billing a failed run

Whether a failure is charged is a commercial decision, so it is decided per
cause against a published table rather than by whoever writes the next
exception message:

| Error | Billing |
|---|---|
| `INVALID_GEOMETRY`, `MESH_FAILED` | not charged |
| `SOLVER_INTERNAL`, `PLATFORM_ERROR`, `TIMEOUT` | not charged — our bug, our cost |
| `NON_CONVERGED` | charged at actual |
| `RESOURCE_EXCEEDED` | charged at the cap, partial results |
| `USER_CANCELLED` | prorated on CPU actually burned |

A test asserts that **every** code resolves its reservation. A code that left a
hold dangling is how credits silently vanish from an account.

## Two status fields, not one

`execution_status` and `settlement_status` are independent. A job can be
succeeded and not yet settled, or failed and already released. Collapsing them
into one enumeration forces an invented state for every combination and makes
"which jobs are running?" ambiguous.

## Three decisions this code already commits to

**One `accounts` table.** Not `owner_user_id`/`organization_id`, not a
polymorphic `owner_type`. Every project, wallet and subscription hangs off one
`account_id` with a real foreign key. A personal account is an organization
with one member, so turning a solo user into a team is an INSERT into
`memberships` rather than a data migration.

**The ledger is append-only; the balance is a cache.** `credit_transactions` is
never updated or deleted — a mistake is corrected by an opposing `adjustment`
entry. `credit_wallets.balance` is a projection, written under a row lock in
the same transaction as the entry that changed it. `make reconcile` recomputes
every balance from the ledger and exits non-zero on any divergence; run it
nightly and page someone if it fires.

**Mutable state lives outside the ledger.** A reservation moves from `held` to
`settled`/`released`/`expired` and points at the ledger rows that caused each
move. This is what makes the reaper possible: a worker killed mid-job leaves a
hold behind, and `expire_stale_reservations` frees it. Without that, one crashed
worker permanently reduces a customer's usable balance and they never find out
why.

## The job/credit lifecycle

```
reserve(cost_max)            hold the ceiling the customer was quoted
  ├── settle(cost_actual)    release the hold, consume the real cost
  ├── release()              job failed → full refund
  └── expire()               worker died → reaper frees it
```

`settle` writes two entries rather than one net entry, so a statement shows
what was held and what was actually used. `actual` is capped at the reserved
amount: the quote is a ceiling, and the customer is never charged above the
number they were shown before pressing Run.

## Layout

```
easyem/
  config.py       settings; refuses the dev secret outside dev/test
  db.py           engine, session, UtcDateTime
  errors.py       RFC 9457 problem+json, one shape for every error
  models/         accounts, identity, credits
  identity/       security.py (argon2, JWT), service.py (signup/login/refresh)
  credits/        service.py — the ledger
  api/v1/         auth, me, credits
migrations/       alembic
tests/            42 tests
```

`UtcDateTime` exists because PostgreSQL round-trips timezone-aware values and
SQLite silently drops the offset. Normalising in one type stops the tests from
passing on a semantic production does not share.

## Tests worth reading first

`tests/test_credit_ledger.py` — each test maps to a way of losing money:

- a webhook replayed three times credits once
- two sequential holds cannot exceed the balance
- a customer is never charged above the quote
- a failed job refunds the whole hold
- an expired reservation is freed by the reaper
- `reconcile` detects a tampered balance

`tests/test_auth.py::test_reusing_a_refresh_token_revokes_the_whole_family` —
replaying a spent refresh token kills every session it spawned, including the
legitimate one. Losing one good session is the correct price for closing a
stolen one.

## What comes next

In order. Each depends on the one above it.

1. **Resolve the payment path.** Stripe does not support Moroccan entities.
   Merchant of Record, US LLC or EU entity — the choice changes the shape of
   the `BillingProvider` interface, so it blocks the billing module. This is
   the only item that cannot be solved by writing code, which is why it is
   still first, and why it has been first for three iterations.
2. **Move `jobs.run` into a worker.** It currently executes inline in the
   request. The service already carries `heartbeat_at`, `worker_id`, `attempt`
   and `timeout_seconds`, and `recover_stalled_jobs` already exists — the
   worker process is the only missing piece.
3. **Copilot** over the Engineering Engine, with per-account spend caps applied
   before the call and token accounting on every message. The tool list is
   built server-side from the registry, so the model cannot invent parameters.
4. **Billing** on the decision from step 1.

## Known gaps

Deliberate, and worth stating rather than discovering later:

- **Credit expiry and bucketing.** Plan-included credits that lapse monthly and
  purchased credits that do not are two different liabilities. The schema does
  not distinguish them yet; consumption ordering will need a bucket column.
- **Organizations are in the schema but not in the product.** `list_projects`
  and `list_jobs` still filter on `default_account_id`, so a member never sees
  their organization's work. Either finish it or drop it — the current state
  pays the complexity and delivers none of the benefit.
- **Results are stored whole in PostgreSQL.** 32 KB per analytical job, roughly
  70 KB per openEMS one. `CanonicalResults.artifacts` exists to reference large
  payloads by URI and the contract tests enforce it, but `summary` goes around
  that. Push sweeps to object storage; keep scalars and pointers in the table.
- **Rate limiting is per-process.** Two API containers allow twice the quota.
  Move the store to Redis before scaling out.
- **No data export.** Account deletion works; GDPR export does not.
- **Cursor pagination is declared but not wired.** `/v1/credits/transactions`
  still takes a limit.
- **Two components only.** Rectangular patch and microstrip line. Filters,
  couplers and waveguides are schema entries plus an analytical model each —
  the framework holds, the library is thin.
- **No dispersion in the microstrip model.** Quasi-static only. The result
  warns when frequency is high enough for this to matter.
- **`schemas/project/v1.0.0.json` is generated, not hand-written.** Regenerate
  it whenever the registry changes, or the two will drift.
- **Feed reactance is not modelled.** The inset is snapped to a 0.05 mm design
  grid, which removes the impossible perfect match, but predicted return loss
  is still an upper bound — a fabricated part will do worse. Stated in the
  result warnings rather than papered over with a fudge factor.
- **The credit cost formula is a placeholder.** `_actual_cost` charges
  `cpu_seconds * 0.5`. Real pricing needs the unit-economics work: cloud cost
  per second, target margin, break-even.
- **No result caching yet.** `content_hash` is stored and indexed on every job,
  so identical physics is already detectable — nothing consumes it.
- **openEMS is not installed in CI**, so nothing verifies that openEMS actually
  accepts the generated script. Everything else about it is tested: script
  syntax, geometry values, mesh sizing, cost, the licence boundary. Running one
  real patch is the first thing to do on a machine that has openEMS.
- **Two geometry templates only** for openEMS — patch and microstrip line. Each
  new component needs its own template; there is no general CAD path.
- **Nothing has been compared to a measurement.** Three methods agreeing is
  evidence, but all three are models. Etch one patch, measure it on a VNA. It is
  a week of work and worth more than any refactoring on this list.
