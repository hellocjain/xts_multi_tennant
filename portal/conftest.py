import os
import sys
import tempfile
import pytest

@pytest.fixture(autouse=True, scope="session")
def portal_test_sandbox():
    temp_sandbox = tempfile.mkdtemp(prefix="portal_test_sandbox_")
    portal_data = os.path.join(temp_sandbox, "portal")
    client_data = os.path.join(temp_sandbox, "data")
    backup_data = os.path.join(temp_sandbox, "backups")
    caddy_data = os.path.join(temp_sandbox, "caddy")
    
    os.makedirs(portal_data, exist_ok=True)
    os.makedirs(client_data, exist_ok=True)
    os.makedirs(backup_data, exist_ok=True)
    os.makedirs(caddy_data, exist_ok=True)

    os.environ["PORTAL_DATA_DIR"] = portal_data
    os.environ["CLIENT_DATA_ROOT"] = client_data
    os.environ["DATA_DIR"] = client_data
    os.environ["BACKUP_DEST_DIR"] = backup_data
    os.environ["CADDY_CONFIG_PATH"] = os.path.join(caddy_data, "Caddyfile")
    os.environ["PORTAL_MASTER_KEY"] = "uYvN3lM8k9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p="
    os.environ["TESTING_MODE"] = "1"

    yield temp_sandbox
