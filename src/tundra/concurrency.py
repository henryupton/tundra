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
