import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hyper import storage
from hyper.copy import replay_parallel
from hyper.copy.copy_backtest import (
    prepare_price_path,
    prepare_replay_fills,
    subset_price_path,
)
from hyper.discovery import scanner
from hyper.market import rest


def _square(value):
    return value * value


class RuntimeAccelerationTests(unittest.TestCase):
    def tearDown(self):
        rest.configure_post_budget(weight_per_min=None)

    def test_weighted_rest_budget_charges_heavy_and_light_requests_differently(self):
        clock = [100.0]

        def sleep(seconds):
            clock[0] += float(seconds)

        with patch.object(rest.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(rest.time, "time", side_effect=lambda: clock[0]), \
                patch.object(rest.time, "sleep", side_effect=sleep):
            rest.configure_post_budget(
                weight_per_min=1200.0, burst_weight=20.0, min_interval=0.0,
            )
            self.assertEqual(rest._reserve_post(20), 0.0)
            self.assertAlmostEqual(rest._reserve_post(20), 1.0)
            self.assertAlmostEqual(rest._reserve_post(2), 0.1)

    def test_weighted_rest_budget_backs_off_and_recovers(self):
        rest.configure_post_budget(
            weight_per_min=1200.0, burst_weight=20.0, min_interval=0.0,
        )
        rest._rate_limit_feedback(limited=True)
        self.assertAlmostEqual(rest.request_stats()["budget_scale"], 0.70)
        for _ in range(120):
            rest._rate_limit_feedback(limited=False)
        self.assertAlmostEqual(rest.request_stats()["budget_scale"], 1.0)

    def test_weighted_rest_budget_charges_large_fill_responses(self):
        clock = [100.0]

        def sleep(seconds):
            clock[0] += float(seconds)

        with patch.object(rest.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(rest.time, "time", side_effect=lambda: clock[0]), \
                patch.object(rest.time, "sleep", side_effect=sleep):
            rest.configure_post_budget(
                weight_per_min=1200.0, burst_weight=20.0, min_interval=0.0,
            )
            rest._reserve_post(20)
            self.assertEqual(
                rest._charge_result_weight(
                    {"type": "userFillsByTime"}, [{} for _ in range(40)],
                ),
                2,
            )
            self.assertAlmostEqual(rest._reserve_post(2), 0.2)

    def test_prepared_fill_and_path_surfaces_are_reused(self):
        fills = prepare_replay_fills([
            {
                "coin": "BTC", "time": 1000, "tid": 1, "side": "B",
                "sz": "1", "px": "100", "startPosition": "0",
            },
        ], addr="0xabc")
        self.assertIs(prepare_replay_fills(fills, addr="0xabc"), fills)

        path = prepare_price_path([
            {
                "coin": "BTC", "time": 1000, "open_time": 900,
                "close_time": 1100, "low": 90, "high": 110, "close": 100,
            },
        ])
        first = subset_price_path(path, fills, start_ms=900, end_ms=1100)
        second = subset_price_path(path, fills, start_ms=900, end_ms=1100)
        self.assertIs(first, second)

    def test_replay_workers_follow_cpu_affinity_and_one_core_stays_serial(self):
        with patch.object(
            replay_parallel.os, "sched_getaffinity", return_value={0}, create=True,
        ):
            self.assertEqual(replay_parallel.effective_worker_count(8), 1)
            self.assertEqual(replay_parallel.map_ordered(_square, [2, 3]), [4, 9])
        with patch.object(
            replay_parallel.os, "sched_getaffinity",
            return_value={0, 1, 2, 3}, create=True,
        ):
            self.assertEqual(replay_parallel.effective_worker_count(8), 4)
        with patch.object(
            replay_parallel.os, "sched_getaffinity", return_value={0, 1}, create=True,
        ):
            self.assertEqual(
                replay_parallel.map_ordered(_square, [2, 3, 4], max_workers=2),
                [4, 9, 16],
            )

    def test_reusable_replay_pool_initializes_once_across_dependent_batches(self):
        initialized = []

        class FakeExecutor:
            created = 0

            def __init__(self, *, initializer=None, initargs=(), **_kwargs):
                type(self).created += 1
                if initializer:
                    initializer(*initargs)

            def map(self, fn, rows, chunksize=1):
                return map(fn, rows)

            def shutdown(self, **_kwargs):
                return None

        with patch.object(
            replay_parallel.os, "sched_getaffinity", return_value={0, 1}, create=True,
        ), patch.object(
            replay_parallel.concurrent.futures, "ProcessPoolExecutor", FakeExecutor,
        ):
            with replay_parallel.ReusableOrderedPool(
                initializer=lambda value: initialized.append(value),
                initargs=("context",),
            ) as pool:
                self.assertEqual(pool.map_ordered(_square, [2, 3]), [4, 9])
                self.assertEqual(pool.map_ordered(_square, [4, 5]), [16, 25])

        self.assertEqual(FakeExecutor.created, 1)
        self.assertEqual(initialized, ["context"])

    def test_profile_artifacts_are_written_only_by_parent_batch(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(
                str(Path(td) / "hl.db"),
                storage.DISCOVERY_SCHEMA,
                storage.OBSERVE_SCHEMA,
            )
            db.execute(
                "INSERT INTO profile(addr,status,reason,score,data_status,evidence_status) "
                "VALUES ('0xtest','active','ok',.5,'valid','qualified')"
            )
            db.commit()
            columns = storage.PROFILE_COLS.split(",")
            prior_row = db.execute(
                f"SELECT {storage.PROFILE_COLS} FROM profile WHERE addr='0xtest'"
            ).fetchone()
            prior = dict(zip(columns, prior_row))

            _status, _reason, artifact, _hit_cap = scanner._defer_profile(
                db, "0xtest", prior, "later", "temporary",
                generation_id="g-next", persist=False,
            )
            before = db.execute(
                "SELECT data_status FROM profile WHERE addr='0xtest'"
            ).fetchone()[0]
            self.assertEqual(before, "valid")

            scanner._persist_profile_batch(db, [artifact])
            after = db.execute(
                "SELECT data_status,profile_generation FROM profile WHERE addr='0xtest'"
            ).fetchone()
            self.assertEqual(after, ("deferred_data_error", "g-next"))


if __name__ == "__main__":
    unittest.main()
