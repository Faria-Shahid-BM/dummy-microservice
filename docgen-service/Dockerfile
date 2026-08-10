FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app/backend
COPY docgen-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY docgen-service/ /app/backend/
RUN useradd --create-home appuser && mkdir -p /app/backend/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
