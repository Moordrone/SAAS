"""Worker tests, and the end-to-end flow that bypasses nothing.

The audit found that no email was ever sent, and the smoke test had missed it
because it edited the database to fake verification. A test that skips a step of
the journey does not test the journey. `test_a_stranger_can_go_from_signup_to_
results` goes through every door a real user goes through.
"""

from decimal import Decimal

import pytest

from easyem.credits import service as credits
from easyem.identity import service as identity
from easyem.jobs import service as jobs
from easyem.jobs import worker
from easyem.models import ExecutionStatus, SettlementStatus
from easyem.projects import service as projects
from easyem.projects.schema import empty_definition, set_parameter

PASSWORD = "correct-horse-battery-staple"
DEFINITION = {
    "schema_version": "1.0.0",
    "component": {"family": "Antennas", "type": "RectangularPatch"},
    "parameters": {
        "frequency_center": {"value": 2.45, "unit": "GHz", "provenance": "user"},
        "substrate_material": {"value": "RO4003C", "provenance": "user"},
        "substrate_height": {"value": 0.813, "unit": "mm", "provenance": "user"},
    },
}


@pytest.fixture()
def user(db):
    u, token = identity.signup(
        db, email="w@example.com", password=PASSWORD, full_name="W"
    )
    identity.verify_email(db, token)
    return u


@pytest.fixture()
def project(db, user):
    p = projects.create_project(
        db, user, name="patch", component_type="RectangularPatch", family="Antennas"
    )
    d = empty_definition("RectangularPatch", "Antennas")
    d = set_parameter(d, "frequency_center", 2.45, "GHz")
    d = set_parameter(d, "substrate_material", "RO4003C")
    d = set_parameter(d, "substrate_height", 0.813, "mm")
    projects.update_definition(db, p, user, d)
    return p


# --- claiming -------------------------------------------------------------

def test_claim_takes_a_queued_job(db, user, project):
    job = jobs.submit(db, project, user)
    db.commit()

    claimed = worker.claim_next_job(db, "worker-1")

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.execution_status is ExecutionStatus.running
    assert claimed.worker_id == "worker-1"
    assert claimed.attempt == 1


def test_a_job_is_claimed_only_once(db, user, project):
    """Two workers, one job. The guarded UPDATE decides."""
    jobs.submit(db, project, user)
    db.commit()

    first = worker.claim_next_job(db, "worker-1")
    second = worker.claim_next_job(db, "worker-2")

    assert first is not None
    assert second is None


def test_claim_returns_none_when_idle(db):
    assert worker.claim_next_job(db, "worker-1") is None


def test_higher_priority_is_claimed_first(db, user, project):
    """The Professional plan promises priority queueing."""
    low = jobs.submit(db, project, user, idempotency_key="low")
    high = jobs.submit(db, project, user, idempotency_key="high")
    high.priority = 10
    db.commit()

    assert worker.claim_next_job(db, "w1").id == high.id
    assert worker.claim_next_job(db, "w2").id == low.id


def test_claimed_job_records_a_heartbeat(db, user, project):
    jobs.submit(db, project, user)
    db.commit()
    claimed = worker.claim_next_job(db, "worker-1")
    assert claimed.heartbeat_at is not None
    assert claimed.started_at is not None


# --- execution ------------------------------------------------------------

def test_execute_settles_a_successful_job(db, user, project):
    before = credits.get_or_create_wallet(db, user.default_account_id).balance
    jobs.submit(db, project, user)
    db.commit()

    job = worker.execute(db, worker.claim_next_job(db, "w1"))

    assert job.execution_status is ExecutionStatus.succeeded
    assert job.settlement_status is SettlementStatus.settled
    balance = credits.get_or_create_wallet(db, user.default_account_id).balance
    assert balance == before - job.cost_actual
    assert credits.reconcile(db) == []


def test_execute_stores_results(db, user, project):
    jobs.submit(db, project, user)
    db.commit()
    job = worker.execute(db, worker.claim_next_job(db, "w1"))
    assert jobs.get_results(db, job) is not None


def test_finished_job_emails_the_customer(db, user, project, outbox):
    jobs.submit(db, project, user)
    db.commit()
    worker.execute(db, worker.claim_next_job(db, "w1"))

    mail = outbox.last_to("w@example.com")
    assert mail is not None
    assert "finished" in mail.subject.lower()


def test_a_failing_notification_does_not_break_the_job(db, user, project, monkeypatch):
    """A mail outage must not lose a simulation the customer paid for."""
    def boom(*_a, **_k):
        raise RuntimeError("mail server down")

    monkeypatch.setattr("easyem.notifications.send", boom)
    jobs.submit(db, project, user)
    db.commit()

    job = worker.execute(db, worker.claim_next_job(db, "w1"))
    assert job.execution_status is ExecutionStatus.succeeded


def test_a_solver_that_explodes_refunds_the_customer(db, user, project, monkeypatch):
    """A crash inside the backend is our fault, so the hold comes back whole."""
    before = credits.get_or_create_wallet(db, user.default_account_id).balance
    jobs.submit(db, project, user)
    db.commit()
    claimed = worker.claim_next_job(db, "w1")

    from easyem.solver.registry import get_backend

    backend = get_backend(claimed.backend)
    monkeypatch.setattr(
        backend, "submit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("solver segfaulted")),
    )

    result = worker.execute(db, claimed)

    assert result.execution_status is ExecutionStatus.failed
    assert result.settlement_status is SettlementStatus.released
    assert credits.get_or_create_wallet(db, user.default_account_id).balance == before
    assert credits.reconcile(db) == []


# --- process_one ----------------------------------------------------------

def test_process_one_is_a_noop_when_idle(db):
    assert worker.process_one("w1") is None


# --- maintenance ----------------------------------------------------------

def test_maintenance_reports_a_clean_ledger(db, user, project):
    jobs.submit(db, project, user)
    db.commit()
    worker.execute(db, worker.claim_next_job(db, "w1"))
    db.commit()
    assert credits.reconcile(db) == []


# --- idempotency race -----------------------------------------------------

def test_the_loser_of_an_idempotency_race_gets_the_winners_job(db, user, project):
    """Two concurrent requests with one key both pass the existence check,
    because neither transaction can see the other's row. The UNIQUE constraint
    settles it — and the loser must receive the winner's job, not a 500.
    """
    from sqlalchemy.exc import IntegrityError

    first = jobs.submit(db, project, user, idempotency_key="RACE")
    db.commit()

    original_scalar = db.scalar
    calls = {"n": 0}

    def blind_first_lookup(statement, *a, **k):
        # Simulate the racing transaction: the existence check sees nothing.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_scalar(statement, *a, **k)

    db.scalar = blind_first_lookup
    try:
        second = jobs.submit(db, project, user, idempotency_key="RACE")
    except IntegrityError:
        pytest.fail("the racing request received an error instead of the job")
    finally:
        db.scalar = original_scalar

    assert second.id == first.id


# --- the whole journey ----------------------------------------------------

def test_a_stranger_can_go_from_signup_to_results(client, db, outbox):
    """No shortcuts. Every step goes through the door a user goes through.

    This is the test that would have caught the fact that no verification email
    was ever sent, which made the product unusable while 203 other tests passed.
    """
    email = "stranger@example.com"

    # 1. Sign up.
    r = client.post("/v1/auth/signup", json={
        "email": email, "password": PASSWORD, "full_name": "Stranger",
    })
    assert r.status_code == 201
    assert r.json()["email_verified"] is False

    # 2. Read the token out of the email that was actually sent.
    mail = outbox.last_to(email)
    assert mail is not None, "no verification email was sent"
    token = mail.text.split("verify-email?token=")[1].split()[0]

    # 3. Verify.
    r = client.post("/v1/auth/verify-email", json={"token": token})
    assert r.status_code == 200
    assert r.json()["email_verified"] is True

    # 4. Log in.
    r = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # 5. Create a project and give it a design.
    r = client.post("/v1/projects", json={
        "name": "First patch", "component_type": "RectangularPatch",
    }, headers=headers)
    assert r.status_code == 201
    pid = r.json()["id"]

    r = client.put(f"/v1/projects/{pid}/definition",
                   json={"definition": DEFINITION}, headers=headers)
    assert r.status_code == 200 and r.json()["is_valid"] is True

    # 6. Quote, then run.
    r = client.post(f"/v1/projects/{pid}/simulations/quote", json={},
                    headers=headers)
    assert r.json()["sufficient_credits"] is True

    r = client.post(f"/v1/projects/{pid}/simulations", json={},
                    headers=headers)
    assert r.status_code == 201
    job = r.json()
    # The request returns immediately: queued, not finished.
    assert job["execution_status"] == "queued"
    assert job["settlement_status"] == "reserved"

    # 7. A worker picks it up.
    worker.execute(db, worker.claim_next_job(db, "test-worker"))

    r = client.get(f"/v1/simulations/{job['id']}", headers=headers)
    assert r.json()["execution_status"] == "succeeded"

    r = client.get(f"/v1/simulations/{job['id']}/results", headers=headers)
    body = r.json()
    assert body["available"] is True
    assert body["demonstration_only"] is False
    assert 0.040 < body["results"]["scalars"]["patch_width_m"] < 0.043

    # 8. And they were charged, once.
    r = client.get("/v1/credits/balance", headers=headers)
    assert Decimal(r.json()["available"]) < Decimal("50")


def test_the_request_does_not_wait_for_the_solver(client, db):
    """The audit's worst finding: the whole solve used to run inside the
    request's transaction, holding a lock on the wallet throughout."""
    email = "async@example.com"
    client.post("/v1/auth/signup", json={
        "email": email, "password": PASSWORD, "full_name": "A",
    })
    identity.verify_email(
        db, _token_for(db, email)
    )
    r = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    pid = client.post("/v1/projects", json={
        "name": "p", "component_type": "RectangularPatch",
    }, headers=headers).json()["id"]
    client.put(f"/v1/projects/{pid}/definition",
               json={"definition": DEFINITION}, headers=headers)

    job = client.post(f"/v1/projects/{pid}/simulations", json={},
                      headers=headers).json()

    assert job["execution_status"] == "queued"
    assert job["cost_actual"] is None      # nothing has run yet
    assert job["progress"] == 0.0


def _token_for(db, email):
    from easyem.notifications import get_mailer

    mail = get_mailer().last_to(email)
    return mail.text.split("verify-email?token=")[1].split()[0]
