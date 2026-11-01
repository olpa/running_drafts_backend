from uuid import uuid4

import pytest

from running_drafts.request_limits import (
    RequestLimiter,
    RequestRateLimitExceeded,
    UploadCapacity,
    UploadConcurrencyLimitExceeded,
)


def test_account_and_process_request_bursts_are_independent() -> None:
    first = uuid4()
    second = uuid4()
    limiter = RequestLimiter(
        account_requests_per_minute=0,
        account_burst=1,
        process_requests_per_minute=0,
        process_burst=3,
    )

    limiter.consume(first)
    with pytest.raises(RequestRateLimitExceeded, match="account"):
        limiter.consume(first)
    limiter.consume(second)
    with pytest.raises(RequestRateLimitExceeded, match="process"):
        limiter.consume(uuid4())


def test_upload_capacity_rejects_account_then_process_and_releases() -> None:
    first = uuid4()
    second = uuid4()
    capacity = UploadCapacity(per_account=1, process=2)
    first_lease = capacity.acquire(first)
    with pytest.raises(UploadConcurrencyLimitExceeded, match="account"):
        capacity.acquire(first)
    second_lease = capacity.acquire(second)
    with pytest.raises(UploadConcurrencyLimitExceeded, match="process"):
        capacity.acquire(uuid4())

    first_lease.release()
    replacement = capacity.acquire(first)
    replacement.release()
    second_lease.release()


def test_request_bucket_refills_from_a_monotonic_clock() -> None:
    now = [0.0]
    account_id = uuid4()
    limiter = RequestLimiter(
        account_requests_per_minute=60,
        account_burst=1,
        process_requests_per_minute=60,
        process_burst=1,
        monotonic=lambda: now[0],
    )
    limiter.consume(account_id)
    with pytest.raises(RequestRateLimitExceeded):
        limiter.consume(account_id)
    now[0] = 1.0
    limiter.consume(account_id)


def test_seventeenth_process_upload_is_rejected() -> None:
    capacity = UploadCapacity(per_account=2, process=16)
    leases = [capacity.acquire(uuid4()) for _ in range(16)]
    with pytest.raises(UploadConcurrencyLimitExceeded, match="process"):
        capacity.acquire(uuid4())
    for lease in leases:
        lease.release()
