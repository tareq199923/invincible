# Invincible gateway container (Phase 16 compose pair).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY invincible ./invincible
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["sh", "-c", "invincible db upgrade && uvicorn invincible.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
