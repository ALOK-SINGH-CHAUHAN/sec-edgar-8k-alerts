# Production Dockerfile for SEC EDGAR 8-K Alert Poller
FROM python:3.11-slim

# Prevent Python from writing .pyc and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install runtime ca-certificates and security updates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root user
RUN groupadd -g 1000 edgar && \
    useradd -u 1000 -g edgar -s /bin/bash -m edgar

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Create and grant permissions for data and cache directories
RUN mkdir -p /app/data /app/data/cache && \
    chown -R edgar:edgar /app

USER edgar

VOLUME ["/app/data"]

ENTRYPOINT ["python", "main.py"]
CMD ["run"]
