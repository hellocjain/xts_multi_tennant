import os
import sys
import pytest

root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
client_dir = os.path.join(root_dir, "client")
portal_dir = os.path.join(root_dir, "portal")

if client_dir not in sys.path:
    sys.path.insert(0, client_dir)
if portal_dir not in sys.path:
    sys.path.insert(0, portal_dir)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

if not os.environ.get("PORTAL_MASTER_KEY"):
    os.environ["PORTAL_MASTER_KEY"] = "uYvN3lM8k9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p="


@pytest.fixture(autouse=True)
def isolate_test_daily_risk(tmp_path, monkeypatch):
    """Ensures each test has an isolated daily risk tracking file so limits never leak."""
    test_file = str(tmp_path / "daily_risk_state.json")
    monkeypatch.setenv("DAILY_RISK_STATE_FILE", test_file)
    for mod_name in ("xts_api", "client.xts_api"):
        if mod_name in sys.modules:
            monkeypatch.setattr(sys.modules[mod_name], "_get_daily_risk_file", lambda: test_file)
    yield
