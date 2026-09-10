"""One-call setup for notebooks.

Jupyter breaks two assumptions the rest of the codebase makes:

  * `uvicorn.run()` wants to own the event loop, and the kernel already does.
  * The worker is a blocking loop, which would freeze the kernel.

Neither is a real obstacle. FastAPI's `TestClient` exercises the whole
application — middleware, dependencies, error handlers — without a socket, and
the worker's unit of work, `process_one`, is a plain function you can call.

    from easyem.notebook import bootstrap
    nb = bootstrap()

`bootstrap` must run before anything else imports the app, because settings are
cached on first read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Notebook:
    """Handles for everything a notebook needs."""

    client: object          # fastapi.testclient.TestClient
    session_factory: object # sqlalchemy sessionmaker
    outbox: object          # MemoryEmailBackend
    db_path: Path

    def session(self):
        """A fresh session. Close it, or commit it, when you are done."""
        return self.session_factory()

    # -- authentication ----------------------------------------------------

    def signup(self, email: str, password: str = "notebook-password-1234",
               name: str = "Notebook user") -> dict:
        """Sign up and verify in one step, reading the token from the outbox.

        Deliberately goes through the real endpoints rather than editing the
        database: skipping the verification step is how the fact that no email
        was ever sent went unnoticed.
        """
        r = self.client.post("/v1/auth/signup", json={
            "email": email, "password": password, "full_name": name,
        })
        r.raise_for_status()

        mail = self.outbox.last_to(email)
        if mail is None:
            raise RuntimeError("no verification email was sent")
        token = mail.text.split("verify-email?token=")[1].split()[0]
        self.client.post("/v1/auth/verify-email", json={"token": token})

        login = self.client.post("/v1/auth/login",
                                 json={"email": email, "password": password})
        login.raise_for_status()
        return {"headers": {"Authorization":
                            f"Bearer {login.json()['access_token']}"},
                "user": r.json()}

    # -- worker ------------------------------------------------------------

    def work(self, limit: int = 10) -> list:
        """Drain the queue. The worker loop's unit of work, called by hand.

        Running `Worker().run()` in a cell would block the kernel forever.
        """
        from easyem.jobs import worker

        done = []
        for _ in range(limit):
            job_id = worker.process_one("notebook")
            if job_id is None:
                break
            done.append(job_id)
        return done

    def reset_limits(self) -> None:
        """Rate limits are process-wide and a notebook re-runs cells."""
        from easyem.api.ratelimit import limiter

        limiter.reset()


def bootstrap(db_path: str | Path = "easyem-notebook.db",
              *, fresh: bool = True) -> Notebook:
    """Configure, migrate and return a ready-to-use handle.

    Safe to re-run in a notebook, which is less trivial than it sounds: on
    Windows an open file cannot be deleted, so a naive `unlink()` fails the
    moment the cell is run twice. `fresh` therefore drops the tables rather
    than the file — same result, no dependence on filesystem semantics.
    """
    path = Path(db_path).resolve()

    os.environ.setdefault("ENVIRONMENT", "dev")
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{path}"
    os.environ["EMAIL_BACKEND"] = "memory"

    # Settings are cached on first read; a notebook re-running this cell must
    # not silently keep the previous database.
    from easyem.config import get_settings
    get_settings.cache_clear()

    # Release any engine left over from an earlier run in this kernel,
    # otherwise the old connection pool keeps pointing at the old file.
    _dispose_previous_engine()

    from easyem import models  # noqa: F401  registers tables
    from easyem.db import Base, SessionLocal, engine

    if fresh:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    from easyem.notifications import MemoryEmailBackend, set_mailer
    outbox = MemoryEmailBackend()
    set_mailer(outbox)

    from fastapi.testclient import TestClient

    from easyem.main import app
    client = TestClient(app)

    return Notebook(
        client=client,
        session_factory=SessionLocal,
        outbox=outbox,
        db_path=path,
    )


def _dispose_previous_engine() -> None:
    import sys

    module = sys.modules.get("easyem.db")
    if module is None:
        return
    try:
        module.engine.dispose()
    except Exception:
        pass
