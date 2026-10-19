from types import SimpleNamespace

import pytest

from mailintel.services import cluster_queue
from mailintel.services.work_queue import WorkQueueRuntime
from mailintel.settings import AppSettings


def test_cluster_workers_default_and_env_override(monkeypatch):
    monkeypatch.delenv("MAILINTEL_CLUSTER_WORKER_COUNT", raising=False)
    assert AppSettings().cluster_worker_count == 4
    monkeypatch.setenv("MAILINTEL_CLUSTER_WORKER_COUNT", "2")
    assert AppSettings().cluster_worker_count == 2
    with pytest.raises(ValueError, match="between 1 and 20"):
        AppSettings(cluster_worker_count=0)


def test_search_and_clustering_claim_disjoint_kinds(monkeypatch):
    runtime = WorkQueueRuntime(AppSettings(), SimpleNamespace())
    cluster_queue.register(runtime, SimpleNamespace())
    runtime.register("search_retrieve", lambda *_: None, operation="local")
    claimed = []

    class Repository:
        def __init__(self):
            self.connection = SimpleNamespace(close=lambda: None, rollback=lambda: None)

        def queue_set_worker(self, worker, state, **kwargs):
            pass

        def queue_claim(self, worker, preset, kinds):
            claimed.append((worker, set(kinds)))
            return None

    monkeypatch.setattr(runtime, "_repository", Repository)
    monkeypatch.setattr(runtime, "_wait", lambda *_: runtime._stop.set())
    runtime._deterministic_loop("local", "local")
    runtime._stop.clear()
    runtime._deterministic_loop("clustering:1", "clustering")
    assert claimed == [
        ("local", {"search_retrieve"}),
        ("clustering:1", {"cluster_message", "cluster_publish"}),
    ]


def test_start_creates_four_clustering_workers(monkeypatch):
    runtime = WorkQueueRuntime(AppSettings(cluster_worker_count=4), SimpleNamespace())
    repository = SimpleNamespace(queue_recover_processing=lambda: None,
                                 connection=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(runtime, "_repository", lambda: repository)
    monkeypatch.setattr(runtime, "_reconcile", lambda _: None)
    started = []

    class Thread:
        def __init__(self, *, target, args, name, daemon):
            self.name = name
            self.args = args

        def start(self):
            started.append((self.name, self.args))

    monkeypatch.setattr("mailintel.services.work_queue.threading.Thread", Thread)
    runtime.start()
    assert started == [
        ("queue-discovery", ()),
        ("queue-local", ("local", "local")),
        *((f"queue-clustering:{slot}", (f"clustering:{slot}", "clustering"))
          for slot in range(1, 5)),
    ]
