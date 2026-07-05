# Boarding-gate verification server (FastAPI).
# Postgres runs as a separate compose service; this image is just the app.
FROM python:3.13-slim

WORKDIR /app

# Install deps first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code only. The project root doubles as a host venv (Scripts/, Lib/,
# Include/), so those are excluded via .dockerignore — copy just src/.
COPY src ./src

EXPOSE 8001

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001"]
