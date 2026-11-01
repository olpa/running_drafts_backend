import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID


class RequestRateLimitExceeded(Exception):
    pass


class UploadConcurrencyLimitExceeded(Exception):
    pass


class TokenBucket:
    def __init__(
        self,
        requests_per_minute: int,
        burst: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate_per_second = requests_per_minute / 60
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._monotonic = monotonic
        self._updated = monotonic()
        self._lock = threading.Lock()

    def consume(self) -> bool:
        with self._lock:
            now = self._monotonic()
            elapsed = max(0.0, now - self._updated)
            self._updated = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)
            if self._tokens < 1:
                return False
            self._tokens -= 1
            return True


class RequestLimiter:
    def __init__(
        self,
        *,
        account_requests_per_minute: int,
        account_burst: int,
        process_requests_per_minute: int,
        process_burst: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._account_rate = account_requests_per_minute
        self._account_burst = account_burst
        self._monotonic = monotonic
        self._process = TokenBucket(
            process_requests_per_minute,
            process_burst,
            monotonic=monotonic,
        )
        self._accounts: dict[UUID, TokenBucket] = {}
        self._lock = threading.Lock()

    def consume(self, account_id: UUID) -> None:
        if not self._process.consume():
            raise RequestRateLimitExceeded("process")
        with self._lock:
            account = self._accounts.get(account_id)
            if account is None:
                account = TokenBucket(
                    self._account_rate,
                    self._account_burst,
                    monotonic=self._monotonic,
                )
                self._accounts[account_id] = account
        if not account.consume():
            raise RequestRateLimitExceeded("account")


@dataclass
class UploadLease:
    capacity: UploadCapacity
    account_id: UUID
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.capacity.release(self.account_id)


class UploadCapacity:
    def __init__(self, *, per_account: int, process: int) -> None:
        self._per_account = per_account
        self._process_maximum = process
        self._process_active = 0
        self._account_active: dict[UUID, int] = {}
        self._lock = threading.Lock()

    def acquire(self, account_id: UUID) -> UploadLease:
        with self._lock:
            account_active = self._account_active.get(account_id, 0)
            if account_active >= self._per_account:
                raise UploadConcurrencyLimitExceeded("account")
            if self._process_active >= self._process_maximum:
                raise UploadConcurrencyLimitExceeded("process")
            self._process_active += 1
            self._account_active[account_id] = account_active + 1
        return UploadLease(self, account_id)

    def release(self, account_id: UUID) -> None:
        with self._lock:
            self._process_active -= 1
            remaining = self._account_active[account_id] - 1
            if remaining:
                self._account_active[account_id] = remaining
            else:
                del self._account_active[account_id]
