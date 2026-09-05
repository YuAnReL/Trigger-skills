#!/usr/bin/env python3
import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("agent_trigger.py")
TERMINAL = {"succeeded", "failed", "launch-failed", "exited"}

module_spec = importlib.util.spec_from_file_location("agent_trigger_module", SCRIPT)
agent_trigger = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(agent_trigger)


def wait_for_terminal(status_path: Path, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if status_path.exists():
            last = json.loads(status_path.read_text(encoding="utf-8"))
            callback_phase = last.get("callback", {}).get("status")
            if last.get("phase") in TERMINAL and callback_phase not in {"pending", "running", "configured"}:
                return last
        time.sleep(0.05)
    raise AssertionError("job did not finish: %r" % last)


class AgentTriggerTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )

    def test_child_exit_code_and_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self.run_cli(
                "start", "--state-dir", root, "--agent", "none", "--cwd", root, "--",
                sys.executable, "-c", "import sys; print('hello'); print('warn', file=sys.stderr)",
            )
            job_dir = Path(json.loads(result.stdout)["job_dir"])
            status = wait_for_terminal(job_dir / "status.json")
            self.assertEqual(status["phase"], "succeeded")
            self.assertEqual(status["exit_code"], 0)
            self.assertEqual(status["callback"]["status"], "disabled")
            self.assertIn("hello", (job_dir / "stdout.log").read_text(encoding="utf-8"))
            self.assertIn("warn", (job_dir / "stderr.log").read_text(encoding="utf-8"))
            event = json.loads((job_dir / "event.json").read_text(encoding="utf-8"))
            self.assertEqual(event["event"], "job.terminal")

    def test_launcher_returns_before_long_child_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            started = time.monotonic()
            result = self.run_cli(
                "start", "--state-dir", root, "--agent", "none", "--cwd", root, "--",
                sys.executable, "-c", "import time; time.sleep(1)",
            )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.5)
            job_dir = Path(json.loads(result.stdout)["job_dir"])
            status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
            self.assertNotIn(status.get("phase"), TERMINAL)
            self.assertEqual(wait_for_terminal(job_dir / "status.json")["phase"], "succeeded")

    def test_nonzero_exit_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self.run_cli(
                "start", "--state-dir", root, "--agent", "none", "--cwd", root, "--",
                sys.executable, "-c", "raise SystemExit(7)",
            )
            job_dir = Path(json.loads(result.stdout)["job_dir"])
            status = wait_for_terminal(job_dir / "status.json")
            self.assertEqual(status["phase"], "failed")
            self.assertEqual(status["exit_code"], 7)

    def test_command_callback_receives_event_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            callback_config = root / "callback.json"
            callback_config.write_text(json.dumps({
                "argv": [
                    sys.executable,
                    "-c",
                    "import os,pathlib; pathlib.Path(os.environ['AGENT_TRIGGER_JOB_DIR']).joinpath('callback-marker.txt').write_text(os.environ['AGENT_TRIGGER_PHASE'])",
                ],
                "timeout_seconds": 10,
            }), encoding="utf-8")
            result = self.run_cli(
                "start", "--state-dir", root, "--callback-config", callback_config, "--cwd", root, "--",
                sys.executable, "-c", "pass",
            )
            job_dir = Path(json.loads(result.stdout)["job_dir"])
            status = wait_for_terminal(job_dir / "status.json")
            self.assertEqual(status["callback"]["status"], "succeeded")
            self.assertEqual((job_dir / "callback-marker.txt").read_text(encoding="utf-8"), "succeeded")

    def test_watch_existing_pid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
            result = self.run_cli(
                "watch-pid", "--state-dir", root, "--agent", "none", "--cwd", root, str(process.pid),
            )
            process.wait(timeout=5)
            job_dir = Path(json.loads(result.stdout)["job_dir"])
            status = wait_for_terminal(job_dir / "status.json")
            self.assertEqual(status["phase"], "exited")
            self.assertIsNone(status["exit_code"])
            self.assertIn(
                status["wait_method"],
                {"kqueue", "pidfd", "windows-process-handle", "fallback-poll", "already-exited"},
            )

    def test_builtin_agent_callback_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = {
                "cwd": str(root),
                "job_id": "job-1",
                "phase": "succeeded",
                "exit_code": 0,
                "event_file": str(root / "event.json"),
                "status_file": str(root / "status.json"),
            }
            codex, codex_stdin, _, _ = agent_trigger.callback_command(
                {"type": "agent", "agent": "codex", "binary": "/test/codex", "session_id": "codex-id", "prompt": "done {job_id}"},
                context,
            )
            claude, claude_stdin, _, _ = agent_trigger.callback_command(
                {"type": "agent", "agent": "claude", "binary": "/test/claude", "session_id": "claude-id", "prompt": "done {job_id}"},
                context,
            )
            pi, pi_stdin, _, _ = agent_trigger.callback_command(
                {"type": "agent", "agent": "pi", "binary": "/test/pi", "session_id": "pi-id", "prompt": "done {job_id}"},
                context,
            )
            self.assertEqual(codex, [
                "/test/codex", "queue", "--thread", "codex-id", "--message", "done job-1",
            ])
            self.assertIsNone(codex_stdin)
            self.assertEqual(claude, ["/test/claude", "-p", "--resume", "claude-id", "done job-1"])
            self.assertIsNone(claude_stdin)
            self.assertEqual(pi, ["/test/pi", "-p", "--session", "pi-id", "done job-1"])
            self.assertIsNone(pi_stdin)

    def test_auto_detects_codex_queue_adapter(self):
        args = agent_trigger.build_parser().parse_args(["doctor", "--agent", "auto"])
        with mock.patch.dict(agent_trigger.os.environ, {
            "CODEX_THREAD_ID": "desktop-thread",
            "CODEX_APP_TOOLS_PIPE_PATH": "/tmp/codex-app.sock",
        }, clear=False), mock.patch.object(
            agent_trigger, "resolve_codex_binary", return_value="/test/codex"
        ), mock.patch.object(agent_trigger, "codex_supports_queue", return_value=True):
            callback = agent_trigger.build_callback(args)
        self.assertEqual(callback["type"], "agent")
        self.assertEqual(callback["agent"], "codex")
        self.assertEqual(callback["session_id"], "desktop-thread")
        self.assertEqual(callback["binary"], "/test/codex")


if __name__ == "__main__":
    unittest.main()
