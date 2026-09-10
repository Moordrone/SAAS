# Adding a solver backend

The contract suite is the acceptance test. A backend is integrated the day it
goes green — not the day it produces a plot someone likes.

```bash
pytest tests/solver_contract/ -q          # every registered backend
pytest tests/solver_contract/ -k emgtlm   # just yours
```

## What EMG-TLM has to expose

A research solver typically reads an input file, writes output files, and stops
on bad input. That is not enough. The gap below is where solver integrations
actually fail — the numerics are rarely the problem.

| Contract requirement | Why | Typical gap in a research code |
|---|---|---|
| Cancel mid-run, with a bounded stop time | The customer is paying by the second | No interrupt handling at all |
| Monotonic progress, readable while running | A bar that goes backwards destroys trust | Prints to stdout, if anything |
| `SolverUsage` on completion | Settlement charges measured cost, and the estimator calibrates against it | Nothing measured |
| Enumerated `error_code` | Billing policy is decided per cause, not per message | Segfault, or `stop 1` |
| Idempotent `submit` | A retried worker must not run twice | No job identity |
| Durable state | A run spanning minutes will meet a deploy | Everything in memory |

Budget weeks for this, not days. The suite tells you exactly which of the six
you still owe.

## Steps

**1. Declare what you are.**

```python
class EMGTLMAdapter(SolverAdapter):
    key = "emg-tlm"
    version = "1.0.0"
    physical = True        # real numbers, reachable in production
    synchronous = False    # submit() returns before the run finishes
    durable_state = True   # therefore state must outlive the process
```

`synchronous = False` without `durable_state = True` fails the contract, and
correctly so: process-local state loses the job on restart and freezes the
customer's credits until the reaper finds them.

**2. Copy the openEMS state pattern.** `easyem/solver/openems/adapter.py` gives
each job a directory holding the script, the pid, a progress file and the
results, written with write-then-rename so a poller never reads a half-written
file. Any worker can pick up any job by reading it. TLM has the same shape as
FDTD here, so most of that adapter transfers directly.

**3. Reuse the mesh planner.** `easyem/solver/openems/mesh.py` is deliberately
method-agnostic — Cartesian grid, Courant-limited timestep, run length from the
resonator Q. TLM shares all three. Recalibrate two constants against your solver
and the cost model works:

```python
CELL_UPDATES_PER_SECOND = 25e6   # measure on your hardware
BYTES_PER_CELL = 100             # measure your footprint
```

Measure them low and conservative. An estimate that reads optimistic produces a
ceiling the run then exceeds, and the ceiling is a promise made before the
customer pressed Run.

**4. Register it.**

```python
_BACKENDS["emg-tlm"] = EMGTLMAdapter()
```

Register conditionally, like openEMS does, if the solver may be absent. Offering
a backend that cannot run is worse than not offering it: the customer finds out
after their credits are held.

**5. Cross-check against the two backends already here.** Run the same patch
through `analytical` and `openems` and compare. Three independent methods
agreeing within a few percent is evidence; one method alone is an assertion.

## What the suite does not check

Be honest about the residue:

- **Whether the physics is right.** The suite checks that resonance lands within
  10 % of the design frequency and that S11 is not flat. It cannot tell a
  correct solver from a plausibly wrong one.
- **Mesh convergence.** Nobody verifies that halving the cell size leaves the
  answer alone.
- **Agreement with measurement.** Nothing here has been compared against a VNA.
  Before selling to RF engineers, etch one patch and measure it. They will ask,
  and it is a week of work.
