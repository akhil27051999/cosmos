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

# Stage 7 part 2: generate the gRPC stubs from the .proto contract at build time
# (so generated code isn't committed to git). Output lands in app/adapters/grpc/gen,
# which the gRPC adapter puts on sys.path at runtime.
COPY proto ./proto
RUN mkdir -p app/adapters/grpc/gen && \
    python -m grpc_tools.protoc -I proto \
      --python_out=app/adapters/grpc/gen \
      --grpc_python_out=app/adapters/grpc/gen \
      proto/worker_control.proto

# 8000 = HTTP API (gateway); 50051 = gRPC control plane (worker).
EXPOSE 8000 50051

# 0.0.0.0 so the port is reachable from outside the container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
