"""Regression coverage for the 2026-11-20 fan closeout; fake sysfs only."""
import asyncio
import json
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import test_socket_contract as fixtures
fanctl = fixtures.fanctl


class CloseoutTests(unittest.TestCase):
    def test_board_identity_variants_and_rejections(self):
        for model, compatible, expected in [
            ("Radxa ZERO 3", ["radxa,zero3", "rockchip,rk3566"], True),
            ("Radxa ZERO 3", ["radxa,zero3-aic8800ds2", "rockchip,rk3566"], True),
            ("Radxa ZERO 3W", ["radxa,zero-3w", "rockchip,rk3566"], True),
            ("Radxa ZERO3W", [], True),
            ("Radxa ZERO 3E", ["radxa,zero3", "rockchip,rk3566"], False),
            ("Radxa ZERO 3W", ["radxa,zero-3e", "rockchip,rk3566"], False),
            ("Radxa ZERO 3W", ["unknown,board", "rockchip,rk3566"], False),
            ("Radxa ZERO 3", ["rockchip,rk3566"], False),
            ("Radxa ZERO 3", [], False), ("", [], False),
        ]:
            with self.subTest(model=model, compatible=compatible):
                self.assertEqual(fanctl.is_zero3w(model, compatible), expected)

    setUp = fixtures.SocketContractTests.setUp
    tearDown = fixtures.SocketContractTests.tearDown
    _sysfs_channel = fixtures.SocketContractTests._sysfs_channel

    def test_fanctl_rejects_unready_physical_pin_before_pwm_export(self):
        self._pin_gate_patch.stop()
        instance = {"id": "blocked", "name": "Blocked", "driver_id": "zelux.fan.radxa-pwm",
                    "enabled": True, "pwm_pin": "radxa-zero3w-pin24-pwm13-m1",
                    "pwm": {"chip": 0, "channel": 1}, "period_ns": 40000}
        readiness = {"mapped_pins": [{"id": instance["pwm_pin"], "ready": False,
                                       "chip": 0, "channel": 1}]}
        with patch.object(fanctl.Controller, "_pwm_readiness", return_value=readiness):
            with self.assertRaisesRegex(fanctl.FanBusyError, "尚未通过"):
                self.controller._make_instance(instance)
        self.assertFalse((self.controller.fans[0].path().parent / "pwm1").exists())

    def test_create_exports_and_delete_unexports_only_fanctl_owned_pwm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sys-pwm"
            chip = root / "pwmchip0"
            chip.mkdir(parents=True)
            (chip / "npwm").write_text("1")
            inventory = Path(tmp) / "inventory.json"
            ownership = Path(tmp) / "pwm-owned.json"
            controller = fanctl.Controller({"fan": {"inventory_file": str(inventory)}})
            real_write = fanctl.Path.write_text
            def sysfs_write(path, data, *args, **kwargs):
                value = str(data)
                if path.name == "export":
                    node = path.parent / f"pwm{value}"
                    node.mkdir()
                    for attr, initial in (("enable", "0"), ("period", "0"), ("duty_cycle", "0")):
                        (node / attr).write_text(initial)
                    return len(value)
                if path.name == "unexport":
                    node = path.parent / f"pwm{value}"
                    for attr in ("enable", "period", "duty_cycle"):
                        (node / attr).unlink()
                    node.rmdir()
                    return len(value)
                return real_write(path, data, *args, **kwargs)
            instance = {"id": "owned", "name": "Owned", "driver_id": "zelux.fan.radxa-pwm",
                        "enabled": True, "pwm": {"chip": 0, "channel": 0}, "period_ns": 40000}
            with patch.object(fanctl, "PWM_ROOT", root), \
                 patch.object(fanctl, "_PWM_OWNERSHIP_FILE", ownership), \
                 patch.object(fanctl, "_PWM_EXPORTED_BY_US", set()), \
                 patch.object(fanctl.Path, "write_text", sysfs_write):
                created = controller.handle({"op": "create", "instance": instance})
                self.assertTrue(created["ok"], created)
                self.assertTrue((chip / "pwm0").is_dir())
                self.assertEqual(__import__("json").loads(ownership.read_text()), [[0, 0]])
                # New service process reconstructs the channel from inventory and ledger.
                restarted = fanctl.Controller({"fan": {"inventory_file": str(inventory)}})
                self.assertTrue(restarted.fans[0]._exported_by_us)
                deleted = restarted.handle({"op": "delete", "instance_id": "owned", "confirm": True})
                self.assertTrue(deleted["ok"], deleted)
                self.assertFalse((chip / "pwm0").exists())
                self.assertTrue(deleted["released"][0]["unexported"])
                self.assertEqual(__import__("json").loads(ownership.read_text()), [])
    def test_pwm_readiness_maps_controller_by_device_identity(self):
        channel = self.controller.fans[0]
        (channel.path().parent / "npwm").write_text("1")
        original_resolve = fanctl.Path.resolve
        def resolve(path, *args, **kwargs):
            if path.name == "device":
                return fanctl.Path("/sys/devices/platform/fe700010.pwm")
            if path.name == "of_node":
                return fanctl.Path("/proc/device-tree/pwm13")
            if path.name == "pwm13":
                return fanctl.Path("/proc/device-tree/pwm13")
            return original_resolve(path, *args, **kwargs)
        original_read_bytes = fanctl.Path.read_bytes
        board_model = b"Radxa ZERO 3\0"
        def read_bytes(path):
            if path.name == "model" and path.parent.name == "device-tree":
                return board_model
            if path.name == "compatible" and path.parent.name == "device-tree":
                return b"radxa,zero3-aic8800ds2\0rockchip,rk3566\0"
            if path.parent.name == "__symbols__" and path.name == "pwm13":
                return b"/pwm13"
            return original_read_bytes(path)
        original_exists = fanctl.Path.exists
        def exists(path):
            return True if path.name == "model" and path.parent.name == "device-tree" else original_exists(path)
        with patch.object(fanctl, "_pinmux_active", return_value=True), \
             patch.object(fanctl.Path, "resolve", resolve), \
             patch.object(fanctl.Path, "read_bytes", read_bytes), \
             patch.object(fanctl.Path, "exists", exists):
            result = self.controller._pwm_readiness()
            board_model = b"Radxa ZERO 3E\0"
            unsupported = self.controller._pwm_readiness()
        self.assertEqual(unsupported["state"], "unsupported_board")
        self.assertFalse(any(pin["ready"] for pin in unsupported["mapped_pins"]))
        self.assertEqual(result["state"], "runtime_available", result)
        __import__("json").dumps(result)
        pin = next(p for p in result["mapped_pins"] if p["pin"] == 24)
        self.assertTrue(pin["ready"])
        self.assertEqual(pin["device"], "fe700010.pwm")
        self.assertFalse(next(p for p in result["mapped_pins"] if p["pin"] == 21)["registered"])

    def test_zero_period_is_initialized_before_duty(self):
        channel = self.controller.fans[0]
        node = channel.path()
        (node / "period").write_text("0")
        (node / "duty_cycle").write_text("0")
        writes = []
        original = channel._write
        def strict_write(name, value):
            if name in ("enable", "duty_cycle") and int((node / "period").read_text()) == 0:
                raise RuntimeError("EINVAL: zero period")
            writes.append((name, value))
            original(name, value)
        with patch.object(channel, "_write", side_effect=strict_write):
            channel.apply(12000)
        self.assertEqual(writes, [("period", 40000),
                                  ("duty_cycle", 12000), ("enable", 1)])

    def test_release_fresh_disabled_zero_period_does_not_write_invalid_state(self):
        channel = self.controller.fans[0]
        node = channel.path()
        (node / "period").write_text("0")
        (node / "enable").write_text("0")
        original = fanctl.Path.write_text
        def strict(path, *args, **kwargs):
            if path.parent == node and path.name in ("enable", "duty_cycle"):
                raise OSError(22, "zero-period apply rejected")
            return original(path, *args, **kwargs)
        with patch.object(fanctl.Path, "write_text", strict):
            result = channel.release()
        self.assertTrue(result["ok"], result)

    def test_pwm_and_fg_same_physical_pin_rejected_before_hardware_access(self):
        instance = {"id": "conflict", "name": "Conflict", "driver_id": "zelux.fan.radxa-pwm",
                    "enabled": True, "pwm_pin": "radxa-zero3w-pin21-pwm12-m1",
                    "pwm": {"chip": 1, "channel": 0}, "period_ns": 40000, "tach_gpio": 149}
        with patch.object(self.controller, "_make_instance", side_effect=AssertionError("hardware access")):
            result = self.controller.handle({"op": "create", "instance": instance})
        self.assertFalse(result["ok"])
        self.assertIn("冲突", result["error"])

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
