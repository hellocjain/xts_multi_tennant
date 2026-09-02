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

# 1. System Dependencies, Timezone & 2GB Swap Configuration
echo "[1/7] Updating system, configuring IST Timezone & allocating 2GB Swap..."
sudo systemctl stop apache2 nginx 2>/dev/null || true

# Set System Timezone to Asia/Kolkata (IST)
echo "🕒 Setting system timezone to Asia/Kolkata (IST)..."
sudo timedatectl set-timezone Asia/Kolkata 2>/dev/null || true

# Provision 2GB Swap Memory if not present
echo "💾 Checking swap memory..."
SWAP_TOTAL=$(free -m | awk '/^Swap:/ {print $2}')
if [ -z "$SWAP_TOTAL" ] || [ "$SWAP_TOTAL" -lt 1000 ]; then
    echo "Creating 2GB swap file to prevent Out-Of-Memory during volatility & builds..."
    sudo fallocate -l 2G /swapfile 2>/dev/null || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile 2>/dev/null || true
    sudo swapon /swapfile 2>/dev/null || true
    grep -qxF '/swapfile none swap sw 0 0' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    echo "✅ 2GB Swapfile activated and persisted in /etc/fstab"
else
    echo "✅ Existing swap memory detected (${SWAP_TOTAL}MB)."
fi

# Kernel Memory Tuning (Locks Active Trading Processes into Fast Physical RAM)
echo "⚡ Tuning Linux kernel memory parameters for zero-latency execution..."
sudo sysctl -w vm.swappiness=20 2>/dev/null || true
sudo sysctl -w vm.vfs_cache_pressure=50 2>/dev/null || true
sudo tee /etc/sysctl.d/99-xts.conf > /dev/null << 'EOF'
vm.swappiness=20
vm.vfs_cache_pressure=50
EOF

# Detect VPS Public IP Address
echo "🌐 Probing VPS Public IP address..."
SERVER_PUBLIC_IP=$(curl -s4 --max-time 3 https://api.ipify.org 2>/dev/null || curl -s4 --max-time 3 https://ifconfig.me 2>/dev/null || curl -s4 --max-time 3 https://icanhazip.com 2>/dev/null || hostname -I | awk '{print $1}')
SERVER_PUBLIC_IP=$(echo "$SERVER_PUBLIC_IP" | tr -d ' \n\r')
if [ -z "$SERVER_PUBLIC_IP" ]; then
    SERVER_PUBLIC_IP="127.0.0.1"
fi
echo "✅ Detected Public Server IP: $SERVER_PUBLIC_IP"

sudo apt-get update -y
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release ufw fail2ban python3 python3-pip tzdata

# Install Docker CE & Compose Plugin if missing
if ! command -v docker &> /dev/null; then
    echo "Installing Docker Engine..."
    sudo mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
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
sudo mkdir -p "$PROJECT_DIR/scripts"
sudo mkdir -p /var/run/caddy
sudo chmod 777 /var/run/caddy

# Clean up any accidental directory created by docker on failed mounts
if [ -d "$PROJECT_DIR/caddy/Caddyfile" ]; then
    sudo rm -rf "$PROJECT_DIR/caddy/Caddyfile"
fi

# 3. Interactive Domain & Security Setup
echo ""
echo "=== 3. DOMAIN & SECURITY CONFIGURATION ==="
read -p "Enter your Domain Name (or Press Enter for Direct Server IP [$SERVER_PUBLIC_IP]): " DOMAIN_NAME
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

# Generate 256-bit Cryptographic Root Master Key (Fernet compatible 32-byte base64)
MASTER_KEY=$(python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
BACKUP_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

# Write Portal Environment (both root and portal dir to ensure 100% sync)
sudo tee "$PROJECT_DIR/.env" > /dev/null << EOF
PORTAL_MASTER_KEY=$MASTER_KEY
PORTAL_ADMIN_USER=$ADMIN_USER
PORTAL_ADMIN_PASSWORD=$ADMIN_PASS
DOMAIN_NAME=$DOMAIN_NAME
SERVER_PUBLIC_IP=$SERVER_PUBLIC_IP
ADMIN_ALLOWED_IPS=$ADMIN_IPS
PORTAL_DATA_DIR=/opt/xts_multi/portal
CLIENT_DATA_ROOT=/opt/xts_multi/data
CADDY_CONFIG_PATH=/opt/xts_multi/caddy/Caddyfile
CADDY_ADMIN_SOCKET=/var/run/caddy/admin.sock
INGRESS_NETWORK=xts_ingress_net
EOF
sudo cp "$PROJECT_DIR/.env" "$PROJECT_DIR/portal/.env"
sudo chmod 400 "$PROJECT_DIR/.env" "$PROJECT_DIR/portal/.env"

# Write Backup Passphrase
sudo tee "$PROJECT_DIR/backup/.backup_env" > /dev/null << EOF
BACKUP_PASSPHRASE=$BACKUP_PASS
EOF
sudo chmod 400 "$PROJECT_DIR/backup/.backup_env"

# Generate Initial Caddyfile File (Guarantees valid file exists for Docker bind mount)
echo "Generating initial Caddy Ingress configuration file..."
if [ "$DOMAIN_NAME" == ":80" ] || [ -z "$DOMAIN_NAME" ] || [ "$DOMAIN_NAME" == "trading.yourdomain.com" ]; then
    SITE_ADDR=":80"
    GLOBAL_BLOCK="{\n    admin \"unix//var/run/caddy/admin.sock\"\n    auto_https off\n}"
else
    SITE_ADDR="$DOMAIN_NAME"
    GLOBAL_BLOCK="{\n    admin \"unix//var/run/caddy/admin.sock\"\n}"
fi

sudo tee "$PROJECT_DIR/caddy/Caddyfile" > /dev/null << EOF
# =====================================================================
# XTS MULTI-TENANT DYNAMIC INGRESS CONFIGURATION (MANAGED BY PORTAL)
# =====================================================================
$GLOBAL_BLOCK

$SITE_ADDR {
    # 1. Hardened Admin Portal Access
    handle /admin* {
        reverse_proxy xts_portal:8500 {
            header_up X-Forwarded-For {remote_host}
            header_up X-Real-IP {remote_host}
        }
    }

    # 2. Root Redirect to Login
    handle / {
        redir /admin/login 302
    }

    # 3. Default Gateway Status
    handle {
        respond "XTS Enterprise Gateway Online" 200
    }
}
EOF
sudo chmod 644 "$PROJECT_DIR/caddy/Caddyfile"

# 4. Copy Code & Build Docker Images
echo "[4/7] Building Docker images for Client Engine & Admin Portal..."
# Copy source files to $PROJECT_DIR
sudo cp -r client/* "$PROJECT_DIR/client/" 2>/dev/null || true
sudo cp -r portal/* "$PROJECT_DIR/portal/" 2>/dev/null || true
sudo cp -r backup/* "$PROJECT_DIR/backup/" 2>/dev/null || true
sudo cp -r cli/* "$PROJECT_DIR/cli/" 2>/dev/null || true
sudo cp -r scripts/* "$PROJECT_DIR/scripts/" 2>/dev/null || true
sudo cp docker-compose.yml "$PROJECT_DIR/" 2>/dev/null || true

# Build Client base image with dual tags
cd "$PROJECT_DIR/client"
sudo docker build -t xts_bot:latest -t xts_client:latest .

# Build Portal image
cd "$PROJECT_DIR/portal"
sudo docker build -t xts_portal:latest .

# 5. Launch Cluster via Docker Compose
echo "[5/7] Starting Multi-Tenant Cluster Services..."
cd "$PROJECT_DIR"
sudo docker compose down --remove-orphans 2>/dev/null || true
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

if [ "$DOMAIN_NAME" == ":80" ] || [ -z "$DOMAIN_NAME" ]; then
    PORTAL_ACCESS_URL="http://$SERVER_PUBLIC_IP/admin/login"
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
echo "   • xts-verify       : Run complete automated system audit "
echo "   • xts-clients      : View all active client containers   "
echo "   • xts-status       : Check token & cache health          "
echo "   • xts-positions    : View client open positions          "
echo "   • xts-mtm          : Live portfolio MTM summary          "
echo "   • xts-test         : Dispatch test webhook signal        "
echo "   • xts-logs         : Stream client container logs        "
echo "   • xts-warmup       : Run rolling master cache warmup     "
echo "   • xts-panic        : Panic square-off single client      "
echo "   • xts-panic-all    : Emergency Global Kill-Switch        "
echo "   • xts-backup       : Trigger immediate backup            "
echo "   • xts-admin-reset-2fa : Host 2FA Break-Glass Tool        "
echo "==========================================================="
