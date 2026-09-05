#!/usr/bin/env python3
"""Detach a process watcher and resume an agent session on terminal state.

New commands run as children of a detached supervisor, so completion waiting
uses waitpid rather than a polling loop. Existing PIDs use native process
notification primitives where available. Only the Python standard library is
required.
"""

import argparse
import ctypes
import datetime as dt
import errno
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_PROMPT = (
    "The monitored job {job_id} reached terminal phase {phase} with exit code "
    "{exit_code}. Read {event_file} and {status_file}, inspect the experiment "
    "logs and artifacts from the original request, validate them, then continue "
    "the promised analysis and report. Do not rerun the experiment unless the "
    "original request authorized it."
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def default_state_dir() -> Path:
    override = os.environ.get("AGENT_TRIGGER_HOME")
    if override:
        return Path(override).expanduser().resolve() / "jobs"
    return Path.home() / ".agent-trigger" / "jobs"


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, os.getpid()))
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object in %s" % path)
    return value


def normalize_command(parts: Sequence[str]) -> List[str]:
    command = list(parts)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("missing command after --")
    if not all(isinstance(part, str) and part for part in command):
        raise ValueError("command argv must contain non-empty strings")
    return command


def make_job_id(name: Optional[str]) -> str:
    base = name or "job"
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.") or "job"
    base = base[:72]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "%s-%s-%s" % (base, stamp, uuid.uuid4().hex[:8])


def create_job_dir(state_dir: Path, requested_id: Optional[str], name: Optional[str]) -> Tuple[str, Path]:
    job_id = requested_id or make_job_id(name)
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("job id must match %s" % JOB_ID_RE.pattern)
    state_dir.mkdir(parents=True, exist_ok=True)
    job_dir = state_dir / job_id
    job_dir.mkdir(mode=0o700)
    return job_id, job_dir


def update_status(job_dir: Path, changes: Mapping[str, Any]) -> Dict[str, Any]:
    path = job_dir / "status.json"
    status = read_json(path) if path.exists() else {}
    status.update(changes)
    status["updated_at"] = utc_now()
    atomic_write_json(path, status)
    return status


def render(template: str, context: Mapping[str, Any]) -> str:
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace("{%s}" % key, str(value))
    return rendered


def is_git_worktree(path: Path) -> bool:
    current = path.resolve()
    for candidate in (current,) + tuple(current.parents):
        if (candidate / ".git").exists():
            return True
    return False


def resolve_codex_binary() -> Optional[str]:
    """Prefer the host application's Codex over an older PATH installation."""
    candidates = [os.environ.get("CODEX_CLI_PATH")]
    if sys.platform == "darwin":
        candidates.append("/Applications/ChatGPT.app/Contents/Resources/codex")
    candidates.append(shutil.which("codex"))
    seen = set()
    for value in candidates:
        if not value:
            continue
        candidate = str(Path(value).expanduser())
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def codex_supports_queue(binary: str) -> bool:
    """Check for the owner-aware session queue command without mutating state."""
    try:
        completed = subprocess.run(
            [binary, "queue", "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def load_command_callback(path: str) -> Dict[str, Any]:
    config = read_json(Path(path).expanduser().resolve())
    argv = config.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
        raise ValueError("callback config requires a non-empty string array named argv")
    stdin_value = config.get("stdin")
    if stdin_value is not None and not isinstance(stdin_value, str):
        raise ValueError("callback stdin must be a string when provided")
    cwd_value = config.get("cwd")
    if cwd_value is not None and not isinstance(cwd_value, str):
        raise ValueError("callback cwd must be a string when provided")
    timeout = config.get("timeout_seconds", 7200)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("callback timeout_seconds must be positive")
    return {
        "type": "command",
        "argv": argv,
        "stdin": stdin_value,
        "cwd": cwd_value,
        "timeout_seconds": timeout,
    }


def build_callback(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    if getattr(args, "callback_config", None):
        if args.agent not in ("auto", "none"):
            raise ValueError("use either --callback-config or a built-in --agent adapter, not both")
        return load_command_callback(args.callback_config)

    agent = args.agent
    session_id = getattr(args, "session_id", None)
    if agent == "none":
        return None
    if agent == "auto":
        codex_id = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
        if codex_id and resolve_codex_binary():
            agent = "codex"
            session_id = session_id or codex_id
        else:
            raise ValueError(
                "could not detect a resumable agent session; pass --agent and --session-id, "
                "or provide --callback-config"
            )
    if not session_id:
        if agent == "codex":
            session_id = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
        if not session_id:
            raise ValueError("--session-id is required for the %s adapter" % agent)
    binary = resolve_codex_binary() if agent == "codex" else shutil.which(agent)
    if not binary:
        raise ValueError("%s is not available" % agent)
    if agent == "codex" and not codex_supports_queue(binary):
        raise ValueError(
            "selected Codex does not support the owner-aware 'queue' command; "
            "update Codex, set CODEX_CLI_PATH, or provide --callback-config"
        )
    return {
        "type": "agent",
        "agent": agent,
        "session_id": session_id,
        "binary": binary,
        "prompt": args.resume_prompt,
        "timeout_seconds": args.callback_timeout,
    }


def callback_command(callback: Mapping[str, Any], context: Mapping[str, Any]) -> Tuple[List[str], Optional[str], str, float]:
    callback_type = callback["type"]
    timeout = float(callback.get("timeout_seconds", 7200))
    if callback_type == "command":
        argv = [render(part, context) for part in callback["argv"]]
        stdin_template = callback.get("stdin")
        stdin_value = render(stdin_template, context) if stdin_template is not None else None
        callback_cwd = callback.get("cwd") or context["cwd"]
        return argv, stdin_value, render(str(callback_cwd), context), timeout

    agent = callback["agent"]
    session_id = str(callback["session_id"])
    prompt = render(str(callback.get("prompt") or DEFAULT_PROMPT), context)
    callback_cwd = str(context["cwd"])
    if agent == "codex":
        binary = str(callback.get("binary") or resolve_codex_binary() or "codex")
        return [binary, "queue", "--thread", session_id, "--message", prompt], None, callback_cwd, timeout
    if agent == "claude":
        binary = str(callback.get("binary") or "claude")
        return [binary, "-p", "--resume", session_id, prompt], None, callback_cwd, timeout
    if agent == "pi":
        binary = str(callback.get("binary") or "pi")
        return [binary, "-p", "--session", session_id, prompt], None, callback_cwd, timeout
    raise ValueError("unsupported agent adapter: %s" % agent)


def callback_context(job_dir: Path, status: Mapping[str, Any]) -> Dict[str, Any]:
    exit_code = status.get("exit_code")
    base = {
        "job_id": status.get("job_id", job_dir.name),
        "phase": status.get("phase", "unknown"),
        "exit_code": "unknown" if exit_code is None else exit_code,
        "job_dir": str(job_dir),
        "event_file": str(job_dir / "event.json"),
        "status_file": str(job_dir / "status.json"),
        "stdout_log": str(job_dir / "stdout.log"),
        "stderr_log": str(job_dir / "stderr.log"),
        "cwd": status.get("cwd", str(Path.cwd())),
    }
    base["message"] = render(DEFAULT_PROMPT, base)
    return base


def write_event(job_dir: Path, status: Mapping[str, Any]) -> None:
    event = {
        "schema_version": SCHEMA_VERSION,
        "event": "job.terminal",
        "emitted_at": utc_now(),
        "job": dict(status),
    }
    atomic_write_json(job_dir / "event.json", event)


def invoke_callback(job_dir: Path, callback: Optional[Mapping[str, Any]], status: Dict[str, Any]) -> Dict[str, Any]:
    if callback is None:
        status = update_status(job_dir, {"callback": {"status": "disabled"}})
        write_event(job_dir, status)
        return status

    callback_state: Dict[str, Any] = {"status": "running", "started_at": utc_now()}
    if callback.get("type") == "agent":
        callback_state["agent"] = callback.get("agent")
        callback_state["session_id"] = callback.get("session_id")
    status = update_status(job_dir, {"callback": callback_state})
    write_event(job_dir, status)
    context = callback_context(job_dir, status)
    stdout_path = job_dir / "callback.stdout.log"
    stderr_path = job_dir / "callback.stderr.log"
    try:
        argv, stdin_value, cwd, timeout = callback_command(callback, context)
        environment = os.environ.copy()
        for key, value in context.items():
            environment["AGENT_TRIGGER_%s" % key.upper()] = str(value)
        with stdout_path.open("ab") as stdout_handle, stderr_path.open("ab") as stderr_handle:
            completed = subprocess.run(
                argv,
                input=stdin_value.encode("utf-8") if stdin_value is not None else None,
                stdout=stdout_handle,
                stderr=stderr_handle,
                cwd=cwd,
                env=environment,
                timeout=timeout,
                check=False,
            )
        callback_state.update({
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode,
            "finished_at": utc_now(),
            "argv": argv,
        })
    except subprocess.TimeoutExpired:
        callback_state.update({"status": "failed", "error": "callback timed out", "finished_at": utc_now()})
    except Exception as exc:
        callback_state.update({"status": "failed", "error": "%s: %s" % (type(exc).__name__, exc), "finished_at": utc_now()})
    status = update_status(job_dir, {"callback": callback_state})
    write_event(job_dir, status)
    return status


def terminalize(job_dir: Path, phase: str, exit_code: Optional[int], extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    changes: Dict[str, Any] = {
        "phase": phase,
        "exit_code": exit_code,
        "completed_at": utc_now(),
        "terminal": True,
        "callback": {"status": "pending"},
    }
    if extra:
        changes.update(extra)
    status = update_status(job_dir, changes)
    write_event(job_dir, status)
    spec = read_json(job_dir / "spec.json")
    return invoke_callback(job_dir, spec.get("callback"), status)


def supervise(job_dir: Path) -> int:
    spec = read_json(job_dir / "spec.json")
    update_status(job_dir, {
        "phase": "starting",
        "monitor_pid": os.getpid(),
        "monitor_started_at": utc_now(),
    })
    try:
        with (job_dir / "stdout.log").open("ab") as stdout_handle, (job_dir / "stderr.log").open("ab") as stderr_handle:
            process = subprocess.Popen(
                spec["command"],
                cwd=spec["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                close_fds=True,
            )
            update_status(job_dir, {"phase": "running", "pid": process.pid, "started_at": utc_now()})
            exit_code = process.wait()
        terminalize(job_dir, "succeeded" if exit_code == 0 else "failed", exit_code)
        return 0
    except Exception as exc:
        terminalize(job_dir, "launch-failed", None, {"error": "%s: %s" % (type(exc).__name__, exc)})
        return 1


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith(("linux", "darwin")):
        try:
            result = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            if result.returncode != 0 or result.stdout.strip().startswith("Z"):
                return False
        except Exception:
            pass
    return True


def process_identity(pid: int) -> Optional[str]:
    if sys.platform.startswith("linux"):
        try:
            fields = Path("/proc/%s/stat" % pid).read_text(encoding="utf-8").split()
            return "linux-starttime:%s" % fields[21]
        except Exception:
            return None
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            value = result.stdout.strip()
            return "darwin-lstart:%s" % value if value else None
        except Exception:
            return None
    return None


def wait_existing_pid(pid: int, fallback_interval: float) -> str:
    if not process_exists(pid):
        return "already-exited"

    if sys.platform == "darwin" and hasattr(select, "kqueue"):
        queue = select.kqueue()
        try:
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                fflags=select.KQ_NOTE_EXIT,
            )
            queue.control([event], 0, 0)
            queue.control(None, 1, None)
            return "kqueue"
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return "already-exited"
        finally:
            queue.close()

    if sys.platform.startswith("linux") and hasattr(os, "pidfd_open"):
        try:
            descriptor = os.pidfd_open(pid, 0)
            try:
                poller = select.poll()
                poller.register(descriptor, select.POLLIN)
                poller.poll()
                return "pidfd"
            finally:
                os.close(descriptor)
        except ProcessLookupError:
            return "already-exited"
        except (PermissionError, OSError):
            pass

    if os.name == "nt":
        synchronize = 0x00100000
        infinite = 0xFFFFFFFF
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return "already-exited"
        try:
            ctypes.windll.kernel32.WaitForSingleObject(handle, infinite)
            return "windows-process-handle"
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    identity = process_identity(pid)
    while process_exists(pid):
        if identity is not None and process_identity(pid) != identity:
            break
        time.sleep(fallback_interval)
    return "fallback-poll"


def watch_pid(job_dir: Path) -> int:
    spec = read_json(job_dir / "spec.json")
    pid = int(spec["pid"])
    update_status(job_dir, {
        "phase": "running" if process_exists(pid) else "starting",
        "monitor_pid": os.getpid(),
        "monitor_started_at": utc_now(),
        "pid": pid,
        "process_identity": process_identity(pid),
    })
    method = wait_existing_pid(pid, float(spec.get("fallback_interval", 5.0)))
    terminalize(job_dir, "exited", None, {"wait_method": method})
    return 0


def daemon_flags() -> Dict[str, Any]:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def launch_supervisor(job_dir: Path, internal_command: str) -> int:
    script = str(Path(__file__).resolve())
    with (job_dir / "monitor.log").open("ab") as monitor_handle:
        process = subprocess.Popen(
            [sys.executable, script, internal_command, "--job-dir", str(job_dir)],
            stdin=subprocess.DEVNULL,
            stdout=monitor_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **daemon_flags()
        )
    return process.pid


def initial_status(job_id: str, name: Optional[str], cwd: str, mode: str) -> Dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "name": name,
        "mode": mode,
        "cwd": cwd,
        "phase": "starting",
        "terminal": False,
        "created_at": now,
        "updated_at": now,
        "callback": {"status": "configured"},
    }


def command_start(args: argparse.Namespace) -> int:
    command = normalize_command(args.command)
    callback = build_callback(args)
    cwd = str(Path(args.cwd).expanduser().resolve())
    if not Path(cwd).is_dir():
        raise ValueError("working directory does not exist: %s" % cwd)
    state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir()
    job_id, job_dir = create_job_dir(state_dir, args.job_id, args.name)
    spec = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "name": args.name,
        "mode": "child",
        "cwd": cwd,
        "command": command,
        "callback": callback,
        "created_at": utc_now(),
    }
    atomic_write_json(job_dir / "spec.json", spec)
    atomic_write_json(job_dir / "status.json", initial_status(job_id, args.name, cwd, "child"))
    monitor_pid = launch_supervisor(job_dir, "_supervise")
    print(json.dumps({
        "job_id": job_id,
        "job_dir": str(job_dir),
        "monitor_pid": monitor_pid,
        "status_file": str(job_dir / "status.json"),
        "event_file": str(job_dir / "event.json"),
    }, ensure_ascii=False))
    return 0


def command_watch_pid(args: argparse.Namespace) -> int:
    if args.pid <= 0:
        raise ValueError("pid must be positive")
    callback = build_callback(args)
    cwd = str(Path(args.cwd).expanduser().resolve())
    if not Path(cwd).is_dir():
        raise ValueError("working directory does not exist: %s" % cwd)
    state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir()
    job_id, job_dir = create_job_dir(state_dir, args.job_id, args.name)
    spec = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "name": args.name,
        "mode": "existing-pid",
        "cwd": cwd,
        "pid": args.pid,
        "fallback_interval": args.fallback_interval,
        "callback": callback,
        "created_at": utc_now(),
    }
    atomic_write_json(job_dir / "spec.json", spec)
    status = initial_status(job_id, args.name, cwd, "existing-pid")
    status["pid"] = args.pid
    atomic_write_json(job_dir / "status.json", status)
    monitor_pid = launch_supervisor(job_dir, "_watch_pid")
    print(json.dumps({
        "job_id": job_id,
        "job_dir": str(job_dir),
        "monitor_pid": monitor_pid,
        "watched_pid": args.pid,
        "status_file": str(job_dir / "status.json"),
        "event_file": str(job_dir / "event.json"),
    }, ensure_ascii=False))
    return 0


def resolve_job_dir(job: str, state_dir: Optional[str]) -> Path:
    candidate = Path(job).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    root = Path(state_dir).expanduser().resolve() if state_dir else default_state_dir()
    return (root / job).resolve()


def command_status(args: argparse.Namespace) -> int:
    job_dir = resolve_job_dir(args.job, args.state_dir)
    status_path = job_dir / "status.json"
    if not status_path.exists():
        raise ValueError("status file not found: %s" % status_path)
    print(json.dumps(read_json(status_path), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_list(args: argparse.Namespace) -> int:
    root = Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir()
    jobs: List[Dict[str, Any]] = []
    if root.exists():
        for status_path in root.glob("*/status.json"):
            try:
                jobs.append(read_json(status_path))
            except Exception as exc:
                jobs.append({"job_id": status_path.parent.name, "phase": "unreadable", "error": str(exc)})
    jobs.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    print(json.dumps(jobs, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    callback = build_callback(args)
    result: Dict[str, Any] = {
        "ok": True,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "state_dir": str(Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir()),
        "callback": callback,
    }
    if callback and callback.get("type") == "agent":
        result["adapter_binary"] = callback.get("binary")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def add_callback_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent",
        choices=("auto", "none", "codex", "claude", "pi"),
        default="auto",
    )
    parser.add_argument("--session-id", help="Agent session/thread ID; Codex can auto-detect it")
    parser.add_argument("--callback-config", help="JSON file defining a generic argv callback")
    parser.add_argument("--resume-prompt", default=DEFAULT_PROMPT, help="Prompt template sent to the resumed agent")
    parser.add_argument("--callback-timeout", type=float, default=7200, help="Seconds allowed for the resume callback")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    doctor = subparsers.add_parser("doctor", help="Validate the callback before launching a job")
    add_callback_arguments(doctor)
    doctor.add_argument("--state-dir")
    doctor.set_defaults(handler=command_doctor)

    start = subparsers.add_parser("start", help="Launch and supervise a new command in the background")
    start.add_argument("--name")
    start.add_argument("--job-id")
    start.add_argument("--state-dir")
    start.add_argument("--cwd", default=str(Path.cwd()))
    add_callback_arguments(start)
    start.add_argument("command", nargs=argparse.REMAINDER)
    start.set_defaults(handler=command_start)

    watcher = subparsers.add_parser("watch-pid", help="Attach a detached watcher to an existing PID")
    watcher.add_argument("--name")
    watcher.add_argument("--job-id")
    watcher.add_argument("--state-dir")
    watcher.add_argument("--cwd", default=str(Path.cwd()))
    watcher.add_argument("--fallback-interval", type=float, default=5.0)
    add_callback_arguments(watcher)
    watcher.add_argument("pid", type=int)
    watcher.set_defaults(handler=command_watch_pid)

    status = subparsers.add_parser("status", help="Print one job status")
    status.add_argument("job", help="Job ID or job directory")
    status.add_argument("--state-dir")
    status.set_defaults(handler=command_status)

    listing = subparsers.add_parser("list", help="List job status snapshots")
    listing.add_argument("--state-dir")
    listing.set_defaults(handler=command_list)

    internal_supervise = subparsers.add_parser("_supervise")
    internal_supervise.add_argument("--job-dir", required=True)
    internal_supervise.set_defaults(handler=lambda args: supervise(Path(args.job_dir).resolve()))

    internal_watch = subparsers.add_parser("_watch_pid")
    internal_watch.add_argument("--job-dir", required=True)
    internal_watch.set_defaults(handler=lambda args: watch_pid(Path(args.job_dir).resolve()))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
