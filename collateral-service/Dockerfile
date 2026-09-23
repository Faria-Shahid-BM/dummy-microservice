# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
COPY engines/ ./engines/
COPY provider.py .
COPY security.py .
COPY streaming.py .
COPY case_store.py .
COPY audit_client.py .
COPY config_client.py .
COPY outbox.py .
COPY collateral-service/main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
