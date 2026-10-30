"""Calls held open the way a stalled connection holds a request, for the deadline tests."""

import threading
from typing import Any, Iterable, List

# The name tundra.concurrency.call_with_deadline gives the threads it runs calls on
WATCHED_THREAD = "call_with_deadline"

HOLD_LIMIT = 5


class Stall:
    """
    A side effect, for a FakeApi listing or an OfflineHttp answer, that doesn't return
    until it's released, and then returns `outcome`, or raises it if it's an exception.
    Each call records the thread it ran on and sets `entered`. One held for HOLD_LIMIT
    seconds gives up and is marked `expired`, so a test whose deadline never fires, or
    that waits for the call anyway, fails at teardown rather than hangs.
    """

    def __init__(self, outcome: Any = ()) -> None:
        self.outcome = outcome
        self.entered = threading.Event()
        self.released = threading.Event()
        self.threads: List[threading.Thread] = []
        self.expired = False
        self._calls = threading.Condition()

    def __call__(self, *args: Any) -> Any:
        with self._calls:
            self.threads.append(threading.current_thread())
            self._calls.notify_all()
        self.entered.set()
        if not self.released.wait(HOLD_LIMIT):
            # Raised on the watched thread, where call_with_deadline drops it, so release
            # fails the test
            self.expired = True
            raise AssertionError(f"Nothing gave up on a call held for {HOLD_LIMIT} s")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def wait_for(self, calls: int = 1) -> bool:
        """
        Whether `calls` calls have reached the stall within HOLD_LIMIT seconds. A read
        can give up on a watched thread before it gets here.
        """
        with self._calls:
            return self._calls.wait_for(lambda: len(self.threads) >= calls, HOLD_LIMIT)


def release(stalls: Iterable[Stall]) -> None:
    """
    Release every stall and join the watched threads, so none outlives its test, and
    fail if a stall was held to its limit.
    """
    stalls = list(stalls)
    for stall in stalls:
        stall.released.set()
    for thread in threading.enumerate():
        if thread.name == WATCHED_THREAD:
            thread.join(5)
            assert not thread.is_alive(), "A watched thread outlived its test"
    expired = [stall for stall in stalls if stall.expired]
    assert not expired, f"A call was held for {HOLD_LIMIT} s: nothing gave up on it"
