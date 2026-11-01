from pathlib import Path

import pytest

from running_drafts.filesystem_capacity import (
    StorageReserveUnavailable,
    available_bytes,
    require_capacity,
    validate_writable_directory,
)


def test_staging_directory_must_exist_and_accept_a_probe(tmp_path: Path) -> None:
    staging = tmp_path / "probe-staging"
    with pytest.raises(ValueError):
        validate_writable_directory(staging)

    staging.mkdir()
    validate_writable_directory(staging)
    assert list(staging.iterdir()) == []


def test_capacity_allows_equality_and_rejects_crossing_reserve(tmp_path: Path) -> None:
    available = available_bytes(tmp_path)
    require_capacity(tmp_path, candidate_bytes=0, minimum_free_bytes=available)
    with pytest.raises(StorageReserveUnavailable):
        require_capacity(tmp_path, candidate_bytes=1, minimum_free_bytes=available)
