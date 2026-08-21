#!/bin/bash
# =====================================================================
# 🚀 XTS V10.0-ENTERPRISE MULTI-TENANT CLUSTER AUTO-INSTALLER
# Broker: AC Agarwal (Symphony XTS API)
# Architecture: Container-per-Client Isolation + Hardened Admin Portal
# Capacity: 20+ Clients on a Single VPS
# Security: 2FA TOTP, Encrypted Vault at Rest, Dual-Network Segmentation,
# Dynamic Caddy Ingress over Unix Socket, Hot SQLite Backups & DR Engine
# =====================================================================
set -e

PROJECT_DIR="/opt/xts_multi"
USER_NAME="${SUDO_USER:-$(whoami)}"

echo "==========================================================="
echo "   🚀 XTS V10.0-ENTERPRISE MULTI-TENANT CLUSTER INSTALLER  "
echo "==========================================================="

# 1. System Dependencies & Port Freeing
echo "[1/7] Updating system and installing Docker & Security utilities..."
sudo systemctl stop apache2 nginx 2>/dev/null || true

sudo apt-get update -y
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release ufw fail2ban python3 python3-pip

# Install Docker CE & Compose Plugin if missing
if ! command -v docker &> /dev/null; then
    echo "Installing Docker Engine..."
    sudo mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
    echo       "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu       $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update -y
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

# Configure UFW firewall
echo "Configuring firewall..."
sudo ufw allow 22/tcp >/dev/null 2>&1 || true
sudo ufw allow 80/tcp >/dev/null 2>&1 || true
sudo ufw allow 443/tcp >/dev/null 2>&1 || true
sudo ufw --force enable >/dev/null 2>&1 || true

# 2. Application Directory Structure
echo "[2/7] Preparing cluster filesystem at $PROJECT_DIR..."
sudo mkdir -p "$PROJECT_DIR/portal"
sudo mkdir -p "$PROJECT_DIR/caddy"
sudo mkdir -p "$PROJECT_DIR/data"
sudo mkdir -p "$PROJECT_DIR/backup"
sudo mkdir -p "$PROJECT_DIR/backups"
sudo mkdir -p "$PROJECT_DIR/client"
sudo mkdir -p "$PROJECT_DIR/cli"
sudo mkdir -p /var/run/caddy

# 3. Interactive Domain & Security Setup
echo ""
echo "=== 3. DOMAIN & SECURITY CONFIGURATION ==="
read -p "Enter your Domain Name (e.g. trading.yourdomain.com) [Press Enter for Direct Server IP]: " DOMAIN_NAME
DOMAIN_NAME=${DOMAIN_NAME:-":80"}

read -p "Enter Allowed Admin IP/CIDR (e.g. 1.2.3.4/32 or press Enter to allow all): " ADMIN_IPS
ADMIN_IPS=${ADMIN_IPS:-"0.0.0.0/0"}

read -p "Enter Initial Admin Username [Default: admin]: " ADMIN_USER
ADMIN_USER=${ADMIN_USER:-"admin"}

read -s -p "Enter Initial Admin Password [Leave blank to generate random]: " ADMIN_PASS; echo
if [ -z "$ADMIN_PASS" ]; then
    ADMIN_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
    echo "🔑 Generated Admin Password: $ADMIN_PASS"
fi

# Generate 256-bit Cryptographic Root Master Key
MASTER_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
BACKUP_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

# Write Portal Environment
sudo tee "$PROJECT_DIR/portal/.env" > /dev/null << EOF
PORTAL_MASTER_KEY=$MASTER_KEY
PORTAL_ADMIN_USER=$ADMIN_USER
PORTAL_ADMIN_PASSWORD=$ADMIN_PASS
DOMAIN_NAME=$DOMAIN_NAME
ADMIN_ALLOWED_IPS=$ADMIN_IPS
PORTAL_DATA_DIR=/opt/xts_multi/portal
CLIENT_DATA_ROOT=/opt/xts_multi/data
CADDY_CONFIG_PATH=/opt/xts_multi/caddy/Caddyfile
CADDY_ADMIN_SOCKET=/var/run/caddy/admin.sock
INGRESS_NETWORK=xts_ingress_net
EOF
sudo chmod 400 "$PROJECT_DIR/portal/.env"

# Write Backup Passphrase
sudo tee "$PROJECT_DIR/backup/.backup_env" > /dev/null << EOF
BACKUP_PASSPHRASE=$BACKUP_PASS
EOF
sudo chmod 400 "$PROJECT_DIR/backup/.backup_env"

# 4. Copy Code & Build Docker Images
echo "[4/7] Building Docker images for Client Engine & Admin Portal..."
# Copy source files to $PROJECT_DIR
sudo cp -r client/* "$PROJECT_DIR/client/" 2>/dev/null || true
sudo cp -r portal/* "$PROJECT_DIR/portal/" 2>/dev/null || true
sudo cp -r backup/* "$PROJECT_DIR/backup/" 2>/dev/null || true
sudo cp -r cli/* "$PROJECT_DIR/cli/" 2>/dev/null || true
sudo cp docker-compose.yml "$PROJECT_DIR/" 2>/dev/null || true

# Build Client base image
cd "$PROJECT_DIR/client"
sudo docker build -t xts_bot:latest .

# Build Portal image
cd "$PROJECT_DIR/portal"
sudo docker build -t xts_portal:latest .

# 5. Launch Cluster via Docker Compose
echo "[5/7] Starting Multi-Tenant Cluster Services..."
cd "$PROJECT_DIR"
sudo docker compose up -d

# 6. Install CLI Suite
echo "[6/7] Installing Enterprise CLI Suite in /usr/local/bin..."
sudo bash "$PROJECT_DIR/cli/install_cli.sh"

# 7. Configure Automated Systemd Timers (Warmup at 08:30 IST & Backup at 23:45 IST)
echo "[7/7] Configuring automated timers and systemd services..."

# Service: Docker Compose Cluster Auto-Start on Reboot
sudo tee /etc/systemd/system/xts-cluster.service > /dev/null << EOF
[Unit]
Description=XTS Multi-Tenant Trading Cluster
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
EOF

# Timer 1: Daily Master Cache Warmup (08:30 AM IST Mon-Fri)
sudo tee /etc/systemd/system/xts-warmup.service > /dev/null << EOF
[Unit]
Description=XTS Multi-Tenant Master Cache Warmup

[Service]
Type=oneshot
ExecStart=/usr/local/bin/xts-warmup
EOF

sudo tee /etc/systemd/system/xts-warmup.timer > /dev/null << EOF
[Unit]
Description=Trigger XTS Master Cache Warmup at 08:30 AM IST (Mon-Fri)

[Timer]
OnCalendar=Mon..Fri *-*-* 08:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Timer 2: Daily Hot SQLite Backup (23:45 IST Daily)
sudo tee /etc/systemd/system/xts-backup.service > /dev/null << EOF
[Unit]
Description=XTS Multi-Tenant Encrypted Hot SQLite Backup

[Service]
Type=oneshot
ExecStart=/usr/local/bin/xts-backup
EOF

sudo tee /etc/systemd/system/xts-backup.timer > /dev/null << EOF
[Unit]
Description=Trigger XTS Multi-Tenant Encrypted Backup at 23:45 IST

[Timer]
OnCalendar=*-*-* 23:45:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable xts-cluster.service
sudo systemctl enable xts-warmup.timer
sudo systemctl enable xts-backup.timer
sudo systemctl start xts-warmup.timer
sudo systemctl start xts-backup.timer

SERVER_IP=$(curl -s ifconfig.me || echo "YOUR_SERVER_IP")
if [ "$DOMAIN_NAME" == ":80" ] || [ -z "$DOMAIN_NAME" ]; then
    PORTAL_ACCESS_URL="http://$SERVER_IP/admin/login"
else
    PORTAL_ACCESS_URL="https://$DOMAIN_NAME/admin/login"
fi

echo "==========================================================="
echo " ✅ V10.0-PRO MULTI-TENANT CLUSTER INSTALLED SUCCESSFULLY! "
echo "==========================================================="
echo "                                                           "
echo " 🌐 Admin Portal URL : $PORTAL_ACCESS_URL                  "
echo " 👤 Admin Username   : $ADMIN_USER                          "
echo " 🔑 Admin Password   : $ADMIN_PASS                          "
echo " 🔐 Master Key       : $MASTER_KEY                          "
echo " 📦 Backup Key       : $BACKUP_PASS                         "
echo "                                                           "
echo " ⚠️ IMPORTANT: SAVE YOUR MASTER KEY & BACKUP KEY NOW!       "
echo " Store them in your primary password manager (1Password).  "
echo "                                                           "
echo " 🛠️ Global Command Suite Active:                            "
echo "   • xts-clients      : View all active client containers   "
echo "   • xts-status       : Check token & cache health          "
echo "   • xts-positions    : View client open positions          "
echo "   • xts-mtm          : Live portfolio MTM summary          "
echo "   • xts-warmup       : Run rolling master cache warmup     "
echo "   • xts-panic        : Panic square-off single client      "
echo "   • xts-panic-all    : Emergency Global Kill-Switch        "
echo "   • xts-backup       : Trigger immediate backup            "
echo "   • xts-admin-reset-2fa : Host 2FA Break-Glass Tool        "
echo "==========================================================="
