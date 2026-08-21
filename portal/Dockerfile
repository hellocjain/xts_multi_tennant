FROM python:3.11-slim

LABEL maintainer="XTS Multi-Tenant Portal"
LABEL version="10.0-PRO"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORTAL_DATA_DIR=/opt/xts_multi/portal

EXPOSE 8500

HEALTHCHECK --interval=20s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8500/admin/login || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8500", "--workers", "1", "--proxy-headers"]
