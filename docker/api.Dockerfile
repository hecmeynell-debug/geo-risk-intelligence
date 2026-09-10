# Shared image for the api and worker services -- same code, different entry points.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     PIP_NO_CACHE_DIR=1     PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update  && apt-get install -y --no-install-recommends curl  && rm -rf /var/lib/apt/lists/*

# Dependency layer first so source edits do not invalidate the install.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[dev]"

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY tests ./tests

# Run as a non-root user.
RUN useradd --create-home --uid 10001 gri && chown -R gri:gri /app
USER gri

EXPOSE 8000

CMD ["uvicorn", "gri.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
