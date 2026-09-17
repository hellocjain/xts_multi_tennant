import os
import sys
import tempfile
import pytest

@pytest.fixture(autouse=True, scope="session")
def client_test_sandbox():
    temp_sandbox = tempfile.mkdtemp(prefix="client_test_sandbox_")
    os.environ["DATA_DIR"] = temp_sandbox
    os.environ["TESTING_MODE"] = "1"
    os.environ["DISABLE_SENTRY"] = "1"
    os.environ["SENTRY_DSN"] = ""

    try:
        import sentry_sdk
        s_client = sentry_sdk.get_client()
        if s_client.is_active():
            s_client.close(timeout=0)
    except Exception:
        pass

    yield temp_sandbox
