FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]"

COPY . .

# Do not run as root.
RUN useradd -m app && chown -R app:app /app
USER app

CMD ["uvicorn", "easyem.main:app", "--host", "0.0.0.0", "--port", "8000"]
