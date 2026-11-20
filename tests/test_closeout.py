"""Regression coverage for the 2026-11-20 fan closeout; fake sysfs only."""
import asyncio
import json
import threading
import time
import unittest
from unittest.mock import patch
import test_socket_contract as fixtures
fanctl = fixtures.fanctl


class CloseoutTests(unittest.TestCase):
    setUp = fixtures.SocketContractTests.setUp
    tearDown = fixtures.SocketContractTests.tearDown
    _sysfs_channel = fixtures.SocketContractTests._sysfs_channel
    def test_zero_period_is_initialized_before_duty(self):
        channel = self.controller.fans[0]
        node = channel.path()
        (node / "period").write_text("0")
        (node / "duty_cycle").write_text("0")
        writes = []
        original = channel._write
        def strict_write(name, value):
            if name == "duty_cycle" and int((node / "period").read_text()) == 0:
                raise RuntimeError("EINVAL: zero period")
            writes.append((name, value))
            original(name, value)
        with patch.object(channel, "_write", side_effect=strict_write):
            channel.apply(12000)
        self.assertEqual(writes, [("enable", 0), ("period", 40000),
                                  ("duty_cycle", 12000), ("enable", 1)])

    def test_stop_failure_does_not_continue_writing(self):
        channel = self.controller.fans[0]
        with patch.object(channel, "_write", side_effect=RuntimeError("permission denied")) as write:
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                channel.apply(12000)
        self.assertEqual(write.call_count, 1)

    def test_missing_pwm_can_be_deleted_without_export(self):
        channel = self.controller.fans[0]
        channel.chip, channel.channel, channel._path = 99, 0, None
        result = self.controller.handle({"op": "delete", "instance_id": channel.id, "confirm": True})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["released"][0]["missing"])
        self.assertEqual(self.controller.inventory["instances"], [])
        self.assertTrue(channel._retired)

    def test_missing_pwm_can_be_disabled(self):
        channel = self.controller.fans[0]
        channel.chip, channel.channel, channel._path = 99, 0, None
        result = self.controller.handle({"op": "disable", "instance_id": channel.id})
        self.assertTrue(result["ok"], result)
        self.assertFalse(self.controller.inventory["instances"][0]["enabled"])

    def test_present_pwm_write_failure_keeps_record(self):
        channel = self.controller.fans[0]
        original = fanctl.Path.write_text
        def denied(path, *args, **kwargs):
            if path.name == "enable":
                raise PermissionError("permission denied")
            return original(path, *args, **kwargs)
        with patch.object(fanctl.Path, "write_text", denied):
            result = self.controller.handle({"op": "delete", "instance_id": channel.id, "confirm": True})
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.controller.inventory["instances"]), 1)

    def test_status_never_samples_rpm_inline(self):
        channel = self.controller.fans[0]
        channel.tach = 149
        with patch.object(channel, "rpm", side_effect=AssertionError("inline sampling")):
            result = self.controller.status()
        self.assertIsNone(result["items"][0]["rpm"])
        self.assertEqual(result["items"][0]["rpm_reason"], "not_sampled")
        channel._rpm_sample = (1200, time.monotonic(), "")
        self.assertEqual(channel.info()["rpm"], 1200)
        channel._rpm_sample = (1200, time.monotonic() - 11, "")
        self.assertIsNone(channel.info()["rpm"])
        self.assertEqual(channel.info()["rpm_reason"], "stale")

    def test_slow_sampler_does_not_block_status_and_delete_fences_it(self):
        async def scenario():
            channel = self.controller.fans[0]
            channel.tach = 149
            entered, finish = threading.Event(), threading.Event()
            def slow_rpm():
                entered.set()
                if not finish.wait(3):
                    raise RuntimeError("test sample wait expired")
                return 1234
            with patch.object(channel, "rpm", side_effect=slow_rpm):
                sample = asyncio.create_task(asyncio.to_thread(channel.sample_rpm))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    result = await asyncio.wait_for(asyncio.to_thread(self.controller.status), 0.5)
                    self.assertIsNone(result["items"][0]["rpm"])
                    deletion = asyncio.create_task(asyncio.to_thread(self.controller.handle,
                        {"op": "delete", "instance_id": channel.id, "confirm": True}))
                    await asyncio.sleep(0)
                    finish.set()
                    self.assertTrue((await asyncio.wait_for(deletion, 2))["ok"])
                    await sample
                    self.assertTrue(channel._retired)
                    with patch.object(channel, "rpm", side_effect=AssertionError("retired sampler")):
                        channel.sample_rpm()
                finally:
                    finish.set()
                    await sample
        asyncio.run(scenario())

    def test_delete_persistence_failure_restores_live_channel(self):
        channel = self.controller.fans[0]
        with patch.object(self.controller, "_save_inventory", side_effect=RuntimeError("disk full")):
            result = self.controller.handle({"op": "delete", "instance_id": channel.id, "confirm": True})
        self.assertFalse(result["ok"])
        self.assertFalse(channel._retired)
        self.assertEqual(len(self.controller.fans), 1)
        self.assertEqual(len(self.controller.inventory["instances"]), 1)

    def test_busy_status_returns_immediately_and_ping_remains_available(self):
        entered, release = threading.Event(), threading.Event()
        def hold():
            with self.controller._inventory_lock:
                entered.set()
                release.wait(3)
        worker = threading.Thread(target=hold)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertEqual(self.controller.handle({"op": "status"})["code"], "resource_busy")
            self.assertTrue(self.controller.handle({"op": "ping"})["ok"])
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            worker.join(2)

    def test_tach_loop_stops_and_joins_samples(self):
        async def scenario():
            channel = self.controller.fans[0]
            channel.tach = 149
            entered, finish = threading.Event(), threading.Event()
            def sample():
                entered.set()
                finish.wait(2)
            with patch.object(channel, "sample_rpm", side_effect=sample) as sampler:
                task = asyncio.create_task(self.controller.tach_loop())
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    self.controller._stop.set()
                    finish.set()
                    await asyncio.wait_for(task, 2)
                    self.assertEqual(sampler.call_count, 1)
                finally:
                    finish.set()
                    self.controller._stop.set()
                    await task
        asyncio.run(scenario())

    def test_selftest_reports_restore_failure(self):
        channel = self.controller.fans[0]
        with patch.object(channel, "apply", side_effect=[None, RuntimeError("restore denied")]), patch.object(channel, "rpm", return_value=2000), patch.object(fanctl.time, "sleep"):
            result = self.controller.selftest()
        self.assertFalse(result["ok"])
        self.assertIn("restore denied", result["items"][0]["error"])

    def test_delete_preserves_disk_and_restore_errors(self):
        channel = self.controller.fans[0]
        with patch.object(self.controller, "_save_inventory", side_effect=RuntimeError("disk full")), patch.object(channel, "apply", side_effect=RuntimeError("restore denied")):
            result = self.controller.handle({"op": "delete", "instance_id": channel.id, "confirm": True})
        self.assertFalse(result["ok"])
        self.assertIn("disk full", result["error"])
        self.assertIn("restore denied", result["error"])

    def test_rollback_collects_all_failures_without_losing_tracking(self):
        channel = self.controller.fans.pop()
        with patch.object(self.controller, "_save_inventory", side_effect=RuntimeError("disk full")), patch.object(channel, "apply", side_effect=RuntimeError("restore denied")), patch.object(channel, "release", return_value={"ok": False, "error": "cleanup denied"}):
            details = self.controller._rollback_update(self.controller._inventory_copy(), channel, channel, 70)
        self.assertIn("disk full", details)
        self.assertIn("cleanup denied", details)
        self.assertIn("restore denied", details)
        self.assertIn(channel, self.controller.fans)
        self.assertNotIn("配置已回滚", details)
