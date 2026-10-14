"""Regression checks for topology validation and rollback after partial failure."""
import importlib.machinery
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
