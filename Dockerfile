# Invincible gateway container (Phase 16 compose pair).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY invincible ./invincible
RUN pip install --no-cache-dir .

EXPOSE 8000
# Migrations run as the schema-owner role when INVINCIBLE_MIGRATE_DB_URL is
# set (two-role production topology); otherwise the runtime DSN is used, as
# in single-role/self-host deploys. The assignment is scoped to `db upgrade`
# only - uvicorn always serves on INVINCIBLE_DB_URL.
CMD ["sh", "-c", "INVINCIBLE_DB_URL=\"${INVINCIBLE_MIGRATE_DB_URL:-$INVINCIBLE_DB_URL}\" invincible db upgrade && uvicorn invincible.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
