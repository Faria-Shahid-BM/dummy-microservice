FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY common/claims.py .
COPY common/audit.py .
COPY common/llm_provider.py .
COPY collateral-reviewer/util.py .
COPY collateral-reviewer/extraction.py .
COPY collateral-reviewer/engine.py .
COPY collateral-reviewer/prompts ./prompts
COPY collateral-reviewer/main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
