# 🚀 XTS Multi-Tenant Enterprise Trading Cluster (V10.0-PRO)

An enterprise-grade, multi-tenant algorithmic trading execution cluster designed for Symphony XTS API (AC Agarwal and other XTS brokers). Features container-per-client micro-isolation, sub-millisecond execution, 2FA TOTP administration, dynamic Caddy ingress routing over Unix socket, automated daily morning warmup (08:30 AM IST), and hot encrypted disaster recovery.

---

## 🌟 Key Architecture & Capabilities

- **Container-per-Client Isolation**: Each client runs inside an isolated micro-container (xts_client_{tenant_id}) with private RAM, token lifecycle, and SQLite WAL database (signals.db).
- **20+ Accounts on a Single Budget VPS**: Ultra-lean runtime footprint (~45MB RAM per client container).
- **Live-Market Tested Execution Engine**:
  - Continuous contract resolution (CRUDEOIL1!, GOLDM1!, NATGAS1!).
  - Specific expiry symbol mapping (CRUDEOIL24AUGFUT).
  - 3-Day MCX/NCDEX auto-rollover protection.
  - Marketable Limit Orders with 0.5% slippage buffer and strict exchange tick-size quantization (0.05 / 1.0).
  - Anti-Duplicate Signal Shield (3-second window) and daily notional turnover caps.
- **Dynamic Caddy Ingress Routing**:
  - Ingests TradingView webhooks per client: http://YOUR_SERVER_IP/webhook/{tenant_id} or https://yourdomain.com/webhook/{tenant_id}.
  - Zero downtime when onboarding new clients via Unix socket live reload.
- **Unified Executive Admin Portal**:
  - 2FA TOTP Google Authenticator protection + Emergency Recovery Codes.
  - Real-time 3-tier P&L dashboard (Net MTM, Realized, Unrealized).
  - Broker RMS Margin gauges and daily turnover utilization bars.
  - 1-Click TradingView Webhook Generator & Modal.
  - 1-Click Single-Client Panic & Global Kill-Switch (Panic Square Off All).
- **Automated Morning Warmup Scheduler**:
  - Automatic rolling batch restart at **08:30 AM IST** (Mon–Fri) to pre-load 18,000+ exchange instruments without broker rate-limiting.
- **Automated Disaster Recovery**:
  - Nightly zero-downtime hot backup (VACUUM INTO) at **23:45 IST** with AES-256 Fernet encryption.
  - 1-Command cold-start restoration on any fresh VPS in under 60 seconds.

---

## ⚡ Quick One-Shot Installation on Ubuntu VPS

### Prerequisites:
- Fresh Ubuntu 22.04 or 24.04 LTS VPS (Minimum: 2 vCPU, 2GB RAM, 20GB SSD).

### 1. Clone Repository & Run Installer:
```bash
# Clone to /opt/xts_multi
sudo git clone https://github.com/hellocjain/xts_multi_tennant.git /opt/xts_multi
cd /opt/xts_multi

# Execute One-Shot Installer
sudo bash setup_multi_tenant.sh
```

### 2. Access Admin Portal:
- Open `http://YOUR_SERVER_IP/admin/login` (or `https://yourdomain.com/admin/login`).
- Login with your Admin credentials and scan the QR code using Google Authenticator / 1Password.

---

## 🛠️ Global Enterprise CLI Suite

Manage and inspect the entire cluster directly from the host terminal:

| Command | Description |
|---|---|
| `xts-status` | Inspect broker session tokens, master cache size, and health for all clients. |
| `xts-mtm` | Live real-time portfolio MTM table across all accounts. |
| `xts-positions <client_id>` | View open positions for a specific client account. |
| `xts-warmup` | Trigger staged rolling cache warmup across all active clients. |
| `xts-panic <client_id>` | Emergency cancel all orders and square off positions for one client. |
| `xts-panic-all` | Global Kill-Switch: Square off all clients simultaneously. |
| `xts-backup` | Create an immediate encrypted hot backup snapshot. |
| `xts-admin-reset-2fa` | Emergency 2FA Break-Glass tool if locked out of the web portal. |
| `xts-logs <client_id> -f` | Stream live real-time execution logs for a client container. |
| `xts-dr-restore` | Restore entire multi-tenant cluster from an encrypted backup archive. |

---

## 📡 TradingView Webhook Format

Set your TradingView Strategy Alert to:
- **Webhook URL**: `http://YOUR_SERVER_IP/webhook/{tenant_id}`
- **Message (JSON)**:
```json
{
  "secret": "YourClientWebhookSecret",
  "action": "{{strategy.order.action}}",
  "symbol": "{{ticker}}",
  "quantity": "{{strategy.order.contracts}}",
  "price": "{{close}}"
}
```

---

## 🔒 Security & Disaster Recovery

- **Vault at Rest**: All client broker secrets (API_KEY, API_SECRET, MD_API_KEY, MD_API_SECRET) are encrypted using Fernet (AES-128-CBC + HMAC-SHA256).
- **Cold-Start Disaster Recovery**:
```bash
sudo xts-dr-restore --backup-file /path/to/xts_backup.tar.gz.gpg --backup-passphrase "YourBackupPassphrase" --master-key "YourMasterKey"
```
