from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def api_limit_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    values = {
        "DATABASE_MIN_FREE_BYTES": "0",
        "DOWNLOAD_STAGING_DIRECTORY": str(staging),
        "STAGING_MIN_FREE_BYTES": "0",
        "MAX_TRANSPORT_FRAME_BYTES": "2097152",
        "ACCOUNT_REQUESTS_PER_MINUTE": "30000",
        "ACCOUNT_REQUEST_BURST": "15000",
        "PROCESS_REQUESTS_PER_MINUTE": "600000",
        "PROCESS_REQUEST_BURST": "60000",
        "MAX_ACCOUNT_CONCURRENT_FRAME_UPLOADS": "200",
        "MAX_PROCESS_CONCURRENT_FRAME_UPLOADS": "1600",
        "MAX_STAGED_DOWNLOAD_FILES": "200",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
