import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, List, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(fn: Callable[[T], R], items: Iterable[T], max_workers: int) -> List[R]:
    """Apply ``fn`` to each item concurrently, preserving input order.

    Returns results in the same order as ``items``. Propagates the first
    exception raised by any worker. Returns an empty list for empty input.
    """
    materialised = list(items)
    if not materialised:
        return []

    workers = max(1, min(max_workers, len(materialised)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(fn, materialised))


class DeadlineExceeded(Exception):
    """A call given to call_with_deadline was still running when its deadline passed."""

    def __init__(self, deadline: float) -> None:
        super().__init__(f"still running after {deadline:g} s")
        self.deadline = deadline


class ThreadStartError(RuntimeError):
    """call_with_deadline couldn't start the thread to run a call on, so never made it."""


def call_with_deadline(fn: Callable[[], R], deadline: float) -> R:
    """
    What ``fn`` returns or raises, if it finishes within ``deadline`` seconds, and
    DeadlineExceeded if it doesn't.

    ``fn`` runs on a daemon thread, in a copy of the caller's context. A call can't be
    cancelled, so one that misses its deadline is left to finish on its own, and what it
    returns or raises then is dropped. Being a daemon, it doesn't hold up the
    interpreter's exit, which a concurrent.futures worker would. A deadline that isn't a
    positive number raises ValueError before ``fn`` starts, and a thread that can't be
    started raises ThreadStartError, so a caller can tell a call never made from one
    that raised.
    """
    wait = float(deadline)
    if not wait > 0:
        raise ValueError(f"A deadline must be a positive number, not {deadline!r}")
    done = threading.Event()
    results: List[R] = []
    errors: List[BaseException] = []

    def run() -> None:
        try:
            results.append(fn())
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    context = contextvars.copy_context()
    thread = threading.Thread(
        target=context.run, args=(run,), name="call_with_deadline", daemon=True
    )
    try:
        thread.start()
    except RuntimeError as error:
        raise ThreadStartError(f"no thread to run the call on: {error}") from error
    if not done.wait(min(wait, threading.TIMEOUT_MAX)):
        raise DeadlineExceeded(deadline)
    if errors:
        raise errors[0]
    return results[0]
