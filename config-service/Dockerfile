# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/pip pip install fastapi uvicorn httpx sqlalchemy PyJWT cryptography
COPY security.py .
COPY audit_client.py .
COPY outbox.py .
COPY config-service/main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
