.PHONY: up down logs migrate revision test lint fmt worker api reconcile maintenance

up:            ## build, start, and migrate. The only command you need.
	docker compose up -d --build
	@echo "waiting for postgres..."
	@until docker compose exec -T db pg_isready -U easyem >/dev/null 2>&1; do sleep 1; done
	docker compose exec -T api alembic upgrade head
	@echo ""
	@echo "  API    http://localhost:8000/docs"
	@echo "  logs   make logs"
	@echo ""
	@echo "  In dev, EMAIL_BACKEND=console: verification links are printed"
	@echo "  in the api logs. That is where you get your first token."

down:
	docker compose down

logs:          ## follow api + worker
	docker compose logs -f api worker

migrate:
	alembic upgrade head

revision:      ## make revision m="add filters"
	alembic revision --autogenerate -m "$(m)"

api:           ## run the API locally (needs a reachable DATABASE_URL)
	uvicorn easyem.main:app --reload --port 8000

worker:        ## run a worker locally
	python -m easyem.jobs.worker

test:
	pytest -q

lint:
	ruff check easyem tests

fmt:
	ruff check --fix easyem tests

reconcile:     ## every wallet balance against its ledger; non-zero exit on drift
	python -c "from easyem.db import SessionLocal; from easyem.credits.service import reconcile; \
	           s=SessionLocal(); d=reconcile(s); print(d or 'ledger consistent'); \
	           raise SystemExit(1 if d else 0)"

maintenance:   ## reap stalled jobs, expire holds, purge sessions
	python -c "from easyem.jobs.worker import run_maintenance; print(run_maintenance())"
