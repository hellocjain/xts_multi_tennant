import os
import sys
import shutil
import sqlite3
import tarfile
import time
import datetime
import logging
import hashlib
import json
from cryptography.fernet import Fernet
from contextlib import closing

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

def get_portal_dir():
    return os.environ.get("PORTAL_DATA_DIR", "/opt/xts_multi/portal")

def get_client_data_root():
    return os.environ.get("CLIENT_DATA_ROOT", "/opt/xts_multi/data")

def get_backup_dest_dir():
    if "BACKUP_DEST_DIR" in os.environ:
        return os.environ["BACKUP_DEST_DIR"]
    if os.path.exists("/opt/xts_multi"):
        try:
            d = "/opt/xts_multi/backups"
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            pass
    fallback = os.path.join(get_portal_dir(), "backups")
    os.makedirs(fallback, exist_ok=True)
    return fallback

def derive_backup_key(passphrase: str) -> bytes:
    salt = b"XTS_BACKUP_SALT_V1"
    key = hashlib.scrypt(passphrase.encode('utf-8'), salt=salt, n=16384, r=8, p=1, dklen=32)
    import base64
    return base64.urlsafe_b64encode(key)

def backup_sqlite_database(src_db_path: str, dest_db_path: str):
    """Safe hot backup using SQLite's native VACUUM INTO command."""
    os.makedirs(os.path.dirname(dest_db_path), exist_ok=True)
    if os.path.exists(dest_db_path):
        os.remove(dest_db_path)
    try:
        with closing(sqlite3.connect(src_db_path)) as conn:
            conn.execute(f"VACUUM INTO '{dest_db_path}'")
    except Exception as e:
        logger.error(f"VACUUM INTO failed for {src_db_path}: {e}")
        # Fallback copy if VACUUM INTO fails
        shutil.copy2(src_db_path, dest_db_path)

def create_backup_archive(passphrase: str = None) -> str:
    if not passphrase:
        passphrase = os.environ.get("BACKUP_PASSPHRASE", "DefaultBackupPassphrase123!")

    portal_dir = get_portal_dir()
    client_data_root = get_client_data_root()
    backup_dest_dir = get_backup_dest_dir()

    os.makedirs(backup_dest_dir, exist_ok=True)
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    timestamp_str = datetime.datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    staging_dir = os.path.join(backup_dest_dir, f"staging_{timestamp_str}")
    os.makedirs(staging_dir, exist_ok=True)

    try:
        # 1. Hot snapshot portal.db
        portal_db = os.path.join(portal_dir, "portal.db")
        if os.path.exists(portal_db):
            backup_sqlite_database(portal_db, os.path.join(staging_dir, "portal", "portal.db"))

        # 2. Hot snapshot all client databases & risk states
        if os.path.exists(client_data_root):
            for client_id in os.listdir(client_data_root):
                client_path = os.path.join(client_data_root, client_id)
                if not os.path.isdir(client_path):
                    continue

                dest_client_path = os.path.join(staging_dir, "data", client_id)
                os.makedirs(dest_client_path, exist_ok=True)

                # SQLite signals.db
                sig_db = os.path.join(client_path, "signals.db")
                if os.path.exists(sig_db):
                    backup_sqlite_database(sig_db, os.path.join(dest_client_path, "signals.db"))

                # Copy risk state, paper trades, config
                for fname in ("daily_risk_state.json", "paper_trades.log", "config.json"):
                    fsrc = os.path.join(client_path, fname)
                    if os.path.exists(fsrc):
                        shutil.copy2(fsrc, os.path.join(dest_client_path, fname))

        # 3. Create compressed tarball
        tar_path = os.path.join(backup_dest_dir, f"xts_backup_{timestamp_str}.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(staging_dir, arcname="backup")

        # 4. Encrypt archive with backup passphrase
        fernet = Fernet(derive_backup_key(passphrase))
        with open(tar_path, "rb") as f:
            unencrypted_data = f.read()

        encrypted_data = fernet.encrypt(unencrypted_data)
        enc_path = f"{tar_path}.gpg"
        with open(enc_path, "wb") as f:
            f.write(encrypted_data)

        # Cleanup unencrypted tarball and staging
        os.remove(tar_path)
        shutil.rmtree(staging_dir, ignore_errors=True)

        logger.info(f"✅ Encrypted backup created successfully: {enc_path} ({len(encrypted_data)} bytes)")
        return enc_path
    except Exception as e:
        logger.error(f"Backup creation failed: {e}")
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

if __name__ == "__main__":
    pass_arg = sys.argv[1] if len(sys.argv) > 1 else None
    out = create_backup_archive(pass_arg)
    print(f"Backup File: {out}")
