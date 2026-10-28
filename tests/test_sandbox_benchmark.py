"""Check sandbox startup routing without changing host settings."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class SandboxBenchmarkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.capture = self.root / "arguments.json"
        sudo = self.root / "sudo"
        sudo.write_text("#!/usr/bin/python3\nimport json, os, sys\n"
                        "from pathlib import Path\n"
                        "Path(os.environ['TEST_ARGUMENTS']).write_text(json.dumps(sys.argv[1:]))\n")
        sudo.chmod(0o755)
        self.helper = self.root / "helper"
        self.helper.write_text("#!/bin/sh\nexit 99\n")
        self.helper.chmod(0o755)
        settings = self.root / "paths.sh"
        settings.touch()
        self.env = dict(os.environ, PATH=f"{self.root}:/usr/bin:/bin",
                        JAKUB_ENV_PATHS=str(settings), AGENT_CPU_RESERVE_HELPER=str(self.helper),
                        TEST_ARGUMENTS=str(self.capture))
        self.env.pop("AGENT_SANDBOX_RESERVED_CHILD", None)
        self.env.pop("AGENT_SANDBOX_BENCHMARK_CHILD", None)

    def launch(self, *arguments):
        return subprocess.run([str(SCRIPTS / "agent-sandbox"), *arguments],
                              env=self.env, text=True, capture_output=True)

    def test_benchmark_daemon_uses_shared_stable_profile_and_memory_limit(self):
        arguments = ["codex", "--daemon", "--reserve-cores", "48", "--benchmark",
                     "--memory-limit-gb", "16", str(self.root)]
        result = self.launch(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(self.capture.read_text())
        self.assertEqual(actual[:3], ["--", str(self.helper), "benchmark"])
        self.assertEqual(actual[actual.index("--cores") + 1], "48")
        self.assertEqual(actual[actual.index("--memory-max") + 1], "16G")
        self.assertEqual(actual[actual.index("--scheduler") + 1], "bpfland")
        self.assertIn("--session", actual)
        boundary = actual.index("--", 1)
        self.assertEqual(actual[boundary + 1:],
                         ["/usr/bin/env", "AGENT_SANDBOX_BENCHMARK_CHILD=1",
                          str(SCRIPTS / "agent-sandbox"), *arguments])

    def test_plain_reservation_keeps_original_command_and_literal_arguments(self):
        arguments = ["codex", "--reserve-cores=2", str(self.root), "--", "literal $text; with spaces"]
        result = self.launch(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(self.capture.read_text())
        self.assertEqual(actual[:3], ["--", str(self.helper), "run"])
        boundary = actual.index("--", 1)
        self.assertEqual(actual[boundary + 1:], [str(SCRIPTS / "agent-sandbox"), *arguments])
        self.assertNotIn("--session", actual)

    def test_benchmark_requires_an_explicit_reservation(self):
        result = self.launch("codex", "--benchmark", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--benchmark requires --reserve-cores", result.stderr)
        self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()
