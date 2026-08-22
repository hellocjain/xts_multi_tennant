#!/usr/bin/env bash
# =====================================================================
# XTS Multi-Tenant Algorithmic Trading Platform — Master Deploy Script
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "🚀 [DEPLOY] Starting XTS Multi-Tenant Deployment from ${ROOT_DIR}..."

cd "${ROOT_DIR}"

# 1. Build Client Bot Image
echo "🔨 [1/3] Building xts_bot:latest client image..."
docker build -t xts_bot:latest "${ROOT_DIR}/client"

# 2. Build & Update Portal + Caddy
echo "🔨 [2/3] Building and recreating xts_portal service..."
docker compose build xts_portal
docker compose up -d --force-recreate xts_portal

# 3. Synchronize All Active Client Containers via docker_manager.py
echo "🔄 [3/3] Synchronizing client containers through docker_manager.py..."
docker exec xts_portal python3 -c '
import database, docker_manager, caddy_manager, logging
logger = logging.getLogger("deploy")

with database.get_db_connection() as conn:
    tenants = [dict(r) for r in conn.execute("SELECT id, name, status FROM tenants WHERE status=\"ACTIVE\"").fetchall()]

print(f"Found {len(tenants)} active tenants to sync.")
for t in tenants:
    t_id = t["id"]
    res = docker_manager.provision_client_container(t_id)
    print(f"  • Tenant {t_id} ({t[\"name\"]}): {res.get(\"status\")}")

caddy_ok = caddy_manager.sync_caddy_config()
print(f"Caddy Ingress Sync: {caddy_ok}")
'

echo "✅ [DEPLOY] Deployment completed successfully!"
