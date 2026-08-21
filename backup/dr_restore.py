#!/usr/bin/env python3
"""
🚨 COLD-START DISASTER RECOVERY (DR) RESTORATION ENGINE
Restores complete multi-tenant cluster state on a fresh Ubuntu VPS from an encrypted offsite backup.
"""
import os
import sys
import shutil
import tarfile
import logging
import argparse
import base64
import hashlib
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

def derive_backup_key(passphrase: str) -> bytes:
    salt = b"XTS_BACKUP_SALT_V1"
    key = hashlib.scrypt(passphrase.encode('utf-8'), salt=salt, n=16384, r=8, p=1, dklen=32)
    return base64.urlsafe_b64encode(key)

def restore_disaster_backup(backup_file: str, passphrase: str, master_key: str, dest_root: str = "/opt/xts_multi"):
    if not os.path.exists(backup_file):
        raise FileNotFoundError(f"Backup file not found: {backup_file}")

    logger.info(f"--- INITIATING DISASTER RECOVERY FROM {backup_file} ---")

    # 1. Decrypt archive
    fernet = Fernet(derive_backup_key(passphrase))
    with open(backup_file, "rb") as f:
        encrypted_data = f.read()

    try:
        decrypted_tar_bytes = fernet.decrypt(encrypted_data)
    except Exception as e:
        logger.critical(f"Decryption failed! Invalid backup passphrase: {e}")
        sys.exit(1)

    tmp_tar = "/tmp/dr_restore_temp.tar.gz"
    with open(tmp_tar, "wb") as f:
        f.write(decrypted_tar_bytes)

    # 2. Extract files
    extract_dir = "/tmp/dr_extract_temp"
    os.makedirs(extract_dir, exist_ok=True)
    with tarfile.open(tmp_tar, "r:gz") as tar:
        tar.extractall(path=extract_dir)

    staging_root = os.path.join(extract_dir, "backup")

    # 3. Restore portal.db
    portal_dest = os.path.join(dest_root, "portal")
    os.makedirs(portal_dest, exist_ok=True)
    staging_portal_db = os.path.join(staging_root, "portal", "portal.db")
    if os.path.exists(staging_portal_db):
        shutil.copy2(staging_portal_db, os.path.join(portal_dest, "portal.db"))
        logger.info("Restored portal.db")

    # 4. Restore master key env file
    env_file = os.path.join(portal_dest, ".env")
    with open(env_file, "w") as f:
        f.write(f"PORTAL_MASTER_KEY={master_key.strip()}\n")
    os.chmod(env_file, 0o400)
    logger.info("Wrote hardened PORTAL_MASTER_KEY to portal/.env (0400)")

    # 5. Restore client data folders
    data_dest = os.path.join(dest_root, "data")
    staging_data = os.path.join(staging_root, "data")
    if os.path.exists(staging_data):
        for client_id in os.listdir(staging_data):
            c_src = os.path.join(staging_data, client_id)
            c_dst = os.path.join(data_dest, client_id)
            os.makedirs(c_dst, exist_ok=True)
            for fname in os.listdir(c_src):
                shutil.copy2(os.path.join(c_src, fname), os.path.join(c_dst, fname))
            logger.info(f"Restored tenant state for {client_id}")

    # Cleanup temp
    os.remove(tmp_tar)
    shutil.rmtree(extract_dir, ignore_errors=True)

    logger.info("✅ DISASTER RECOVERY RESTORATION COMPLETE. System ready for container boot.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="XTS Cold-Start Disaster Recovery Tool")
    parser.add_argument("--backup-file", required=True, help="Path to encrypted backup file (.tar.gz.gpg)")
    parser.add_argument("--backup-passphrase", required=True, help="Passphrase used to decrypt backup")
    parser.add_argument("--master-key", required=True, help="PORTAL_MASTER_KEY used to decrypt broker credentials")
    parser.add_argument("--dest-root", default="/opt/xts_multi", help="Destination root path")

    args = parser.parse_args()
    restore_disaster_backup(args.backup_file, args.backup_passphrase, args.master_key, args.dest_root)
