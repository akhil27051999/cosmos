# A simple, readable image for Stage 0. (Multi-stage + distroless hardening
# comes in the containerization module later — kept plain here on purpose.)
FROM python:3.12-slim

# - PYTHONDONTWRITEBYTECODE: no .pyc clutter in the image
# - PYTHONUNBUFFERED: logs stream immediately (important for container logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first (their own layer) so code changes don't re-run pip install.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then the application code.
COPY app ./app

EXPOSE 8000

# 0.0.0.0 so the port is reachable from outside the container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
