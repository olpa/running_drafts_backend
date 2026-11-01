import os
import tempfile
from pathlib import Path


class StorageReserveUnavailable(Exception):
    pass


def available_bytes(path: Path) -> int:
    try:
        statistics = os.statvfs(path)
    except OSError as error:
        raise StorageReserveUnavailable from error
    return statistics.f_bavail * statistics.f_frsize


def require_capacity(path: Path, *, candidate_bytes: int, minimum_free_bytes: int) -> None:
    if available_bytes(path) - candidate_bytes < minimum_free_bytes:
        raise StorageReserveUnavailable


def validate_writable_directory(path: Path) -> None:
    if not path.is_dir():
        raise ValueError(f"staging directory is not a directory: {path}")
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".running-drafts-probe-") as probe:
            os.fstat(probe.fileno())
    except OSError as error:
        raise ValueError(f"staging directory is not writable: {path}") from error
