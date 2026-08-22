import os
import io
import base64
import json
import time
import secrets
import hashlib
import logging
import pyotp
import qrcode
from cryptography.fernet import Fernet
from contextlib import closing
from database import get_db_connection

logger = logging.getLogger(__name__)

def get_fernet():
    key = os.environ.get("PORTAL_MASTER_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "CRITICAL SECURITY ERROR: PORTAL_MASTER_KEY environment variable is not configured. "
            "Refusing to start with insecure cryptographic fallback."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)

def encrypt_credentials(payload: dict) -> str:
    json_bytes = json.dumps(payload).encode('utf-8')
    return get_fernet().encrypt(json_bytes).decode('utf-8')

def decrypt_credentials(encrypted_payload: str) -> dict:
    decrypted_bytes = get_fernet().decrypt(encrypted_payload.encode('utf-8'))
    return json.loads(decrypted_bytes.decode('utf-8'))

# Password Security (Scrypt Key Derivation)
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.scrypt(password.encode('utf-8'), salt=salt.encode('utf-8'), n=16384, r=8, p=1, dklen=64)
    return f"{salt}${key.hex()}"

def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, key_hex = stored_hash.split('$')
        computed = hashlib.scrypt(password.encode('utf-8'), salt=salt.encode('utf-8'), n=16384, r=8, p=1, dklen=64)
        return secrets.compare_digest(computed.hex(), key_hex)
    except Exception:
        return False

# 2FA / TOTP Management
def generate_totp_secret() -> str:
    return pyotp.random_base32()

def get_totp_uri(secret: str, username: str) -> str:
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=username, issuer_name="XTS Multi-Tenant Portal")

def generate_qr_base64(uri: str) -> str:
    try:
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode('utf-8')
    except Exception as e:
        logger.warning(f"PIL QR generation failed ({e}), falling back to SVG generation")
        try:
            import qrcode.image.svg
            factory = qrcode.image.svg.SvgImage
            img = qrcode.make(uri, image_factory=factory)
            buf = io.BytesIO()
            img.save(buf)
            return base64.b64encode(buf.getvalue()).decode('utf-8')
        except Exception as ex:
            logger.error(f"Fallback QR generation failed: {ex}")
            return ""

def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    totp = pyotp.TOTP(secret)
    # Validates with 1 interval window (±30s drift allowance)
    return bool(totp.verify(code.strip(), valid_window=1))

# One-Time Backup Recovery Codes
def generate_recovery_codes(count=10) -> list:
    codes = []
    for _ in range(count):
        part1 = secrets.token_hex(2).upper()
        part2 = secrets.token_hex(2).upper()
        codes.append(f"{part1}-{part2}")
    return codes

def hash_recovery_codes(codes: list) -> list:
    return [hashlib.sha256(c.replace("-", "").upper().encode()).hexdigest() for c in codes]

def verify_and_consume_recovery_code(user_id: str, code: str) -> bool:
    cleaned = code.replace("-", "").upper()
    code_hash = hashlib.sha256(cleaned.encode()).hexdigest()
    
    with closing(get_db_connection()) as conn:
        with conn:
            row = conn.execute("SELECT recovery_codes_hash_json FROM admin_users WHERE id=?", (user_id,)).fetchone()
            if not row or not row["recovery_codes_hash_json"]:
                return False
            
            stored_hashes = json.loads(row["recovery_codes_hash_json"])
            if code_hash in stored_hashes:
                stored_hashes.remove(code_hash)
                conn.execute(
                    "UPDATE admin_users SET recovery_codes_hash_json=? WHERE id=?",
                    (json.dumps(stored_hashes), user_id)
                )
                logger.warning(f"Recovery code consumed for user {user_id}. {len(stored_hashes)} codes remaining.")
                return True
    return False

def validate_broker_credentials(api_key: str, api_secret: str, client_id: str, base_url: str = "https://symphony.acagarwal.com:3000/interactive", md_api_key: str = "", md_api_secret: str = "") -> dict:
    """Validates XTS Interactive & Market Data credentials against live broker REST endpoints."""
    import requests
    results = {
        "valid": False,
        "interactive": False,
        "market_data": False,
        "client_name": "",
        "segments": [],
        "errors": []
    }
    
    clean_base = (base_url or "https://symphony.acagarwal.com:3000/interactive").rstrip("/")
    if not clean_base.endswith("/interactive"):
        clean_base = f"{clean_base}/interactive" if "/interactive" not in clean_base else clean_base

    # 1. Test Interactive Login
    try:
        login_url = f"{clean_base}/user/session"
        payload = {"appKey": api_key.strip(), "secretKey": api_secret.strip(), "source": "WEBAPI"}
        resp = requests.post(login_url, json=payload, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("type") == "success" and (data.get("result") or {}).get("token"):
                token = data["result"]["token"]
                results["interactive"] = True
                
                # Fetch profile details
                prof_url = f"{clean_base}/user/profile"
                p_resp = requests.get(prof_url, headers={"authorization": token}, timeout=5)
                if p_resp.status_code == 200:
                    p_data = p_resp.json().get("result", {})
                    results["client_name"] = p_data.get("ClientName", "") or p_data.get("clientName", "") or client_id
                    results["segments"] = p_data.get("ExchangeList", []) or p_data.get("exchangeList", [])
            else:
                desc = data.get("description") or data.get("error") or "Interactive login rejected"
                results["errors"].append(f"Interactive Auth Error: {desc}")
        else:
            results["errors"].append(f"Interactive HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        results["errors"].append(f"Interactive Connection Error: {str(e)}")

    # 2. Test Market Data Login if provided
    md_key = (md_api_key or api_key).strip()
    md_sec = (md_api_secret or api_secret).strip()
    if md_key and md_sec:
        try:
            md_base = clean_base.replace("/interactive", "/apimarketdata")
            md_login_url = f"{md_base}/auth/login"
            md_payload = {"appKey": md_key, "secretKey": md_sec, "source": "WEBAPI"}
            md_resp = requests.post(md_login_url, json=md_payload, timeout=6)
            if md_resp.status_code == 200 and md_resp.json().get("type") == "success":
                results["market_data"] = True
            else:
                md_err = md_resp.json().get("description") if md_resp.status_code == 200 else f"HTTP {md_resp.status_code}"
                results["errors"].append(f"Market Data Auth Warning: {md_err}")
        except Exception as e:
            results["errors"].append(f"Market Data Connection Warning: {str(e)}")
    else:
        results["market_data"] = True

    results["valid"] = results["interactive"]
    return results

# Session Management
def create_session(user_id: str, ip_address: str, user_agent: str, lifetime_seconds=43200) -> str:
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = time.time()
    expires_at = now + lifetime_seconds

    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO admin_sessions (token_hash, user_id, expires_at, created_at, ip_address, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token_hash, user_id, expires_at, now, ip_address, user_agent)
            )
    return raw_token

def validate_session(raw_token: str, ip_address: str, user_agent: str) -> dict | None:
    if not raw_token:
        return None
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = time.time()

    with closing(get_db_connection()) as conn:
        row = conn.execute("""
            SELECT s.token_hash, s.user_id, s.expires_at, s.ip_address, u.username, u.is_2fa_enabled
            FROM admin_sessions s
            JOIN admin_users u ON s.user_id = u.id
            WHERE s.token_hash=?
        """, (token_hash,)).fetchone()

        if not row:
            return None

        if row["expires_at"] < now:
            with conn:
                conn.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash,))
            return None

        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "is_2fa_enabled": bool(row["is_2fa_enabled"])
        }

def destroy_session(raw_token: str):
    if not raw_token:
        return
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash,))
