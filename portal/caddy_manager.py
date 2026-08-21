import os
import socket
import http.client
import logging
from contextlib import closing
from database import get_db_connection, get_portal_data_dir

logger = logging.getLogger(__name__)

def get_caddy_config_path():
    return os.environ.get("CADDY_CONFIG_PATH", os.path.join(get_portal_data_dir(), "Caddyfile"))

def get_caddy_admin_socket():
    return os.environ.get("CADDY_ADMIN_SOCKET", "/var/run/caddy/admin.sock")

DOMAIN_NAME = os.environ.get("DOMAIN_NAME", "trading.yourdomain.com")
ADMIN_ALLOWED_IPS = os.environ.get("ADMIN_ALLOWED_IPS", "127.0.0.1 172.28.0.0/24")

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)

def generate_caddyfile_content() -> str:
    with closing(get_db_connection()) as conn:
        active_tenants = conn.execute("SELECT id FROM tenants WHERE status='ACTIVE'").fetchall()

    client_routes = []
    for t in active_tenants:
        t_id = t["id"]
        client_routes.append(f"""
    handle_path /webhook/{t_id}* {{
        reverse_proxy xts_client_{t_id}:8000 {{
            header_up X-Forwarded-For {{remote_host}}
            transport http {{
                response_header_timeout 5s
                dial_timeout 1s
            }}
        }}
    }}""")

    routes_block = "\n".join(client_routes) if client_routes else "    # No active client routes configured"

    admin_socket = get_caddy_admin_socket()
    domain_env = os.environ.get("DOMAIN_NAME", "").strip()
    if not domain_env or domain_env in ("trading.yourdomain.com", ":80") or domain_env.startswith("http://"):
        site_address = ":80"
        global_opts = f"""{{
    admin "unix/{admin_socket}"
    auto_https off
}}"""
    else:
        site_address = domain_env
        global_opts = f"""{{
    admin "unix/{admin_socket}"
}}"""

    caddy_content = f"""# =====================================================================
# XTS MULTI-TENANT DYNAMIC INGRESS CONFIGURATION (MANAGED BY PORTAL)
# =====================================================================
{global_opts}

{site_address} {{
    # 1. Hardened Admin Portal Access with IP Allowlist
    handle /admin* {{
        @blocked_admin {{
            not client_ip {ADMIN_ALLOWED_IPS}
        }}
        respond @blocked_admin "Access Denied: IP Not Authorized" 403

        reverse_proxy xts_portal:8500 {{
            header_up X-Forwarded-For {{remote_host}}
            header_up X-Real-IP {{remote_host}}
        }}
    }}

    # 2. Internal Portal-to-Client Telemetry Proxy (xts_mgmt_net only)
    handle /internal-client-proxy/* {{
        @blocked_internal {{
            not client_ip 172.28.0.0/24 127.0.0.1
        }}
        respond @blocked_internal "Access Denied" 403

        @client_route {{
            path_regexp internal ^/internal-client-proxy/([^/]+)(/.*)?$
        }}
        reverse_proxy @client_route {{re.internal.1}}:8000 {{
            rewrite {{re.internal.2}}
            transport http {{
                response_header_timeout 3s
                dial_timeout 1s
            }}
        }}
    }}

    # 3. Dynamic Per-Client Webhook Routes
    ### CLIENT_ROUTES_START ###
{routes_block}
    ### CLIENT_ROUTES_END ###

    # 4. Fallback for Inactive or Unrecognized Endpoints
    handle {{
        respond "Tenant Not Found or Inactive" 404
    }}
}}
"""
    return caddy_content

def sync_caddy_config() -> bool:
    content = generate_caddyfile_content()
    caddy_config_path = get_caddy_config_path()
    caddy_admin_socket = get_caddy_admin_socket()
    try:
        if os.path.isdir(caddy_config_path):
            import shutil
            shutil.rmtree(caddy_config_path)
        os.makedirs(os.path.dirname(caddy_config_path), exist_ok=True)
        with open(caddy_config_path, "w") as f:
            f.write(content)
        logger.info(f"Wrote updated Caddyfile to {caddy_config_path}")

        # If Unix socket exists, fire atomic reload
        if os.path.exists(caddy_admin_socket):
            conn = UnixHTTPConnection(caddy_admin_socket)
            headers = {"Content-Type": "text/caddyfile"}
            conn.request("POST", "/load", body=content.encode('utf-8'), headers=headers)
            resp = conn.getresponse()
            if resp.status in (200, 202):
                logger.info("Caddy reloaded dynamically over Unix socket successfully.")
                return True
            else:
                logger.error(f"Caddy reload failed with HTTP {resp.status}: {resp.read().decode()}")
                return False
        else:
            logger.info("Caddy socket not present (running in standalone/dev mode). Config written.")
            return True
    except Exception as e:
        logger.error(f"Failed to sync Caddy configuration: {e}")
        return False
