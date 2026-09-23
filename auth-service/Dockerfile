# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/pip pip install fastapi uvicorn pyjwt cryptography argon2-cffi
COPY main.py .
COPY rehash_seed_passwords.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]