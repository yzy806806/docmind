# ── Stage 1: builder ─────────────────────────────────────────────
# Installs uv, resolves & locks dependencies, builds the wheel.
FROM python:3.11-slim AS builder

ENV \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_INSTALL_DIR=/opt/uv

# Install uv (pinned version for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:0.7.12 /uv /usr/local/bin/uv

WORKDIR /app

# Copy only dependency manifests first for better layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies into a virtual-env in /opt/venv.
# --frozen ensures we use the locked versions from uv.lock exactly.
# --no-dev skips test/lint deps; --extra embeddings pulls sentence-transformers + numpy.
RUN uv venv /opt/venv && \
    uv sync --frozen --no-dev --extra embeddings --no-install-project

# Copy the project source and install the package itself
COPY src/ ./src/
COPY README.md ./

RUN uv sync --frozen --no-dev --extra embeddings --no-editable


# ── Stage 2: runtime ────────────────────────────────────────────
# Slim runtime image with only the virtualenv and application code.
FROM python:3.11-slim AS runtime

ENV \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DOCMIND_HOST=0.0.0.0 \
    DOCMIND_PORT=8000

# Minimal runtime system deps: libpq5 is not needed (no psycopg in deps),
# but libxml2/libxslt are needed by beautifulsoup4 lxml fallback and
# pdfplumber may need libjpeg for some PDFs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
        libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

# Copy the virtual-env from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy application source
COPY --from=builder /app/src /app/src
COPY --from=builder /app/README.md /app/README.md

# Create writable directories for data and config (will be overridden by volume mounts)
RUN mkdir -p /app/data /app/config

WORKDIR /app

# Copy example config as a fallback if none is volume-mounted
COPY config/config.example.yaml /app/config/config.example.yaml

EXPOSE 8000

# Healthcheck: hit the dashboard endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/', timeout=3)" || exit 1

# Run the FastAPI app with uvicorn directly (single worker for container;
# scale horizontally via docker-compose replicas instead)
CMD ["python", "-m", "uvicorn", "src.web.server:app", "--host", "0.0.0.0", "--port", "8000"]
