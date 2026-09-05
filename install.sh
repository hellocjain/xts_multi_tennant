#!/bin/bash
# =====================================================================
# 🚀 XTS V10.0 Enterprise Multi-Tenant High-Performance 1-Command Installer
# Target OS: Ubuntu 20.04 / 22.04 / 24.04 LTS
# One-Line Usage:
#   curl -fsSL https://raw.githubusercontent.com/hellocjain/xts_multi_tennant/main/install.sh | sudo bash
# =====================================================================
set -e

# 1. Require Root / Sudo
if [ "$EUID" -ne 0 ]; then
    echo "❌ Error: This installer must be run as root or with sudo:"
    echo "   curl -fsSL https://raw.githubusercontent.com/hellocjain/xts_multi_tennant/main/install.sh | sudo bash"
    exit 1
fi

REPO_URL="https://github.com/hellocjain/xts_multi_tennant.git"
REPO_DIR="/opt/xts_multi_repo"

# 2. Check if already inside a cloned repository
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/setup_multi_tenant.sh" ]; then
    TARGET_DIR="$SCRIPT_DIR"
else
    # 3. Running via curl pipe: ensure git is installed and clone repository
    echo "📦 Preparing XTS multi-tenant codebase from GitHub..."
    apt-get update -y >/dev/null 2>&1 || true
    apt-get install -y git curl ca-certificates >/dev/null 2>&1 || true

    if [ -d "$REPO_DIR/.git" ]; then
        echo "🔄 Updating existing repository at $REPO_DIR..."
        cd "$REPO_DIR"
        git fetch origin main >/dev/null 2>&1 || true
        git reset --hard origin/main >/dev/null 2>&1 || true
    else
        echo "📥 Cloning XTS Multi-Tenant repository into $REPO_DIR..."
        rm -rf "$REPO_DIR"
        git clone "$REPO_URL" "$REPO_DIR"
    fi
    TARGET_DIR="$REPO_DIR"
fi

cd "$TARGET_DIR"
chmod +x "$TARGET_DIR/setup_multi_tenant.sh"
exec bash "$TARGET_DIR/setup_multi_tenant.sh" "$@"

