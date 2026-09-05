#!/bin/bash
# =====================================================================
# XTS Multi-Tenant Cluster Auto-Installer Wrapper
# =====================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "$SCRIPT_DIR/setup_multi_tenant.sh"
exec bash "$SCRIPT_DIR/setup_multi_tenant.sh" "$@"
