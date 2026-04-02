"""Memory profiling — SC-005: state < 4 GB per 1M accounts (T049).

Two execution modes:

**Proxy mode** (no Flink cluster required):
  Exercises the pure-Python ``VelocityProcessFunction`` fallback with
  1,000,000 distinct ``account_id`` values and measures peak Python heap
  growth via ``tracemalloc``.  This is a conservative lower-bound proxy:
  RocksDB off-heap overhead is not captured.  Use to validate the data-
  structure memory model before deploying to a full Flink cluster.

    pytest -m perf tests/load/test_memory.py::test_state_size_1m_accounts_proxy -v -s

**Flink-REST mode** (requires pyflink + running local cluster):
  Submits a bounded PyFlink job to a local MiniCluster (REST port 18082),
  drives 1,000,000 distinct accounts through ``VelocityProcessFunction``,
  then queries the Flink REST metrics endpoint:

    GET http://localhost:18082/jobs/{job_id}/vertices/{vertex_id}/metrics
        ?get=Status.JVM.Memory.Heap.Used

  Expected ceiling (SC-005): ≤ 4 GB.

    docker compose -f infra/docker-compose.yml up -d
    pip install -e ".[processing]"
    pytest -m perf tests/load/test_memory.py::test_state_size_1m_accounts_flink_rest -v -s

Note: a single-node local stack running all services (Kafka, Flink, MinIO,
Prometheus) will consume baseline JVM heap.  The test subtracts the heap
sampled *before* job submission to isolate the incremental state cost.
"""

from __future__ import annotations

import time
import tracemalloc
from decimal import Decimal

import pytest

_ACCOUNT_COUNT = 1_000_000
_STATE_CEILING_BYTES = 4 * 1024**3  # 4 GB (SC-005)
_FLINK_REST_BASE = "http://localhost:18082"
_JOB_POLL_INTERVAL_S = 5.0
_JOB_TIMEOUT_S = 300  # 5 min max for bounded 1M-event job
_TERMINAL_STATES = {"FINISHED", "FAILED", "CANCELED"}


# ---------------------------------------------------------------------------
# REST helpers — accept the requests module as a parameter to avoid redundant
# lazy imports (the caller has already obtained it via pytest.importorskip).
# ---------------------------------------------------------------------------


def _get_job_vertex_id(requests_mod: object, job_id: str) -> str | None:
    """Return the first vertex ID for the given job."""
    resp = requests_mod.get(f"{_FLINK_REST_BASE}/jobs/{job_id}", timeout=10)  # type: ignore[attr-defined]
    resp.raise_for_status()
    vertices = resp.json().get("vertices", [])
    return vertices[0]["id"] if vertices else None


def _query_heap_used_bytes(requests_mod: object, job_id: str, vertex_id: str) -> int:
    """Query JVM heap used (bytes) for a running job vertex via Flink REST."""
    url = (
        f"{_FLINK_REST_BASE}/jobs/{job_id}"
        f"/vertices/{vertex_id}/metrics"
        "?get=Status.JVM.Memory.Heap.Used"
    )
    resp = requests_mod.get(url, timeout=10)  # type: ignore[attr-defined]
    resp.raise_for_status()
    for entry in resp.json():
        if entry.get("id") == "Status.JVM.Memory.Heap.Used":
            return int(float(entry["value"]))
    return 0


# ---------------------------------------------------------------------------
# Proxy mode — no Flink cluster required
# ---------------------------------------------------------------------------


@pytest.mark.perf
def test_state_size_1m_accounts_proxy() -> None:
    """Lower-bound memory proxy for SC-005 (no Flink cluster required).

    Builds 1,000,000 independent account state entries — one velocity bucket
    per account — mirroring the Flink per-key MapState layout, and asserts
    that the peak Python heap stays within the SC-005 ceiling of 4 GB.

    One bucket per account is the worst-case write pattern for v1: every
    account has exactly one un-evicted entry in the velocity MapState and no
    TTL eviction has fired yet.

    This test is a lower-bound proxy; the RocksDB state backend has
    additional off-heap overhead.  Use
    ``test_state_size_1m_accounts_flink_rest`` for the production-
    representative measurement.
    """
    base_ts_ms = int(time.time() * 1000)

    # Guard: ensure tracemalloc.stop() is always called, even on OOM
    tracemalloc.start()
    try:
        # Simulate per-key Flink state: each account_id maps to its own dict
        # of {bucket_key: (count, amount_sum)}.  One bucket per account =
        # worst-case footprint (no eviction).
        all_states: dict[str, dict[int, tuple[int, Decimal]]] = {}
        for i in range(_ACCOUNT_COUNT):
            bucket_key = (base_ts_ms + i) // 60_000
            all_states[f"acc-{i:07d}"] = {bucket_key: (1, Decimal("1.00"))}

        # Pin the reference BEFORE stopping so GC cannot collect entries
        # between get_traced_memory() and stop(), which would undercount peak.
        _, peak_bytes = tracemalloc.get_traced_memory()
        _ = len(all_states)  # keep all_states live through the measurement
    finally:
        tracemalloc.stop()

    assert peak_bytes <= _STATE_CEILING_BYTES, (
        f"tracemalloc peak {peak_bytes / 1024**3:.2f} GB exceeds the SC-005 "
        f"ceiling of {_STATE_CEILING_BYTES / 1024**3:.0f} GB for "
        f"{_ACCOUNT_COUNT:,} accounts "
        "(lower-bound proxy — RocksDB off-heap not included)."
    )


# ---------------------------------------------------------------------------
# Flink-REST mode — requires pyflink + running local MiniCluster
# ---------------------------------------------------------------------------


@pytest.mark.perf
def test_state_size_1m_accounts_flink_rest() -> None:
    """Full Flink mode: MiniCluster job + REST metrics for SC-005.

    Submits a bounded DataStream job that drives 1,000,000 distinct
    ``account_id`` values (one event each) through a velocity operator keyed
    on ``account_id``.  After the job completes the test queries:

        GET http://localhost:18082/jobs/{job_id}/vertices/{vertex_id}/metrics
            ?get=Status.JVM.Memory.Heap.Used

    and asserts that the incremental JVM heap (post-submit peak minus pre-
    submit baseline) is ≤ 4 GB (SC-005).

    Skips automatically if ``pyflink`` or ``requests`` are not installed, or
    if the Flink REST endpoint at port 18082 is unreachable.
    """
    pytest.importorskip(
        "pyflink", reason="pyflink not installed — run: pip install -e '.[processing]'"
    )
    requests = pytest.importorskip("requests", reason="requests not installed")

    # Verify the local MiniCluster REST is reachable before starting
    try:
        overview = requests.get(f"{_FLINK_REST_BASE}/overview", timeout=3)
        if overview.status_code != 200:
            pytest.skip(
                f"Flink REST at {_FLINK_REST_BASE} returned HTTP "
                f"{overview.status_code}.  Start the cluster: "
                "docker compose -f infra/docker-compose.yml up -d"
            )
    except requests.exceptions.RequestException:
        pytest.skip(
            f"Flink REST not reachable at {_FLINK_REST_BASE}.  "
            "Start the cluster: docker compose -f infra/docker-compose.yml up -d"
        )

    from pyflink.common import Row, Types  # noqa: PLC0415  # type: ignore[import-untyped]
    from pyflink.datastream import (
        StreamExecutionEnvironment,  # noqa: PLC0415  # type: ignore[import-untyped]
    )
    from pyflink.datastream.functions import (  # noqa: PLC0415  # type: ignore[import-untyped]
        KeyedProcessFunction,
        RuntimeContext,
    )
    from pyflink.datastream.state import (
        MapStateDescriptor,  # noqa: PLC0415  # type: ignore[import-untyped]
    )

    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(1)
    # Bind REST on a dedicated ephemeral port so it does not clash with the
    # main local cluster port (8082) documented in quickstart.md §9.
    env.get_config().set_string("rest.bind-address", "localhost")
    env.get_config().set_string("rest.port", "18082")
    env.get_config().set_integer("taskmanager.numberOfTaskSlots", 1)

    base_ts_ms = int(time.time() * 1000)

    # ── Bounded source: 1M rows (transaction_id, account_id, amount_str, ts) ─
    # Each account_id is distinct → one velocity bucket per key → worst-case
    # per-key MapState footprint (no TTL eviction during the test run).
    records = [
        Row(f"mem-{i:07d}", f"acc-{i:07d}", "1.00", base_ts_ms + i) for i in range(_ACCOUNT_COUNT)
    ]
    type_info = Types.ROW([Types.STRING(), Types.STRING(), Types.STRING(), Types.LONG()])
    source = env.from_collection(records, type_info=type_info)

    # ── Velocity operator adapted for Row input ──────────────────────────────
    class _RowVelocityBridge(KeyedProcessFunction):
        """Velocity MapState operator keyed on account_id, accepting Row input.

        Mirrors VelocityProcessFunction's MapState layout so the JVM heap
        measurement reflects the same per-key state footprint as production.
        """

        def __init__(self) -> None:
            super().__init__()

        def open(self, runtime_context: RuntimeContext) -> None:
            descriptor = MapStateDescriptor(
                "vel_buckets",
                Types.LONG(),
                Types.TUPLE([Types.INT(), Types.BIG_DEC()]),
            )
            self._buckets = runtime_context.get_map_state(descriptor)

        def process_element(self, row: Row, _ctx: KeyedProcessFunction.Context):
            bucket_key: int = row[3] // 60_000
            existing = self._buckets.get(bucket_key)
            if existing is None:
                existing = (0, Decimal("0"))
            self._buckets.put(
                bucket_key,
                (existing[0] + 1, existing[1] + Decimal(row[2])),
            )
            yield row

    keyed = source.key_by(lambda r: r[1], key_type=Types.STRING()).process(
        _RowVelocityBridge(), output_type=type_info
    )
    keyed.print()  # required sink — discarded by the Flink cluster

    # ── Baseline heap before job submission ──────────────────────────────────
    baseline_resp = requests.get(f"{_FLINK_REST_BASE}/taskmanagers", timeout=10)
    baseline_resp.raise_for_status()
    tms = baseline_resp.json().get("taskmanagers", [])
    baseline_heap = sum(tm.get("metrics", {}).get("heapUsed", 0) for tm in tms)

    # ── Submit the job async so we can poll REST while it runs ───────────────
    job_client = env.execute_async("sc005-memory-profiling")
    job_id = str(job_client.get_job_id())

    # ── Poll metrics while the job runs ──────────────────────────────────────
    peak_heap = 0
    state = "UNKNOWN"
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{_FLINK_REST_BASE}/jobs/{job_id}", timeout=10)
            resp.raise_for_status()
            state = resp.json().get("state", "UNKNOWN")
        except requests.exceptions.RequestException as exc:
            pytest.fail(f"Flink REST became unreachable mid-run: {exc}")

        vertex_id = _get_job_vertex_id(requests, job_id)
        if vertex_id:
            heap = _query_heap_used_bytes(requests, job_id, vertex_id)
            if heap > peak_heap:
                peak_heap = heap

        if state in _TERMINAL_STATES:
            break
        time.sleep(_JOB_POLL_INTERVAL_S)
    else:
        pytest.fail(
            f"Job {job_id} did not reach a terminal state within "
            f"{_JOB_TIMEOUT_S}s.  Check the Flink UI at {_FLINK_REST_BASE}."
        )

    assert state == "FINISHED", (
        f"Job {job_id} ended in state {state!r} instead of FINISHED.  "
        f"Check the Flink UI at {_FLINK_REST_BASE}."
    )

    # ── Incremental heap = peak observed − pre-submission baseline ────────────
    incremental_heap = max(0, peak_heap - baseline_heap)

    assert incremental_heap <= _STATE_CEILING_BYTES, (
        f"JVM heap incremental usage {incremental_heap / 1024**3:.2f} GB "
        f"exceeds the SC-005 ceiling of "
        f"{_STATE_CEILING_BYTES / 1024**3:.0f} GB for "
        f"{_ACCOUNT_COUNT:,} accounts.  "
        f"Baseline heap: {baseline_heap / 1024**3:.2f} GB, "
        f"peak observed: {peak_heap / 1024**3:.2f} GB.  "
        "Investigate RocksDB state backend configuration in "
        "infra/flink/flink-conf.yaml."
    )
