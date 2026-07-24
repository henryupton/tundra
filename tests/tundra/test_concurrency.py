import pytest

from tundra.concurrency import parallel_map


def test_parallel_map_preserves_order():
    assert parallel_map(lambda x: x * 2, [1, 2, 3, 4], max_workers=4) == [2, 4, 6, 8]


def test_parallel_map_empty_returns_empty():
    assert parallel_map(lambda x: x, [], max_workers=4) == []


def test_parallel_map_runs_concurrently():
    import threading

    barrier = threading.Barrier(3, timeout=5)

    def wait_on_barrier(_):
        # If calls were serial this would time out; concurrency lets all 3 meet.
        return barrier.wait() is not None

    assert all(parallel_map(wait_on_barrier, [1, 2, 3], max_workers=3))


def test_parallel_map_propagates_exceptions():
    def boom(x):
        raise ValueError(f"boom {x}")

    with pytest.raises(ValueError):
        parallel_map(boom, [1, 2, 3], max_workers=2)
