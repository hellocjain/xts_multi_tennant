#!/bin/bash
# =====================================================================
# XTS MULTI-TENANT ENTERPRISE CLI SUITE
# =====================================================================
set -e

SCRIPT_DIR="/opt/xts_multi"

# 1. xts-clients
sudo tee /usr/local/bin/xts-clients > /dev/null << 'EOF'
#!/bin/bash
docker ps --filter "name=xts_client_" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
EOF

# 2. xts-status
sudo tee /usr/local/bin/xts-status > /dev/null << 'EOF'
#!/bin/bash
CLIENT_ID=$1
if [ -z "$CLIENT_ID" ]; then
    echo "=== 📊 ALL ACTIVE CLIENT STATUS ==="
    for c in $(docker ps --filter "name=xts_client_" --format "{{.Names}}"); do
        echo "--- Container: $c ---"
        docker exec "$c" curl -s http://127.0.0.1:8000/health | python3 -m json.tool || true
    done
else
    CONTAINER="xts_client_$CLIENT_ID"
    docker exec "$CONTAINER" curl -s http://127.0.0.1:8000/health | python3 -m json.tool
fi
EOF

# 3. xts-positions
sudo tee /usr/local/bin/xts-positions > /dev/null << 'EOF'
#!/bin/bash
CLIENT_ID=$1
if [ -z "$CLIENT_ID" ]; then
    echo "Usage: xts-positions <client_id>"
    exit 1
fi
CONTAINER="xts_client_$CLIENT_ID"
docker exec "$CONTAINER" python3 -c "import xts_api, json; print(json.dumps(xts_api.get_positions_telemetry(), indent=2))"
EOF

# 4. xts-mtm
sudo tee /usr/local/bin/xts-mtm > /dev/null << 'EOF'
#!/bin/bash
docker exec xts_portal python3 -c "
import asyncio, telemetry_service
async def main():
    res = await telemetry_service.aggregate_all_telemetry()
    s = res['summary']
    print('\n========================= 📈 LIVE MULTI-TENANT MTM SUMMARY =========================')
    print(f'Total Clients: {s[\"total_clients\"]} | Active: {s[\"active_clients\"]} | Healthy: {s[\"healthy_clients\"]}')
    print(f'Total Unrealized MTM: ₹{s[\"total_unrealized_mtm\"]:,.2f}')
    print(f'Total Realized P&L:  ₹{s[\"total_realized_pnl\"]:,.2f}')
    print(f'TOTAL NET PORTFOLIO MTM: ₹{s[\"total_net_mtm\"]:,.2f}')
    print('-------------------------------------------------------------------------------------')
    print(f'{\"CLIENT ID\":<16} {\"STATUS\":<10} {\"MODE\":<6} {\"POSITIONS\":<10} {\"LIVE MTM (₹)\"}')
    print('-' * 85)
    for c in res['clients']:
        mode = 'PAPER' if c.get('paper_mode') else 'LIVE'
        print(f'{c[\"id\"]: <16} {c[\"status\"]: <10} {mode: <6} {c[\"positions_count\"]: <10} ₹{c[\"net_mtm\"]:,.2f}')
    print('=====================================================================================\n')
asyncio.run(main())
"
EOF

# 5. xts-panic
sudo tee /usr/local/bin/xts-panic > /dev/null << 'EOF'
#!/bin/bash
CLIENT_ID=$1
if [ -z "$CLIENT_ID" ]; then
    echo "Usage: xts-panic <client_id>"
    exit 1
fi
CONTAINER="xts_client_$CLIENT_ID"
echo "🚨 INITIATING EMERGENCY SQUARE-OFF FOR $CLIENT_ID..."
docker exec "$CONTAINER" python3 -c "import xts_api, json; print(json.dumps(xts_api.panic_square_off_all(), indent=2))"
EOF

# 6. xts-panic-all
sudo tee /usr/local/bin/xts-panic-all > /dev/null << 'EOF'
#!/bin/bash
read -p "⚠️ CRITICAL: Are you sure you want to SQUARE OFF ALL CLIENTS? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted."
    exit 0
fi
echo "🚨 INITIATING GLOBAL PANIC SWEEP ACROSS ALL CLIENT CONTAINERS..."
docker exec xts_portal python3 -c "import asyncio, telemetry_service, json; print(json.dumps(asyncio.run(telemetry_service.panic_all_active_clients()), indent=2))"
EOF

# 7. xts-logs
sudo tee /usr/local/bin/xts-logs > /dev/null << 'EOF'
#!/bin/bash
CLIENT_ID=$1
if [ -z "$CLIENT_ID" ]; then
    echo "Usage: xts-logs <client_id> [-f]"
    exit 1
fi
CONTAINER="xts_client_$CLIENT_ID"
shift || true
docker logs "$CONTAINER" "$@"
EOF

# 8. xts-backup
sudo tee /usr/local/bin/xts-backup > /dev/null << 'EOF'
#!/bin/bash
echo "📦 Initiating Hot SQLite Multi-Tenant Backup..."
docker exec xts_portal python3 /opt/xts_multi/backup/backup_engine.py || python3 /opt/xts_multi/backup/backup_engine.py
EOF

# 9. xts-admin-reset-2fa
sudo tee /usr/local/bin/xts-admin-reset-2fa > /dev/null << 'EOF'
#!/bin/bash
echo "🚨 Initiating Host Emergency 2FA Break-Glass Tool..."
python3 /opt/xts_multi/portal/scripts/break_glass.py "$@"
EOF

# 10. xts-dr-restore
sudo tee /usr/local/bin/xts-dr-restore > /dev/null << 'EOF'
#!/bin/bash
python3 /opt/xts_multi/backup/dr_restore.py "$@"
EOF

# 11. xts-warmup
sudo tee /usr/local/bin/xts-warmup > /dev/null << 'EOF'
#!/bin/bash
echo "☀️ Running Rolling Master Cache Warmup for all active clients..."
docker exec xts_portal python3 -c "
import asyncio, scheduler, json
async def main():
    res = await scheduler.run_rolling_cache_warmup(delay_between_batches_sec=3.0)
    print(json.dumps(res, indent=2))
asyncio.run(main())
"
EOF

# 12. xts-test
sudo tee /usr/local/bin/xts-test > /dev/null << 'EOF'
#!/bin/bash
CLIENT_ID=$1
SYMBOL=${2:-"MCX:CRUDEOIL1!"}
ACTION=${3:-"BUY"}
QTY=${4:-1}
PRICE=${5:-6500.0}

if [ -z "$CLIENT_ID" ]; then
    echo "Usage: xts-test <client_id> [symbol] [action] [quantity] [price]"
    echo "Example: xts-test c01 MCX:CRUDEOIL1! BUY 1 6500.0"
    exit 1
fi

docker exec xts_portal python3 -c "
import sqlite3, requests, json, security
with sqlite3.connect('/opt/xts_multi/portal/portal.db') as conn:
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?', ('$CLIENT_ID',)).fetchone()
    if not row:
        print('❌ Client $CLIENT_ID not found in database')
        exit(1)
    creds = security.decrypt_credentials(row['encrypted_payload'])
    secret = creds.get('WEBHOOK_SECRET', '')

payload = {
    'secret': secret,
    'action': '$ACTION',
    'symbol': '$SYMBOL',
    'quantity': int('$QTY'),
    'price': float('$PRICE')
}
print(f'📤 Dispatching Test Webhook for client $CLIENT_ID -> $ACTION $QTY x $SYMBOL @ Rs $PRICE')
try:
    r = requests.post(f'http://xts_client_$CLIENT_ID:8000/webhook', json=payload, timeout=5)
    print(f'📥 Response: {r.status_code} | {r.text}')
except Exception as e:
    print(f'❌ Failed: {e}')
"
# 13. xts-verify
sudo tee /usr/local/bin/xts-verify > /dev/null << 'EOF'
#!/bin/bash
python3 /opt/xts_multi/scripts/verify_new_server.py "$@"
EOF

sudo chmod +x /usr/local/bin/xts-*
echo "✅ Multi-Tenant CLI Suite installed in /usr/local/bin/xts-*"

