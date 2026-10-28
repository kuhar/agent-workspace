"""Regression checks for topology validation and rollback after partial failure."""
import importlib.machinery
import importlib.util
import json
import pathlib
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1] / "scripts"
loader = importlib.machinery.SourceFileLoader("reserve", str(ROOT / "agent-cpu-reserve"))
spec = importlib.util.spec_from_loader(loader.name, loader)
h = importlib.util.module_from_spec(spec)
loader.exec_module(h)
spec = importlib.util.spec_from_file_location("profile", ROOT / "agent-benchmark-profile.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class TimeoutTests(unittest.TestCase):
    def test_default_and_explicit_timeouts(self):
        for options, expected in [([], 93600), (["--timeout", "1"], 1),
                                  (["--timeout", "7200"], 7200),
                                  (["--timeout", "93600"], 93600)]:
            with self.subTest(options=options), patch.object(p, "plan", return_value={}) as plan, \
                 patch("builtins.print"):
                self.assertEqual(p.main(["benchmark-plan", *options], h), 0)
                self.assertEqual(plan.call_args.args[0].timeout, expected)

    def test_out_of_range_timeout_is_rejected(self):
        for timeout in ("0", "93601"):
            with self.subTest(timeout=timeout), patch.object(p, "plan") as plan:
                with self.assertRaises(SystemExit):
                    p.main(["benchmark-plan", "--timeout", timeout], h)
                plan.assert_not_called()


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.groups = [(0, n, (n, n + 4)) for n in range(4)]
        self.stack = []
        for name, value in [("physical_core_groups", self.groups), ("online_cpus", set(range(8)))]:
            mock = patch.object(h, name, return_value=value)
            mock.start()
            self.stack.append(mock)

    def tearDown(self):
        for mock in self.stack:
            mock.stop()

    def args(self, **kw):
        return types.SimpleNamespace(cpus=None, cores=2, reserve_cpus=None, **kw)

    def test_default_uses_one_thread_but_reserves_both(self):
        selected, reserved = p.selection(self.args(), h)
        self.assertEqual(selected, {2, 3})
        self.assertEqual(reserved, {2, 3, 6, 7})

    def test_larger_pool_keeps_unused_physical_cores_exclusive(self):
        args = self.args()
        args.cpus, args.reserve_cpus = "1", "1-2"
        self.assertEqual(p.selection(args, h), ({1}, {1, 2, 5, 6}))

    def test_two_smt_threads_are_rejected(self):
        args = self.args()
        args.cpus = "1,5"
        with self.assertRaises(SystemExit):
            p.selection(args, h)

    def test_workload_outside_pool_is_rejected(self):
        args = self.args()
        args.cpus, args.reserve_cpus = "0", "1-2"
        with self.assertRaises(SystemExit):
            p.selection(args, h)

    def test_reserving_entire_machine_is_rejected(self):
        args = self.args()
        args.cpus = "0-3"
        with self.assertRaises(SystemExit):
            p.selection(args, h)


class RollbackTests(unittest.TestCase):
    def test_shared_guard_closes_admission_before_stopping_workloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config = {"scope": "test.scope", "session": True,
                      "uid": 1000, "guard": "test.service",
                      "scheduler": "native", "housekeeping": "0", "lock_wait_seconds": 0}
            p.atomic(root / "config.json", config)
            events = []
            broker = types.SimpleNamespace(
                serve=lambda: (_ for _ in ()).throw(RuntimeError("injected broker failure")),
                close=lambda: events.append("close"))
            module = types.SimpleNamespace(Broker=lambda *_: broker)
            state = {"previous": {}}
            # Notification is the only socket used here; admission has its own
            # real Unix socket tests in test_reservation_broker.py.
            with patch.object(h, "require_root"), patch.object(h, "LOCK_PATH", root / "lock"), \
                 patch.object(h, "STATE_PATH", root / "global.json"), \
                 patch.object(h, "broker_module", return_value=module), \
                 patch.object(h, "stop_unit", side_effect=lambda _: events.append("stop")), \
                 patch.object(p.os, "sched_setaffinity"), patch.object(p, "snapshot", return_value=state), \
                 patch.object(p, "apply"), patch.object(p, "verify"), \
                 patch.object(p, "restore", side_effect=lambda *_: events.append("restore")), \
                 patch.object(p.socket, "socket"), patch.dict(p.os.environ, NOTIFY_SOCKET="/unused"):
                with self.assertRaisesRegex(RuntimeError, "injected broker failure"):
                    p.guard(root, h)
            self.assertEqual(events, ["close", "stop", "restore"])

    def test_completed_exec_stop_post_does_not_contend_with_next_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            p.atomic(root / "state.json", {"restored": True})
            with patch.object(h, "require_root"), patch.object(p, "checked_directory", return_value=root), \
                 patch.object(h, "acquire_lock") as lock:
                self.assertEqual(p.main(["benchmark-restore", str(root)], h), 0)
                lock.assert_not_called()

    def test_restart_limit_is_reset_only_for_the_paused_service(self):
        results = [types.SimpleNamespace(returncode=1, stdout="", stderr="limited"),
                   types.SimpleNamespace(returncode=0, stdout="start-limit-hit\n", stderr=""),
                   types.SimpleNamespace(returncode=0, stdout="", stderr=""),
                   types.SimpleNamespace(returncode=0, stdout="", stderr="")]
        with patch.object(p, "command", side_effect=results) as commands:
            p.service(h, {}, {"unit": "ananicy-cpp.service", "user": False}, "start")
            self.assertEqual([c.args[2:] for c in commands.call_args_list],
                             [("start", "ananicy-cpp.service"),
                              ("show", "ananicy-cpp.service", "-p", "Result", "--value"),
                              ("reset-failed", "ananicy-cpp.service"), ("start", "ananicy-cpp.service")])

    def test_partial_apply_failure_restores_clocks_idle_and_cpu_sets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            policy = root / "policy"
            policy.mkdir()
            previous = {"scaling_min_freq": "500000", "scaling_max_freq": "5700000",
                        "scaling_governor": "powersave", "energy_performance_preference": "balance_performance",
                        "boost": "1"}
            for key, value in previous.items():
                (policy / key).write_text(value)
            (policy / "cpuinfo_min_freq").write_text("500000")
            idle = root / "idle"
            idle.write_text("0")
            desired = {**previous, "scaling_min_freq": "2500000", "scaling_max_freq": "2500000",
                       "scaling_governor": "performance", "energy_performance_preference": "performance", "boost": "0"}
            state = {"restored": False, "config": {"scope": "unused.scope", "reserved": "0", "policies": {str(policy): desired}},
                     "previous": {"user.slice": ""}, "frequency": {str(policy): previous},
                     "idle": {str(idle): "0"}, "services": []}
            p.atomic(root / "state.json", state)
            original = pathlib.Path.write_text
            def fail_one(path, value, *a, **kw):
                if path == policy / "scaling_max_freq" and value == "2500000":
                    raise OSError("injected policy failure")
                return original(path, value, *a, **kw)
            with patch.object(h, "restrict_units"), patch.object(pathlib.Path, "write_text", fail_one):
                with self.assertRaises(OSError):
                    p.apply(state, h)
            self.assertEqual(idle.read_text(), "1")
            with patch.object(h, "stop_unit"), patch.object(h, "restore_units", return_value=True) as restore, \
                 patch.object(h, "read_state", return_value={"benchmark_directory": str(root)}), \
                 patch.object(h, "clear_state") as clear:
                p.restore(root, h)
                restore.assert_called_once_with({"user.slice": ""})
                clear.assert_called_once()
            self.assertTrue(__import__("json").loads((root / "state.json").read_text())["restored"])
            self.assertEqual(idle.read_text(), "0")
            for key, value in previous.items():
                self.assertEqual((policy / key).read_text(), value)

    def test_failed_restore_keeps_recovery_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            p.atomic(root / "state.json", {"restored": False, "config": {"scope": "unused.scope"},
                                         "previous": {"system.slice": ""}, "frequency": {}, "idle": {}, "services": []})
            with patch.object(h, "stop_unit"), patch.object(h, "restore_units", return_value=False), \
                 patch.object(h, "clear_state") as clear:
                with self.assertRaises(RuntimeError):
                    p.restore(root, h)
                clear.assert_not_called()
            self.assertFalse(__import__("json").loads((root / "state.json").read_text())["restored"])


class PowerRestorationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.cpu = self.root / "cpu"
        self.original = {}
        for index, governor, epp in [(0, "performance", "performance"),
                                     (1, "powersave", "balance_power")]:
            policy = self.cpu / "cpufreq" / f"policy{index}"
            policy.mkdir(parents=True)
            previous = {"scaling_min_freq": "500000", "scaling_max_freq": "5700000",
                        "scaling_governor": governor, "energy_performance_preference": epp, "boost": "1"}
            for key, value in previous.items():
                (policy / key).write_text(value)
            (policy / "cpuinfo_min_freq").write_text("500000")
            self.original[str(policy)] = previous
        reserved = next(iter(self.original))
        self.config = {"scope": "test.scope", "reserved": "0", "uid": 1000,
                       "deep_idle_disabled": False,
                       "policies": {reserved: {**self.original[reserved], "scaling_min_freq": "2500000",
                                               "scaling_max_freq": "2500000", "boost": "0"}}}
        self.active_profile = "performance"
        self.now = 0
        self.on_sleep = lambda: None
        self.mock(p, "CPU", self.cpu)
        self.mock(p, "time", types.SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep))
        self.mock(p, "scheduler_state", return_value="disabled")
        self.mock(p.shutil, "which", return_value="/usr/bin/powerprofilesctl")
        self.commands = self.mock(p, "command", side_effect=self.power_command)
        self.services = self.mock(p, "service", side_effect=self.service)
        self.user_manager = self.mock(p, "user_manager", return_value=types.SimpleNamespace(stdout="active"))
        self.systemctl = self.mock(h, "systemctl_show", side_effect=lambda unit, _: (
            "active" if unit == "power-profiles-daemon.service" else "inactive"))
        self.read_state = self.mock(h, "read_state", return_value=None)
        self.mock(h, "snapshot_units", return_value={"user.slice": ""})
        self.mock(h, "restrict_units")
        self.mock(h, "restore_units", return_value=True)
        self.mock(h, "stop_unit")
        self.clear = self.mock(h, "clear_state")

    def mock(self, obj, name, *args, **kwargs):
        patcher = patch.object(obj, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()

    def reset_power(self):
        self.active_profile = "balanced"
        for policy in self.original:
            root = pathlib.Path(policy)
            (root / "scaling_governor").write_text("powersave")
            (root / "energy_performance_preference").write_text("balance_performance")

    def power_command(self, _h, _binary, action, *args):
        if action == "set":
            self.reset_power()
            self.active_profile = args[0]
        return types.SimpleNamespace(stdout=self.active_profile + "\n", returncode=0)

    def service(self, _h, _state, _entry, action):
        if action == "start":
            self.reset_power()

    def prepare(self):
        state = p.snapshot(self.config, h)
        p.atomic(self.root / "state.json", state)
        self.read_state.return_value = {"benchmark_directory": str(self.root)}
        p.apply(state, h)
        return state

    def assert_original(self):
        for policy, previous in self.original.items():
            for name, expected in previous.items():
                self.assertEqual(p.read(pathlib.Path(policy) / name), expected, f"{policy}/{name}")
        self.assertEqual(self.active_profile, "performance")

    def test_service_restart_preserves_profile_and_housekeeping_policy(self):
        self.prepare()
        p.restore(self.root, h)
        self.assert_original()
        self.clear.assert_called_once()
        self.assertTrue(json.loads((self.root / "state.json").read_text())["restored"])

    def test_late_desktop_policy_change_is_restored_before_success(self):
        self.prepare()
        def late_change():
            if self.now >= .4:
                self.reset_power()
                self.on_sleep = lambda: None
        self.on_sleep = late_change
        p.restore(self.root, h)
        self.assertGreaterEqual(self.now, .4)
        self.assert_original()

    def test_persistent_policy_changes_keep_recovery_state(self):
        self.prepare()
        self.on_sleep = self.reset_power
        with self.assertRaisesRegex(RuntimeError, "restoration incomplete"):
            p.restore(self.root, h)
        self.clear.assert_not_called()
        saved = json.loads((self.root / "state.json").read_text())
        self.assertFalse(saved["restored"])
        self.assertIn("did not settle", "; ".join(saved["restore_errors"]))

    def test_profile_failure_still_restores_raw_cpu_settings(self):
        self.prepare()
        self.commands.side_effect = RuntimeError("D-Bus unavailable")
        with self.assertRaisesRegex(RuntimeError, "restoration incomplete"):
            p.restore(self.root, h)
        for policy, previous in self.original.items():
            for name, expected in previous.items():
                self.assertEqual(p.read(pathlib.Path(policy) / name), expected)
        self.clear.assert_not_called()

    def test_inactive_power_services_need_no_profile_tool(self):
        self.systemctl.side_effect = None
        self.systemctl.return_value = "inactive"
        self.user_manager.return_value.stdout = "inactive"
        state = p.snapshot(self.config, h)
        self.assertEqual(set(state["frequency"]), set(self.config["policies"]))
        self.commands.assert_not_called()

    def test_unreadable_profile_fails_before_host_changes(self):
        self.commands.side_effect = RuntimeError("D-Bus unavailable")
        with self.assertRaisesRegex(RuntimeError, "D-Bus unavailable"):
            p.snapshot(self.config, h)
        self.services.assert_not_called()
        self.assert_original()


if __name__ == "__main__":
    unittest.main()
