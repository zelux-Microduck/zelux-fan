"""Contract smoke tests that do not need PWM/GPIO hardware."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("fanctl_under_test", str(ROOT / "fanctl"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
fanctl = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(fanctl)


class SocketContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name) / "pwm"
        self._sysfs_channel(root, 0)
        self._pwm_patch = patch.object(fanctl, "PWM_ROOT", root)
        self._pwm_patch.start()
        self.addCleanup(self._pwm_patch.stop)
        self._pin_gate_patch = patch.object(fanctl.Controller, "_validate_physical_pin", return_value=None)
        self._pin_gate_patch.start()
        self.addCleanup(self._pin_gate_patch.stop)
        self.controller = fanctl.Controller({"fan": {
            "inventory_file": str(Path(self._tmp.name) / "inventory.json"),
            "channel": [{
                "id": "cooling-main", "name": "Main", "pwm": 14,
                "chip": 0, "channel": 0, "tach": 0,
            }]}})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_status_publishes_stable_instance_id(self) -> None:
        response = self.controller.handle({"api_version": 1, "op": "status"})
        self.assertEqual(response["api_version"], 1)
        self.assertEqual([item["id"] for item in response["items"]], ["cooling-main"])

    def test_restart_discards_overrides_but_keeps_curve(self) -> None:
        ctrl = self.controller
        self.assertTrue(ctrl.handle({"op": "curve", "points": [[40, 75]]})["ok"])
        ctrl.handle({"op": "set", "instance_id": "cooling-main", "pct": 85, "hold_s": 0})
        restarted = fanctl.Controller({"fan": {"inventory_file": str(ctrl.inventory_file)}})
        self.assertEqual(restarted.mode, "auto")
        self.assertEqual(restarted._manual_overrides, {})
        self.assertEqual(restarted.curve, [(40.0, 75)] if isinstance(restarted.curve[0], tuple) else [[40.0, 75]])

    def test_expired_override_returns_to_curve_without_temperature_change(self) -> None:
        ctrl = self.controller
        ctrl.handle({"op": "set", "instance_id": "cooling-main", "pct": 85, "hold_s": 30})
        ctrl._manual_overrides["cooling-main"]["until"] = 0
        async def tick():
            task = asyncio.create_task(ctrl.auto_loop())
            await asyncio.sleep(0)
            ctrl.stop_auto()
            await task
        with patch.object(fanctl, "read_max_temp", return_value=20):
            asyncio.run(tick())
        self.assertEqual(ctrl.fans[0].pct(), ctrl.default_pct)
        self.assertNotIn("cooling-main", ctrl._manual_overrides)

    def test_enable_hardware_failure_preserves_disabled_record(self) -> None:
        ctrl = self.controller
        self.assertTrue(ctrl.handle({"op": "disable", "instance_id": "cooling-main"})["ok"])
        original = ctrl._inventory_copy()
        with patch.object(fanctl.PwmChannel, "apply", side_effect=RuntimeError("broken PWM")):
            result = ctrl.handle({"op": "enable", "instance_id": "cooling-main"})
        self.assertFalse(result["ok"])
        self.assertEqual(ctrl.inventory, original)
        self.assertEqual(json.loads(ctrl.inventory_file.read_text()), original)

    def test_period_save_failure_restores_properties_and_output(self) -> None:
        ctrl = self.controller
        before = ctrl._inventory_copy()
        instance = {**before["instances"][0], "name": "Changed", "period_ns": 50000}
        previous = ctrl.fans[0].pct()
        with patch.object(ctrl, "_save_inventory", side_effect=RuntimeError("disk full")):
            result = ctrl.handle({"op": "update", "instance_id": "cooling-main", "instance": instance})
        self.assertFalse(result["ok"])
        self.assertEqual(ctrl.fans[0].name, "Main")
        self.assertEqual(ctrl.fans[0].period_ns, before["instances"][0]["period_ns"])
        self.assertEqual(ctrl.fans[0].pct(), previous)

    def test_disable_clears_override_and_reenable_is_automatic(self) -> None:
        ctrl = self.controller
        ctrl.handle({"op": "set", "instance_id": "cooling-main", "pct": 85, "hold_s": 0})
        self.assertTrue(ctrl.handle({"op": "disable", "instance_id": "cooling-main"})["ok"])
        self.assertNotIn("cooling-main", ctrl._manual_overrides)
        self.assertTrue(ctrl.handle({"op": "enable", "instance_id": "cooling-main"})["ok"])
        self.assertEqual(ctrl.status()["items"][0]["control_mode"], "auto")

    def test_get_addresses_fan_by_stable_id(self) -> None:
        response = self.controller.handle({
            "api_version": 1, "op": "get", "instance_id": "cooling-main",
        })
        self.assertTrue(response["ok"])
        self.assertEqual(response["id"], "cooling-main")

    def test_fresh_config_does_not_guess_two_fans_or_pins(self) -> None:
        path = Path(self._tmp.name) / "fresh-inventory.json"
        ctrl = fanctl.Controller({"fan": {"inventory_file": str(path)}})
        self.assertEqual(ctrl.fans, [])
        self.assertEqual(ctrl.inventory["instances"], [])

    def test_unknown_instance_is_rejected(self) -> None:
        response = self.controller.handle({
            "api_version": 1, "op": "get", "instance_id": "missing",
        })
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "invalid_request")

    def test_unsupported_api_version_is_rejected(self) -> None:
        response = self.controller.handle({"api_version": 99, "op": "ping"})
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "unsupported_version")

    def _sysfs_channel(self, root: Path, channel: int) -> None:
        node = root / "pwmchip0" / f"pwm{channel}"
        node.mkdir(parents=True)
        (node / "enable").write_text("0")
        (node / "period").write_text("40000")
        (node / "duty_cycle").write_text("24000")

    def test_create_and_delete_persist_inventory_and_release_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sys-pwm"
            root.mkdir()
            self._sysfs_channel(root, 0)
            self._sysfs_channel(root, 1)
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [{"id": "cooling-main", "name": "Main", "pwm": -1,
                             "chip": 0, "channel": 0, "tach": 0}],
            }}
            with patch.object(fanctl, "PWM_ROOT", root):
                controller = fanctl.Controller(config)
                (root / "pwmchip0" / "pwm0" / "enable").write_text("1")
                instance = {"id": "cooling-aux", "name": "Aux", "driver_id": "zelux.fan.radxa-pwm",
                            "enabled": True, "pwm": {"chip": 0, "channel": 1}, "period_ns": 40000}
                created = controller.handle({"api_version": 1, "op": "create", "instance": instance})
                self.assertTrue(created["ok"], created)
                self.assertTrue(inventory_path.exists())
                self.assertEqual(len(json.loads(inventory_path.read_text())["instances"]), 2)
                deleted = controller.handle({"api_version": 1, "op": "delete",
                                             "instance_id": "cooling-aux", "confirm": True})
                self.assertTrue(deleted["ok"], deleted)
                self.assertTrue(deleted["deleted"])
                # PWM should be stopped but NOT unexported (fanctl never unexports PWM)
                self.assertEqual(deleted["released"], [{"ok": True, "resource": "pwmchip0/pwm1",
                                                         "stopped": True, "unexported": False}])
                self.assertEqual(len(json.loads(inventory_path.read_text())["instances"]), 1)
                self.assertEqual((root / "pwmchip0" / "pwm1" / "enable").read_text(), "0")
                self.assertEqual((root / "pwmchip0" / "pwm0" / "enable").read_text(), "1")

    def test_release_never_unexports_pwm_only_tach(self) -> None:
        """Release stops PWM but never unexports it; only owned tach GPIO is unexported."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pwm"
            node = root / "pwmchip0" / "pwm2"
            node.mkdir(parents=True)
            for key, value in (("enable", "1"), ("period", "40000"), ("duty_cycle", "20000")):
                (node / key).write_text(value)
            (root / "pwmchip0" / "unexport").write_text("")
            channel = fanctl.PwmChannel({"id": "cooling-test", "chip": 0, "channel": 2}, 40000)
            channel.path = lambda: node
            channel._exported_by_us = True  # Even if marked as exported by us
            with patch.object(fanctl, "PWM_ROOT", root):
                result = channel.release()
            self.assertTrue(result["ok"])
            # PWM should be stopped but NOT unexported
            self.assertEqual((node / "enable").read_text(), "0")
            self.assertEqual((node / "duty_cycle").read_text(), "0")
            self.assertEqual((root / "pwmchip0" / "unexport").read_text(), "")  # Not unexported
            # Result should indicate stopped=True, unexported=False
            released = result["resources"][0]
            self.assertTrue(released["stopped"])
            self.assertFalse(released["unexported"])

    def test_delete_requires_explicit_confirmation(self) -> None:
        response = self.controller.handle({
            "api_version": 1, "op": "delete", "instance_id": "cooling-main",
        })
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "invalid_request")

    def test_inventory_rejects_duplicate_pwm_ownership(self) -> None:
        instance = {"id": "cooling-main", "name": "Main", "driver_id": "zelux.fan.radxa-pwm",
                    "enabled": True, "pwm": {"chip": 1, "channel": 0}, "period_ns": 40000}
        inventory = {"schema": "zelux.fan-inventory/v1", "mode": "auto", "default_pct": 60,
                     "curve": [], "instances": [instance, {**instance, "id": "cooling-aux"}]}
        response = self.controller.handle({"api_version": 1, "op": "validate", "inventory": inventory})
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "resource_busy")

    def _setup_sysfs_with_tach(self, pwm_root: Path, gpio_root: Path, pwm_channel: int, gpio: int) -> None:
        """Setup mock sysfs with PWM channel and GPIO."""
        node = pwm_root / "pwmchip0" / f"pwm{pwm_channel}"
        node.mkdir(parents=True)
        (node / "enable").write_text("0")
        (node / "period").write_text("40000")
        (node / "duty_cycle").write_text("20000")
        gpio_node = gpio_root / f"gpio{gpio}"
        # Reuse existing GPIO node if present (preexisting GPIO scenario)
        if not gpio_node.exists():
            gpio_node.mkdir(parents=True)
        (gpio_node / "value").write_text("0")
        (gpio_node / "direction").write_text("in")

    def test_migration_persisted_on_first_startup(self) -> None:
        """No inventory file: derive stable inventory from config and save atomically on first startup."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            gpio_root = Path(tmp) / "sys-gpio"
            pwm_root.mkdir()
            gpio_root.mkdir()
            self._setup_sysfs_with_tach(pwm_root, gpio_root, 0, 150)
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [
                    {"id": "cooling-main", "name": "Main", "pwm": -1,
                     "chip": 0, "channel": 0, "tach": 150, "period_ns": 40000}
                ],
            }}
            with patch.object(fanctl, "PWM_ROOT", pwm_root), \
                 patch.object(fanctl, "GPIO_ROOT", gpio_root):
                # First startup - should create inventory file
                self.assertFalse(inventory_path.exists())
                controller = fanctl.Controller(config)
                self.assertTrue(inventory_path.exists())
                saved = json.loads(inventory_path.read_text())
                self.assertEqual(saved["schema"], "zelux.fan-inventory/v1")
                self.assertEqual(len(saved["instances"]), 1)
                self.assertEqual(saved["instances"][0]["id"], "cooling-main")
                self.assertEqual(saved["instances"][0]["tach_gpio"], 150)
                # Verify the instance is active
                self.assertEqual(len(controller.fans), 1)
                self.assertEqual(controller.fans[0].id, "cooling-main")

    def test_preexisting_gpio_not_unexported(self) -> None:
        """Service may unexport only GPIO it itself exported; preexisting GPIO must not be unexported."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            gpio_root = Path(tmp) / "sys-gpio"
            pwm_root.mkdir()
            gpio_root.mkdir()
            # Create preexisting GPIO (simulating another process exported it)
            preexisting_gpio = 149
            gpio_node = gpio_root / f"gpio{preexisting_gpio}"
            gpio_node.mkdir(parents=True)
            (gpio_node / "value").write_text("0")
            (gpio_node / "direction").write_text("in")
            (gpio_root / "unexport").write_text("")
            # Setup PWM
            self._setup_sysfs_with_tach(pwm_root, gpio_root, 0, preexisting_gpio)
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [
                    {"id": "cooling-main", "name": "Main", "pwm": -1,
                     "chip": 0, "channel": 0, "tach": preexisting_gpio, "period_ns": 40000}
                ],
            }}
            with patch.object(fanctl, "PWM_ROOT", pwm_root), \
                 patch.object(fanctl, "GPIO_ROOT", gpio_root):
                controller = fanctl.Controller(config)
                # Delete the instance - should NOT unexport GPIO since it was preexisting
                result = controller.handle({
                    "api_version": 1, "op": "delete",
                    "instance_id": "cooling-main", "confirm": True
                })
                self.assertTrue(result["ok"])
                # Check that GPIO was NOT unexported
                self.assertEqual((gpio_root / "unexport").read_text(), "")
                # GPIO should still exist
                self.assertTrue(gpio_node.exists())

    def test_same_pwm_tach_change_releases_old_and_tracks_new(self) -> None:
        """Update same PWM with tach config change must release removed old owned tach and track new."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            gpio_root = Path(tmp) / "sys-gpio"
            pwm_root.mkdir()
            gpio_root.mkdir()
            # Setup PWM channel (without tach initially)
            pwm_node = pwm_root / "pwmchip0" / "pwm0"
            pwm_node.mkdir(parents=True)
            (pwm_node / "enable").write_text("0")
            (pwm_node / "period").write_text("40000")
            (pwm_node / "duty_cycle").write_text("20000")
            # Create unexport file
            (gpio_root / "unexport").write_text("")
            old_tach = 150
            new_tach = 151
            # Do NOT create old_tach GPIO node - simulate service exported it via measure_hz
            # When we update to new_tach, the old_tach should be unexported
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [
                    {"id": "cooling-main", "name": "Main", "pwm": -1,
                     "chip": 0, "channel": 0, "tach": old_tach, "period_ns": 40000}
                ],
            }}
            with patch.object(fanctl, "PWM_ROOT", pwm_root), \
                 patch.object(fanctl, "GPIO_ROOT", gpio_root):
                controller = fanctl.Controller(config)
                # Simulate that measure_hz exported old_tach and tracked ownership
                fanctl._TACH_EXPORTED_BY_US.add(old_tach)
                # Update with new tach
                updated_instance = {
                    "id": "cooling-main", "name": "Main",
                    "driver_id": "zelux.fan.radxa-pwm",
                    "enabled": True,
                    "pwm": {"chip": 0, "channel": 0},
                    "period_ns": 40000,
                    "tach_gpio": new_tach
                }
                result = controller.handle({
                    "api_version": 1, "op": "update",
                    "instance_id": "cooling-main",
                    "instance": updated_instance
                })
                self.assertTrue(result["ok"])
                # Old tach should have been unexported (owned by us via measure_hz)
                self.assertEqual((gpio_root / "unexport").read_text(), str(old_tach))
                # Fan should now have new tach
                self.assertEqual(controller.fans[0].tach, new_tach)
                # Cleanup
                fanctl._TACH_EXPORTED_BY_US.discard(old_tach)

    def test_same_pwm_tach_change_preexisting_not_released(self) -> None:
        """Update same PWM with tach change: preexisting GPIO must not be unexported."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            gpio_root = Path(tmp) / "sys-gpio"
            pwm_root.mkdir()
            gpio_root.mkdir()
            old_tach = 150
            new_tach = 151
            # Create old tach GPIO as preexisting
            self._setup_sysfs_with_tach(pwm_root, gpio_root, 0, old_tach)
            (gpio_root / "unexport").write_text("")
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [
                    {"id": "cooling-main", "name": "Main", "pwm": -1,
                     "chip": 0, "channel": 0, "tach": old_tach, "period_ns": 40000}
                ],
            }}
            with patch.object(fanctl, "PWM_ROOT", pwm_root), \
                 patch.object(fanctl, "GPIO_ROOT", gpio_root):
                controller = fanctl.Controller(config)
                # Old tach is preexisting and not in _TACH_EXPORTED_BY_US, so it's not owned by us
                # Update with new tach
                updated_instance = {
                    "id": "cooling-main", "name": "Main",
                    "driver_id": "zelux.fan.radxa-pwm",
                    "enabled": True,
                    "pwm": {"chip": 0, "channel": 0},
                    "period_ns": 40000,
                    "tach_gpio": new_tach
                }
                result = controller.handle({
                    "api_version": 1, "op": "update",
                    "instance_id": "cooling-main",
                    "instance": updated_instance
                })
                self.assertTrue(result["ok"])
                # Old tach should NOT have been unexported (not owned)
                self.assertEqual((gpio_root / "unexport").read_text(), "")
                # Fan should now have new tach
                self.assertEqual(controller.fans[0].tach, new_tach)

    def test_disabled_instance_releases_active_channel_and_tach(self) -> None:
        """enabled->disabled releases active channel and tach."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            gpio_root = Path(tmp) / "sys-gpio"
            pwm_root.mkdir()
            gpio_root.mkdir()
            tach_gpio = 150
            self._setup_sysfs_with_tach(pwm_root, gpio_root, 0, tach_gpio)
            (pwm_root / "pwmchip0" / "unexport").write_text("")
            (gpio_root / "unexport").write_text("")
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [
                    {"id": "cooling-main", "name": "Main", "pwm": -1,
                     "chip": 0, "channel": 0, "tach": tach_gpio, "period_ns": 40000}
                ],
            }}
            with patch.object(fanctl, "PWM_ROOT", pwm_root), \
                 patch.object(fanctl, "GPIO_ROOT", gpio_root):
                controller = fanctl.Controller(config)
                # Enable the fan
                (pwm_root / "pwmchip0" / "pwm0" / "enable").write_text("1")
                # Update to disabled
                disabled_instance = {
                    "id": "cooling-main", "name": "Main",
                    "driver_id": "zelux.fan.radxa-pwm",
                    "enabled": False,
                    "pwm": {"chip": 0, "channel": 0},
                    "period_ns": 40000,
                    "tach_gpio": tach_gpio
                }
                result = controller.handle({
                    "api_version": 1, "op": "update",
                    "instance_id": "cooling-main",
                    "instance": disabled_instance
                })
                self.assertTrue(result["ok"])
                # Fan should be removed from active list
                self.assertEqual(len(controller.fans), 0)
                # PWM should be disabled
                self.assertEqual((pwm_root / "pwmchip0" / "pwm0" / "enable").read_text(), "0")

    def test_disabled_to_enabled_creates_and_applies(self) -> None:
        """disabled->enabled creates/applies new channel."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            gpio_root = Path(tmp) / "sys-gpio"
            pwm_root.mkdir()
            gpio_root.mkdir()
            self._setup_sysfs_with_tach(pwm_root, gpio_root, 1, 151)
            inventory_path = Path(tmp) / "fan-inventory.json"
            # Create inventory with disabled instance
            inventory = {
                "schema": "zelux.fan-inventory/v1",
                "mode": "auto",
                "default_pct": 60,
                "curve": [[50, 60], [70, 100]],
                "instances": [{
                    "id": "cooling-aux",
                    "name": "Aux",
                    "driver_id": "zelux.fan.radxa-pwm",
                    "enabled": False,
                    "pwm": {"chip": 0, "channel": 1},
                    "period_ns": 40000,
                    "tach_gpio": 151
                }]
            }
            inventory_path.write_text(json.dumps(inventory))
            config = {"fan": {"inventory_file": str(inventory_path)}}
            with patch.object(fanctl, "PWM_ROOT", pwm_root), \
                 patch.object(fanctl, "GPIO_ROOT", gpio_root):
                controller = fanctl.Controller(config)
                # Initially no active fans
                self.assertEqual(len(controller.fans), 0)
                # Enable the instance
                enabled_instance = {
                    **inventory["instances"][0],
                    "enabled": True
                }
                result = controller.handle({
                    "api_version": 1, "op": "update",
                    "instance_id": "cooling-aux",
                    "instance": enabled_instance
                })
                self.assertTrue(result["ok"])
                # Fan should now be active
                self.assertEqual(len(controller.fans), 1)
                self.assertEqual(controller.fans[0].id, "cooling-aux")

    def test_persistence_failure_preserves_state(self) -> None:
        """Persistence failure should preserve old inventory/config/other fans."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            gpio_root = Path(tmp) / "sys-gpio"
            pwm_root.mkdir()
            gpio_root.mkdir()
            self._setup_sysfs_with_tach(pwm_root, gpio_root, 0, 150)
            self._setup_sysfs_with_tach(pwm_root, gpio_root, 1, 151)
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [
                    {"id": "cooling-main", "name": "Main", "pwm": -1,
                     "chip": 0, "channel": 0, "tach": 150, "period_ns": 40000}
                ],
            }}
            with patch.object(fanctl, "PWM_ROOT", pwm_root), \
                 patch.object(fanctl, "GPIO_ROOT", gpio_root):
                controller = fanctl.Controller(config)
                # Mock _save_inventory to raise an exception
                original_save = controller._save_inventory
                def failing_save(inventory):
                    raise PermissionError("simulated write failure")
                controller._save_inventory = failing_save
                # Try to create a new instance - should fail due to mocked failure
                new_instance = {
                    "id": "cooling-aux", "name": "Aux",
                    "driver_id": "zelux.fan.radxa-pwm",
                    "enabled": True,
                    "pwm": {"chip": 0, "channel": 1},
                    "period_ns": 40000,
                    "tach_gpio": 151
                }
                result = controller.handle({
                    "api_version": 1, "op": "create",
                    "instance": new_instance
                })
                self.assertFalse(result["ok"])
                # Original fan should still be active
                self.assertEqual(len(controller.fans), 1)
                self.assertEqual(controller.fans[0].id, "cooling-main")
                # Restore original save method
                controller._save_inventory = original_save

    def test_explicit_config_takes_precedence_over_env(self) -> None:
        """Explicit TOML config values take precedence over environment variables."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            node = pwm_root / "pwmchip0" / "pwm0"
            node.mkdir(parents=True)
            (node / "enable").write_text("0")
            (node / "period").write_text("40000")
            (node / "duty_cycle").write_text("20000")

            # Set environment variables
            env_inventory = str(Path(tmp) / "env-inventory.json")
            with patch.dict(os.environ, {
                "FANCTL_INVENTORY_FILE": env_inventory,
                "FANCTL_SOCKET_GROUP": "env-group"
            }):
                # But provide explicit config values - these should win
                explicit_inventory = str(Path(tmp) / "explicit-inventory.json")
                config = {"fan": {
                    "inventory_file": explicit_inventory,
                    "socket_group": "explicit-group",
                    "channel": [{"id": "cooling-main", "name": "Main", "pwm": -1,
                                 "chip": 0, "channel": 0, "tach": 0}],
                }}
                with patch.object(fanctl, "PWM_ROOT", pwm_root):
                    controller = fanctl.Controller(config)
                    # Verify explicit values were used, not env vars
                    self.assertEqual(str(controller.inventory_file), explicit_inventory)
                    self.assertEqual(controller.group, "explicit-group")

    def test_env_vars_used_when_config_absent(self) -> None:
        """Environment variables are used when config keys are absent."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            node = pwm_root / "pwmchip0" / "pwm0"
            node.mkdir(parents=True)
            (node / "enable").write_text("0")
            (node / "period").write_text("40000")
            (node / "duty_cycle").write_text("20000")

            env_inventory = str(Path(tmp) / "env-inventory.json")
            with patch.dict(os.environ, {
                "FANCTL_INVENTORY_FILE": env_inventory,
                "FANCTL_SOCKET_GROUP": "env-group"
            }):
                # Config without inventory_file or socket_group
                config = {"fan": {
                    "channel": [{"id": "cooling-main", "name": "Main", "pwm": -1,
                                 "chip": 0, "channel": 0, "tach": 0}],
                }}
                with patch.object(fanctl, "PWM_ROOT", pwm_root):
                    controller = fanctl.Controller(config)
                    # Verify env vars were used
                    self.assertEqual(str(controller.inventory_file), env_inventory)
                    self.assertEqual(controller.group, "env-group")

    def test_standalone_defaults_when_no_config_no_env(self) -> None:
        """Standalone defaults are used when both config and env are absent."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            node = pwm_root / "pwmchip0" / "pwm0"
            node.mkdir(parents=True)
            (node / "enable").write_text("0")
            (node / "period").write_text("40000")
            (node / "duty_cycle").write_text("20000")

            # Patch the module constant to a temp path to avoid touching global /var/lib
            temp_inventory = str(Path(tmp) / "default-inventory.json")
            # Clear env vars (remove them entirely)
            env_clear = {key: "" for key in ["FANCTL_INVENTORY_FILE", "FANCTL_SOCKET_GROUP"]}
            with patch.dict(os.environ, env_clear, clear=False), \
                 patch.object(fanctl, "DEFAULT_INVENTORY_FILE", temp_inventory):
                # Config without inventory_file or socket_group
                config = {"fan": {
                    "channel": [{"id": "cooling-main", "name": "Main", "pwm": -1,
                                 "chip": 0, "channel": 0, "tach": 0}],
                }}
                with patch.object(fanctl, "PWM_ROOT", pwm_root):
                    controller = fanctl.Controller(config)
                    # Verify standalone defaults use patched constant
                    self.assertEqual(str(controller.inventory_file), temp_inventory)
                    self.assertEqual(controller.group, "")

    def test_explicit_empty_string_in_config_overrides_env(self) -> None:
        """Explicit empty string in config takes precedence over env vars."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            node = pwm_root / "pwmchip0" / "pwm0"
            node.mkdir(parents=True)
            (node / "enable").write_text("0")
            (node / "period").write_text("40000")
            (node / "duty_cycle").write_text("20000")

            with patch.dict(os.environ, {
                "FANCTL_SOCKET_GROUP": "env-group"
            }):
                # Config with explicit empty string
                config = {"fan": {
                    "socket_group": "",
                    "channel": [{"id": "cooling-main", "name": "Main", "pwm": -1,
                                 "chip": 0, "channel": 0, "tach": 0}],
                }}
                with patch.object(fanctl, "PWM_ROOT", pwm_root):
                    controller = fanctl.Controller(config)
                    # Explicit empty string wins over env var
                    self.assertEqual(controller.group, "")

    def test_gpio_export_race_not_added_to_ownership(self) -> None:
        """If GPIO node appears during export write that raises OSError, it must not be owned."""
        with tempfile.TemporaryDirectory() as tmp:
            gpio_root = Path(tmp) / "sys-gpio"
            gpio_root.mkdir()
            gpio_num = 123
            gpio_node = gpio_root / f"gpio{gpio_num}"
            # Node does not exist initially
            self.assertFalse(gpio_node.exists())
            # Clear ownership set
            fanctl._TACH_EXPORTED_BY_US.clear()
            # Store original write_text for delegation
            original_write_text = type(gpio_root).write_text
            # Create a mock export that raises OSError but simulates node appearing anyway
            def mock_export_write(path_obj, data, encoding=None, errors=None):
                # Only intercept the specific export path
                if str(path_obj) == str(gpio_root / "export"):
                    # Simulate another process exporting the GPIO
                    gpio_node.mkdir(parents=True)
                    (gpio_node / "value").write_text("0")
                    (gpio_node / "direction").write_text("in")
                    # Then raise OSError (as if write failed due to concurrent export)
                    raise OSError("device or resource busy")
                # Delegate to original for non-target paths
                return original_write_text(path_obj, data, encoding=encoding, errors=errors)
            with patch.object(fanctl, "GPIO_ROOT", gpio_root), \
                 patch.object(fanctl, "time") as mock_time, \
                 patch.object(type(gpio_root), "write_text", autospec=True, side_effect=mock_export_write):
                # Patch time.monotonic to return a fixed time (zero window)
                mock_time.monotonic.return_value = 0.0
                mock_time.sleep.side_effect = lambda _: None
                # Call measure_hz - should return None due to OSError
                result = fanctl.measure_hz(gpio_num, window=0.0)
                # Result should be None (OSError caught early)
                self.assertIsNone(result)
                # GPIO should NOT be in ownership set
                self.assertNotIn(gpio_num, fanctl._TACH_EXPORTED_BY_US)
            # Cleanup
            fanctl._TACH_EXPORTED_BY_US.discard(gpio_num)

    def test_low_level_apply_does_not_silently_export_uninitialized_pwm(self) -> None:
        """Low-level duty writes do not bypass the explicit activation/export lifecycle."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            # Create pwmchip but NOT the pwm channel (simulating unexported)
            chip_dir = pwm_root / "pwmchip0"
            chip_dir.mkdir()
            (chip_dir / "npwm").write_text("4")
            (chip_dir / "export").write_text("")
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [{"id": "cooling-main", "name": "Main", "pwm": -1,
                             "chip": 0, "channel": 1, "tach": 0}],
            }}
            with patch.object(fanctl, "PWM_ROOT", pwm_root):
                # Controller should fail to find the PWM channel
                with self.assertRaises(RuntimeError) as ctx:
                    controller = fanctl.Controller(config)
                    controller.fans[0].apply(24000)
                # The low-level apply call reports the missing channel.
                self.assertIn("PWM", str(ctx.exception))
                # Export is performed explicitly by instance activation/boot_apply,
                # never as a side effect of an arbitrary duty write.
                self.assertEqual((chip_dir / "export").read_text(), "")

    def test_per_instance_manual_override_isolation(self) -> None:
        """Manual set on one instance must not change global mode; other instances remain auto."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            # Create two PWM channels
            for ch in [0, 1]:
                node = pwm_root / "pwmchip0" / f"pwm{ch}"
                node.mkdir(parents=True)
                (node / "enable").write_text("0")
                (node / "period").write_text("40000")
                (node / "duty_cycle").write_text("20000")
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [
                    {"id": "cooling-main", "name": "Main", "pwm": -1, "chip": 0, "channel": 0},
                    {"id": "cooling-aux", "name": "Aux", "pwm": -1, "chip": 0, "channel": 1},
                ],
            }}
            with patch.object(fanctl, "PWM_ROOT", pwm_root):
                controller = fanctl.Controller(config)
                # Set manual override on first instance
                result = controller.handle({
                    "api_version": 1, "op": "set",
                    "instance_id": "cooling-main", "pct": 80, "hold_s": 60
                })
                self.assertTrue(result["ok"])
                # Global mode should remain "auto"
                self.assertEqual(controller.mode, "auto")
                # Status should show control_mode for each instance
                status = controller.handle({"api_version": 1, "op": "status"})
                self.assertEqual(status["mode"], "auto")
                items_by_id = {item["id"]: item for item in status["items"]}
                self.assertEqual(items_by_id["cooling-main"]["control_mode"], "manual")
                self.assertEqual(items_by_id["cooling-aux"]["control_mode"], "auto")
                # First instance should have override tracked
                self.assertIn("cooling-main", controller._manual_overrides)

    def test_auto_clears_manual_overrides(self) -> None:
        """Switching to auto mode clears all per-instance manual overrides."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            node = pwm_root / "pwmchip0" / "pwm0"
            node.mkdir(parents=True)
            (node / "enable").write_text("0")
            (node / "period").write_text("40000")
            (node / "duty_cycle").write_text("20000")
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [{"id": "cooling-main", "name": "Main", "pwm": -1,
                             "chip": 0, "channel": 0, "tach": 0}],
            }}
            with patch.object(fanctl, "PWM_ROOT", pwm_root):
                controller = fanctl.Controller(config)
                # Set manual override
                controller.handle({
                    "api_version": 1, "op": "set",
                    "instance_id": "cooling-main", "pct": 80, "hold_s": 60
                })
                self.assertIn("cooling-main", controller._manual_overrides)
                # Switch to auto
                result = controller.handle({"api_version": 1, "op": "auto"})
                self.assertTrue(result["ok"])
                self.assertEqual(result["mode"], "auto")
                # Overrides should be cleared
                self.assertEqual(controller._manual_overrides, {})
                # Status should show auto control
                status = controller.handle({"api_version": 1, "op": "status"})
                self.assertEqual(status["items"][0]["control_mode"], "auto")

    def test_enable_operation_by_stable_id(self) -> None:
        """enable operation enables a disabled instance by stable id."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            for ch in [0, 1]:
                node = pwm_root / "pwmchip0" / f"pwm{ch}"
                node.mkdir(parents=True)
                (node / "enable").write_text("0")
                (node / "period").write_text("40000")
                (node / "duty_cycle").write_text("20000")
            inventory_path = Path(tmp) / "fan-inventory.json"
            inventory = {
                "schema": "zelux.fan-inventory/v1",
                "mode": "auto",
                "default_pct": 60,
                "curve": [[50, 60]],
                "instances": [
                    {"id": "cooling-main", "name": "Main", "driver_id": "zelux.fan.radxa-pwm",
                     "enabled": True, "pwm": {"chip": 0, "channel": 0}, "period_ns": 40000},
                    {"id": "cooling-aux", "name": "Aux", "driver_id": "zelux.fan.radxa-pwm",
                     "enabled": False, "pwm": {"chip": 0, "channel": 1}, "period_ns": 40000}
                ]
            }
            inventory_path.write_text(json.dumps(inventory))
            config = {"fan": {"inventory_file": str(inventory_path)}}
            with patch.object(fanctl, "PWM_ROOT", pwm_root):
                controller = fanctl.Controller(config)
                # Initially only one active fan
                self.assertEqual(len(controller.fans), 1)
                # Enable the disabled instance
                result = controller.handle({
                    "api_version": 1, "op": "enable",
                    "instance_id": "cooling-aux"
                })
                self.assertTrue(result["ok"])
                # Now two active fans
                self.assertEqual(len(controller.fans), 2)
                self.assertEqual(controller.fans[1].id, "cooling-aux")
                # Inventory should be updated
                saved = json.loads(inventory_path.read_text())
                self.assertTrue(saved["instances"][1]["enabled"])

    def test_disable_operation_by_stable_id(self) -> None:
        """disable operation disables an enabled instance by stable id."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            node = pwm_root / "pwmchip0" / "pwm0"
            node.mkdir(parents=True)
            (node / "enable").write_text("1")
            (node / "period").write_text("40000")
            (node / "duty_cycle").write_text("20000")
            (pwm_root / "pwmchip0" / "unexport").write_text("")
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [{"id": "cooling-main", "name": "Main", "pwm": -1,
                             "chip": 0, "channel": 0, "tach": 0}],
            }}
            with patch.object(fanctl, "PWM_ROOT", pwm_root):
                controller = fanctl.Controller(config)
                # Initially one active fan
                self.assertEqual(len(controller.fans), 1)
                # Disable it
                result = controller.handle({
                    "api_version": 1, "op": "disable",
                    "instance_id": "cooling-main"
                })
                self.assertTrue(result["ok"])
                # No active fans now
                self.assertEqual(len(controller.fans), 0)
                # PWM should be stopped
                self.assertEqual((node / "enable").read_text(), "0")
                # Inventory should show disabled
                saved = json.loads(inventory_path.read_text())
                self.assertFalse(saved["instances"][0]["enabled"])

    def test_enable_already_enabled_returns_truthful_result(self) -> None:
        """Enabling an already-enabled instance returns ok with note."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            node = pwm_root / "pwmchip0" / "pwm0"
            node.mkdir(parents=True)
            (node / "enable").write_text("0")
            (node / "period").write_text("40000")
            (node / "duty_cycle").write_text("20000")
            inventory_path = Path(tmp) / "fan-inventory.json"
            config = {"fan": {
                "inventory_file": str(inventory_path),
                "channel": [{"id": "cooling-main", "name": "Main", "pwm": -1,
                             "chip": 0, "channel": 0, "tach": 0}],
            }}
            with patch.object(fanctl, "PWM_ROOT", pwm_root):
                controller = fanctl.Controller(config)
                # Instance is already enabled
                result = controller.handle({
                    "api_version": 1, "op": "enable",
                    "instance_id": "cooling-main"
                })
                self.assertTrue(result["ok"])
                self.assertEqual(result["note"], "already enabled")
                self.assertTrue(result["enabled"])

    def test_disable_already_disabled_returns_truthful_result(self) -> None:
        """Disabling an already-disabled instance returns ok with note."""
        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            node = pwm_root / "pwmchip0" / "pwm0"
            node.mkdir(parents=True)
            (node / "enable").write_text("0")
            (node / "period").write_text("40000")
            (node / "duty_cycle").write_text("20000")
            inventory_path = Path(tmp) / "fan-inventory.json"
            inventory = {
                "schema": "zelux.fan-inventory/v1",
                "mode": "auto",
                "default_pct": 60,
                "curve": [],
                "instances": [
                    {"id": "cooling-main", "name": "Main", "driver_id": "zelux.fan.radxa-pwm",
                     "enabled": False, "pwm": {"chip": 0, "channel": 0}, "period_ns": 40000}
                ]
            }
            inventory_path.write_text(json.dumps(inventory))
            config = {"fan": {"inventory_file": str(inventory_path)}}
            with patch.object(fanctl, "PWM_ROOT", pwm_root):
                controller = fanctl.Controller(config)
                # Instance is already disabled
                result = controller.handle({
                    "api_version": 1, "op": "disable",
                    "instance_id": "cooling-main"
                })
                self.assertTrue(result["ok"])
                self.assertEqual(result["note"], "already disabled")
                self.assertFalse(result["enabled"])

    def test_concurrent_create_requests_succeed_and_persist(self) -> None:
        """Concurrent create requests must all succeed and be persisted atomically."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with tempfile.TemporaryDirectory() as tmp:
            pwm_root = Path(tmp) / "sys-pwm"
            pwm_root.mkdir()
            # Create multiple PWM channels for concurrent creates
            num_instances = 5
            for i in range(num_instances + 1):
                node = pwm_root / "pwmchip0" / f"pwm{i}"
                node.mkdir(parents=True)
                (node / "enable").write_text("0")
                (node / "period").write_text("40000")
                (node / "duty_cycle").write_text("20000")

            inventory_path = Path(tmp) / "fan-inventory.json"
            # Start with one existing instance
            initial_inventory = {
                "schema": "zelux.fan-inventory/v1",
                "mode": "auto",
                "default_pct": 60,
                "curve": [[50, 60], [70, 100]],
                "instances": [{
                    "id": "cooling-main",
                    "name": "Main",
                    "driver_id": "zelux.fan.radxa-pwm",
                    "enabled": True,
                    "pwm": {"chip": 0, "channel": 0},
                    "period_ns": 40000,
                    "tach_gpio": None
                }]
            }
            inventory_path.write_text(json.dumps(initial_inventory))

            config = {"fan": {"inventory_file": str(inventory_path)}}
            with patch.object(fanctl, "PWM_ROOT", pwm_root):
                controller = fanctl.Controller(config)

                # Concurrently create multiple new instances on distinct PWM channels
                def create_instance(idx: int) -> dict:
                    instance = {
                        "id": f"cooling-aux-{idx}",
                        "name": f"Aux {idx}",
                        "driver_id": "zelux.fan.radxa-pwm",
                        "enabled": True,
                        "pwm": {"chip": 0, "channel": idx + 1},  # Use distinct channels
                        "period_ns": 40000,
                        "tach_gpio": None
                    }
                    return controller.handle({"api_version": 1, "op": "create", "instance": instance})

                results = []
                with ThreadPoolExecutor(max_workers=num_instances) as executor:
                    futures = [executor.submit(create_instance, i) for i in range(num_instances)]
                    for future in as_completed(futures):
                        results.append(future.result())

                # All create requests must succeed
                for i, result in enumerate(results):
                    self.assertTrue(result.get("ok", False), f"Request {i} failed: {result}")

                # Status should contain all instances
                status = controller.handle({"api_version": 1, "op": "status"})
                instance_ids = [item["id"] for item in status["items"]]
                self.assertIn("cooling-main", instance_ids)
                for i in range(num_instances):
                    self.assertIn(f"cooling-aux-{i}", instance_ids)

                # Inventory should have all instances persisted
                saved = json.loads(inventory_path.read_text())
                saved_ids = [inst["id"] for inst in saved["instances"]]
                self.assertEqual(len(saved_ids), num_instances + 1)  # initial + new
                self.assertIn("cooling-main", saved_ids)
                for i in range(num_instances):
                    self.assertIn(f"cooling-aux-{i}", saved_ids)


if __name__ == "__main__":
    unittest.main()
