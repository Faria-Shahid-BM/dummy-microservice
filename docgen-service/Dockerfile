# syntax=docker/dockerfile:1
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app/backend
COPY docgen-service/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
# Shared modules used by every other backend service (security.py's JWT
# verification, engines/'s extraction/valuation/insurance/collateral/policy_qa
# logic) — copied in so docgen imports the single shared copy instead of its
# own fork, which had already drifted out of sync (see app/engines/docgen/
# for the pipeline stages that ARE genuinely docgen-specific and stay local).
# audit_client.py/outbox.py: docgen's audit trail now reports into the
# central audit-service like every other service (see app/audit.py).
COPY security.py .
COPY audit_client.py .
COPY config_client.py .
COPY outbox.py .
COPY engines/ ./engines/
COPY docgen-service/ /app/backend/
RUN useradd --create-home appuser && mkdir -p /app/backend/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
