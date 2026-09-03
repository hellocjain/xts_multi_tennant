#!/usr/bin/env python3
import os
import sys
import json
import time
import logging
from cryptography.fernet import Fernet
from contextlib import closing

# Ensure portal root is on sys.path
PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PORTAL_DIR not in sys.path:
    sys.path.insert(0, PORTAL_DIR)

import database
import docker_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("vault_migration")

def migrate_vault(old_key_str: str = None, new_key_str: str = None, update_env_files: bool = True) -> dict:
    if not old_key_str:
        old_key_str = os.environ.get("OLD_PORTAL_MASTER_KEY", "").strip()

    if not old_key_str:
        raise RuntimeError("CRITICAL SECURITY ERROR: OLD_PORTAL_MASTER_KEY environment variable is not configured. Vault migration aborted.")
    
    if not new_key_str:
        new_key_str = Fernet.generate_key().decode()

    old_fernet = Fernet(old_key_str.encode() if isinstance(old_key_str, str) else old_key_str)
    new_fernet = Fernet(new_key_str.encode() if isinstance(new_key_str, str) else new_key_str)

    logger.info("Starting vault migration to new master key...")
    re_encrypted_tenants = 0
    re_encrypted_admins = 0

    with closing(database.get_db_connection()) as conn:
        with conn:
            # 1. Re-encrypt tenant credentials
            tenant_rows = conn.execute("SELECT tenant_id, encrypted_payload FROM tenant_credentials").fetchall()
            for r in tenant_rows:
                t_id = r["tenant_id"]
                try:
                    decrypted_bytes = old_fernet.decrypt(r["encrypted_payload"].encode("utf-8"))
                    payload = json.loads(decrypted_bytes.decode("utf-8"))
                    re_enc = new_fernet.encrypt(json.dumps(payload).encode("utf-8")).decode("utf-8")
                    conn.execute(
                        "UPDATE tenant_credentials SET encrypted_payload=?, updated_at=? WHERE tenant_id=?",
                        (re_enc, time.time(), t_id)
                    )
                    re_encrypted_tenants += 1
                except Exception as e:
                    # Check if already encrypted with new key
                    try:
                        new_fernet.decrypt(r["encrypted_payload"].encode("utf-8"))
                        logger.info(f"Tenant {t_id} is already encrypted with the new key. Skipping.")
                    except Exception:
                        logger.error(f"Failed to decrypt credentials for tenant {t_id}: {e}")
                        raise

            # 2. Re-encrypt admin TOTP secrets
            admin_rows = conn.execute("SELECT id, totp_secret_enc FROM admin_users WHERE totp_secret_enc IS NOT NULL").fetchall()
            for ar in admin_rows:
                a_id = ar["id"]
                try:
                    dec_bytes = old_fernet.decrypt(ar["totp_secret_enc"].encode("utf-8"))
                    totp_payload = json.loads(dec_bytes.decode("utf-8"))
                    re_enc_totp = new_fernet.encrypt(json.dumps(totp_payload).encode("utf-8")).decode("utf-8")
                    conn.execute("UPDATE admin_users SET totp_secret_enc=? WHERE id=?", (re_enc_totp, a_id))
                    re_encrypted_admins += 1
                except Exception as e:
                    try:
                        new_fernet.decrypt(ar["totp_secret_enc"].encode("utf-8"))
                        logger.info(f"Admin {a_id} TOTP is already encrypted with new key. Skipping.")
                    except Exception:
                        logger.error(f"Failed to decrypt TOTP secret for admin {a_id}: {e}")
                        raise

    # 3. Update environment variable in process
    os.environ["PORTAL_MASTER_KEY"] = new_key_str

    # 4. Write new key to .env file and set 0400 permissions
    if update_env_files:
        env_paths = [
            os.path.join(database.get_portal_data_dir(), ".env"),
            os.path.join(PORTAL_DIR, ".env"),
            "/opt/xts_multi/portal/.env"
        ]
        for env_path in set(env_paths):
            if os.path.exists(os.path.dirname(env_path)):
                lines = []
                found = False
                if os.path.exists(env_path):
                    try:
                        with open(env_path, "r") as f:
                            for line in f:
                                if line.startswith("PORTAL_MASTER_KEY="):
                                    lines.append(f"PORTAL_MASTER_KEY={new_key_str}\n")
                                    found = True
                                else:
                                    lines.append(line)
                    except Exception:
                        pass
                if not found:
                    lines.append(f"PORTAL_MASTER_KEY={new_key_str}\n")

                try:
                    with open(env_path, "w") as f:
                        f.writelines(lines)
                    os.chmod(env_path, 0o400)
                    logger.info(f"Wrote PORTAL_MASTER_KEY to {env_path} (chmod 0400)")
                except Exception as ex:
                    logger.warning(f"Could not write to {env_path}: {ex}")

    # 5. Re-write client config files using the new master key
    for r in tenant_rows:
        try:
            docker_manager.write_client_config(r["tenant_id"])
        except Exception as e:
            logger.warning(f"Could not update client config for {r['tenant_id']}: {e}")

    logger.info(f"✅ Migration complete. Tenants: {re_encrypted_tenants} | Admins: {re_encrypted_admins}")
    return {
        "status": "success",
        "new_master_key": new_key_str,
        "re_encrypted_tenants": re_encrypted_tenants,
        "re_encrypted_admins": re_encrypted_admins
    }

if __name__ == "__main__":
    res = migrate_vault()
    print(json.dumps(res, indent=2))
