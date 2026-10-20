from datetime import UTC, datetime


def utc_milliseconds() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000)
