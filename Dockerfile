# Invincible gateway container (Phase 16 compose pair).
FROM python:3.12-slim

WORKDIR /app

# pip hardening for the build step: a transient PyPI blip inside the
# builder must not fail the deploy (2026-09-22: the build-dep download
# broke mid-stream -> BrokenPipeError -> deploy failed).
ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

# Install the build backend as its own cacheable layer, ABOVE the app
# COPYs. The pin is independent of app files, so this layer survives every
# source and metadata change - pip never re-downloads setuptools into a
# throwaway env on a rebuild (the step that failed on Railway 2026-09-22).
RUN pip install --no-cache-dir "setuptools>=77"

# README.md and LICENSE back pyproject.toml's PEP 639 metadata
# (readme = "README.md", license-files = ["LICENSE"]).
COPY pyproject.toml README.md LICENSE ./
COPY invincible ./invincible

# --no-build-isolation reuses the setuptools installed above instead of
# spinning up a throwaway build env that would re-download it.
RUN pip install --no-cache-dir --no-build-isolation .

EXPOSE 8000
# Migrations run as the schema-owner role when INVINCIBLE_MIGRATE_DB_URL is
# set (two-role production topology); otherwise the runtime DSN is used, as
# in single-role/self-host deploys. The assignment is scoped to `db upgrade`
# only - uvicorn always serves on INVINCIBLE_DB_URL.
CMD ["sh", "-c", "INVINCIBLE_DB_URL=\"${INVINCIBLE_MIGRATE_DB_URL:-$INVINCIBLE_DB_URL}\" invincible db upgrade && uvicorn invincible.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
