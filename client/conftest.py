import os
import sys
import tempfile
import pytest

@pytest.fixture(autouse=True, scope="session")
def client_test_sandbox():
    temp_sandbox = tempfile.mkdtemp(prefix="client_test_sandbox_")
    os.environ["DATA_DIR"] = temp_sandbox
    os.environ["TESTING_MODE"] = "1"
    yield temp_sandbox
