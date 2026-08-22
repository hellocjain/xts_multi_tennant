FROM python:3.11-slim

LABEL maintainer="XTS Multi-Tenant Engine"
LABEL version="10.0-PRO"

# Install system dependencies & timezone data
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ="Asia/Kolkata"

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY config.py xts_api.py main.py supertrend_engine.py ./

# Create default data mount directory
RUN mkdir -p /app/data

ENV DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
