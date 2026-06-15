FROM python:3.11-slim

WORKDIR /app

# Install only dashboard deps (fast, no bot deps)
RUN pip install --no-cache-dir fastapi uvicorn sse-starlette

# Copy the dashboard source
COPY main.py .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
