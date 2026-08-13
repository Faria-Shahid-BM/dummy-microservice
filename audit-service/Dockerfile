FROM python:3.12-slim
WORKDIR /app
RUN pip install fastapi uvicorn httpx python-multipart sqlalchemy PyJWT cryptography
COPY security.py .
COPY audit-service/main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]