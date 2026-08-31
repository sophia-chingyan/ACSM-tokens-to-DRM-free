FROM python:3.11-slim

# Build dependencies for libgourou. No OCR toolchain: this app decrypts,
# it never re-renders a page.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake make g++ \
    libpugixml-dev libzip-dev libssl-dev libcurl4-openssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# libmupdf-dev intentionally NOT installed — PyMuPDF bundles its own
# MuPDF and the system package can cause import conflicts / segfaults.

WORKDIR /app

# Clone and build libgourou
RUN git clone --recurse-submodules https://forge.soutade.fr/soutade/libgourou.git /app/libgourou \
    && cd /app/libgourou \
    && make BUILD_UTILS=1 BUILD_STATIC=1 BUILD_SHARED=0 \
    && ls -la /app/libgourou/utils/acsmdownloader

# All mutable state lives under DATA_DIR. Mount a single Railway volume
# at /app/data so converted books AND the Adobe device registration
# survive redeploys.
ENV DATA_DIR=/app/data
ENV ADEPT_DIR=/app/data/.adept

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app.py converter.py ./
COPY templates/ templates/

RUN mkdir -p /app/data/uploads /app/data/output /app/data/covers

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/login || exit 1

# Shell form (as above, so $PORT is expanded by the shell). Validates PORT
# first: falls back to 8080 (with a warning) for an unset, empty, or
# non-numeric value instead of crashing gunicorn's bind parser. Long worker
# timeout covers large book downloads.
CMD case "$PORT" in \
        ''|*[!0-9]*) echo "WARN: PORT='$PORT' is not numeric, defaulting to 8080" >&2; PORT=8080 ;; \
    esac; \
    exec gunicorn app:app --bind 0.0.0.0:$PORT --threads 4 --timeout 1800 --graceful-timeout 30
