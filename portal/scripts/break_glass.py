#!/usr/bin/env python3
"""
🚨 EMERGENCY BREAK-GLASS 2FA RESET TOOL
Run this tool directly as root on the VPS host if locked out of the Admin Portal.
Usage: sudo python3 break_glass.py [username]
"""
import sys
import os
import sqlite3
import time
import secrets
import hashlib

PORTAL_DATA_DIR = os.environ.get("PORTAL_DATA_DIR", "/opt/xts_multi/portal")
DB_PATH = os.path.join(PORTAL_DATA_DIR, "portal.db")

if not os.path.exists(DB_PATH):
    # Try local directory
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "portal.db")

def break_glass(username="admin"):
    if not os.path.exists(DB_PATH):
        print(f"❌ Error: Portal database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    with conn:
        user = conn.execute("SELECT id, username FROM admin_users WHERE username=?", (username,)).fetchone()
        if not user:
            print(f"❌ Error: Admin user '{username}' not found in database.")
            sys.exit(1)

        user_id = user["id"]
        # Reset 2FA
        conn.execute("UPDATE admin_users SET is_2fa_enabled=0, totp_secret_enc=NULL WHERE id=?", (user_id,))
        
        # Generate 15-minute emergency session token
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now = time.time()
        expires_at = now + 900 # 15 minutes

        conn.execute(
            "INSERT INTO admin_sessions (token_hash, user_id, expires_at, created_at, ip_address, user_agent) "
            "VALUES (?, ?, ?, ?, '127.0.0.1', 'BREAK_GLASS_CLI')",
            (token_hash, user_id, expires_at, now)
        )

        # Record audit
        conn.execute(
            "INSERT INTO audit_logs (timestamp, actor, action, details_json) VALUES (?, 'ROOT_HOST', 'BREAK_GLASS_2FA_RESET', ?)",
            (now, '{"reason": "Host CLI Emergency Reset"}')
        )

    print("\n" + "=" * 70)
    print("🚨 EMERGENCY 2FA RESET COMPLETE")
    print("=" * 70)
    print(f"• User: {username}")
    print("• 2FA Status: DISABLED (You will be prompted to re-enroll on next login)")
    print(f"• Emergency Session Cookie: admin_session={raw_token}")
    print("• Valid For: 15 Minutes")
    print("=" * 70 + "\n")

if __name__ == "__main__":
    target_user = sys.argv[1] if len(sys.argv) > 1 else "admin"
    break_glass(target_user)
