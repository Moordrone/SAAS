# Running a real openEMS simulation

Everything is in place. The adapter generates the script, runs it as a
subprocess, reads the results, and reports a crash the customer can act on. All
of that is tested. The one thing no test can prove — that openEMS itself accepts
the generated geometry — needs a real openEMS, and this is how you get one.

## What is and isn't done

| Piece | Status |
|---|---|
| Script generation | done, tested against a stand-in openEMS |
| Subprocess execution, disk state, crash reporting | done |
| Adapter reads results and error reasons | done |
| openEMS compiled and installed | **this guide** |
| Frontend calling real simulations | not yet — a separate step |

The frontend on `:3000` computes the patch in the browser and will not change
when openEMS starts working. Wiring it to real simulations is deliberate future
work.

## Build and run

The worker has its own image (`Dockerfile.worker`) with openEMS compiled from
source. The API image stays small — only the worker runs the solver.

```bash
docker compose up -d --build
```

The first build is slow: openEMS pulls in CSXCAD, fparser, QCSXCAD and the
Python bindings, and compiles them. Expect 10–15 minutes. It is cached
afterwards, so later builds are fast unless you bump `OPENEMS_REF`.

Watch it come up:

```bash
docker compose logs -f worker
```

You are looking for `openEMS OK` during the build (the image refuses to finish
without it) and `worker_started` at runtime.

## First real run

Once the stack is up, drive one simulation through the API. From the notebook or
any HTTP client:

```python
import requests
API = "http://localhost:8000"

# sign up, verify (dev prints the link in the api logs), log in
requests.post(f"{API}/v1/auth/signup", json={
    "email": "you@lab.example", "password": "a-long-enough-password",
    "full_name": "You"})
# ... verify via the link in `docker compose logs api`, then:
token = requests.post(f"{API}/v1/auth/login", json={
    "email": "you@lab.example", "password": "a-long-enough-password"
}).json()["access_token"]
H = {"Authorization": f"Bearer {token}"}

# a project with a definition
pid = requests.post(f"{API}/v1/projects", headers=H, json={
    "name": "First real run", "component_type": "RectangularPatch"}).json()["id"]
requests.put(f"{API}/v1/projects/{pid}/definition", headers=H, json={"definition": {
    "schema_version": "1.0.0",
    "component": {"family": "Antennas", "type": "RectangularPatch"},
    "parameters": {
        "frequency_center": {"value": 2.45, "unit": "GHz"},
        "substrate_material": {"value": "RO4003C"},
        "substrate_height": {"value": 0.813, "unit": "mm"}}}})

# run it ON openEMS, not the analytical backend
job = requests.post(f"{API}/v1/projects/{pid}/simulations", headers=H,
                    json={"backend": "openems"}).json()
print(job["execution_status"])   # queued — the worker takes it from here
```

Then poll `GET /v1/simulations/{job_id}` until it is `succeeded`, and read
`GET /v1/simulations/{job_id}/results`. A patch takes a couple of minutes.

## The comparison that matters

Run the same design on both backends and compare the resonant frequency:

```python
for backend in ("analytical", "openems"):
    job = requests.post(f"{API}/v1/projects/{pid}/simulations", headers=H,
                        json={"backend": backend}).json()
    # ... wait for it ...
    r = requests.get(f"{API}/v1/simulations/{job['id']}/results", headers=H).json()
    f = r["results"]["scalars"]["resonant_frequency_hz"] / 1e9
    print(f"{backend:12} resonates at {f:.4f} GHz")
```

Two independent methods agreeing within a few percent is cross-validation. A
gap of 10% means one of them is wrong, and it is far better to find that here
than in front of a customer.

Expect openEMS to sit slightly below the analytical model. The analytical
result uses an inset feed; the generated openEMS script uses a lumped port,
which adds a small series inductance the cavity model does not include. That
shifts the resonance down and makes the match shallower. It is a real
difference between two feed structures, not a bug — but it is worth confirming
you see it, and roughly the size you expect.

## If openEMS rejects the geometry

The script wraps its body in a try/except that writes `error.json` with the
real exception. The adapter surfaces that message, so a failed job will tell you
what openEMS objected to rather than a bare "internal error". The most likely
first-run issues:

- A boundary or port call whose signature changed between openEMS versions.
  `Dockerfile.worker` pins `v0.0.36`; if you build a different version and a
  call fails, the traceback in `error.json` names the method.
- Mesh too fine for the container's memory. The estimate is in the job's
  `plan.json`; drop `accuracy` to `draft` to shrink it while debugging.

When you get a resonant frequency out of openEMS, send it over with the
analytical number and I will tell you whether the gap is the expected
feed-structure shift or something to investigate.
