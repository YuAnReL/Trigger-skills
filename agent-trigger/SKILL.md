---
name: agent-trigger
description: Run or attach to a long-running experiment without blocking the active agent, persist its terminal event, and resume the originating Codex, Claude Code, Pi, or custom agent session for promised follow-up analysis. Use when the user says to run something and come back, continue when it finishes, notify them on completion, or avoid sleep-based polling. Do not use for short commands that can finish in the current turn.
---

# Agent Trigger

Use the bundled supervisor for long-running local commands. It detaches immediately, waits for the child with operating-system process primitives, records a durable terminal event, and invokes one configured resume callback.

## Choose the execution path

- For a new long-running command, launch it with `scripts/agent_trigger.py start`. This is the preferred path because the supervisor is the process parent and records the real exit code.
- For a process that is already running, use `watch-pid`. The watcher records that the process exited but cannot recover its exit code.
- For a short command that can reasonably complete during the current turn, run it normally and continue the analysis in the same turn.
- Do not keep the agent turn alive with `sleep`, a shell polling loop, or repeated status calls after a detached watcher has been registered.

## Establish the return path before launching

Run `doctor` first. A request to return and continue is not satisfied by a monitor with no callback.

```bash
python3 <skill-dir>/scripts/agent_trigger.py doctor --agent auto
```

`auto` detects a resumable Codex session from `CODEX_THREAD_ID` or `CODEX_SESSION_ID` and verifies that the selected Codex executable supports owner-aware message queuing. For Claude Code and Pi, pass both `--agent` and the session ID explicitly unless the host exposes a stable session identifier. For another harness, provide `--callback-config`.

If no verified resume transport is available, fail closed before starting the experiment. When the host provides a native task follow-up or heartbeat mechanism, it may be used as a fallback to inspect the durable event file and resume this task. State clearly that this is scheduled polling rather than an immediate process callback.

Read [references/agent-adapters.md](references/agent-adapters.md) only when selecting or configuring a return adapter.

## Start a monitored experiment

Put all launcher options before `--`; everything after `--` is the experiment argv and is executed without a shell.

```bash
python3 <skill-dir>/scripts/agent_trigger.py start \
  --name training-run \
  --agent auto \
  --cwd /absolute/project/path \
  -- \
  python3 train.py --config configs/experiment.yaml
```

For an explicit session:

```bash
python3 <skill-dir>/scripts/agent_trigger.py start \
  --name training-run \
  --agent claude \
  --session-id SESSION_ID \
  --cwd /absolute/project/path \
  -- \
  python3 train.py
```

Confirm that the launcher returns JSON containing `job_id`, `job_dir`, and `monitor_pid`. Read the initial `status.json` once to verify `phase` is `starting` or `running`, then yield the turn. Do not claim the experiment itself has completed at launch time.

The default state directory is `~/.agent-trigger/jobs`; override it with `--state-dir` or `AGENT_TRIGGER_HOME` when durable state must live elsewhere. The job directory contains:

- `spec.json`: immutable launch and callback specification
- `status.json`: latest atomic status snapshot
- `event.json`: durable terminal event, created only after completion
- `stdout.log` and `stderr.log`: experiment output for launched children
- `callback.stdout.log` and `callback.stderr.log`: resumed-agent output
- `monitor.log`: supervisor failures or diagnostics

## Attach to an existing PID

```bash
python3 <skill-dir>/scripts/agent_trigger.py watch-pid \
  --name existing-run \
  --agent codex \
  --session-id SESSION_ID \
  --cwd /absolute/project/path \
  12345
```

The watcher uses kqueue on macOS, pidfd on Linux, and a process handle on Windows. It falls back to low-frequency polling only inside the detached watcher when the platform lacks a native primitive. Never describe an attached PID as successful merely because it disappeared; report the phase as `exited` and determine success from the experiment's own artifacts or logs.

## Handle the resumed turn

When the completion prompt arrives:

1. Read `event.json` and `status.json`; distinguish experiment phase from callback status.
2. Inspect the actual logs and artifacts promised in the original request.
3. Validate outputs before interpreting results. A zero exit code alone is not proof that an experiment produced valid artifacts.
4. Continue the original requested analysis and deliver the report. Do not stop at “the process finished.”
5. Do not rerun the experiment or broaden permissions unless the original request authorized it.

The callback is one-shot. Codex queues the message to the owner of the existing session; Claude Code and Pi resume through their own CLIs. If delivery fails, the event remains durable and `status.json` records `callback.status: failed`; report partial completion rather than fabricating a successful return.

## Safety and scope

- Preserve the original sandbox, approval, and side-effect boundaries in the resumed agent process.
- Use argv arrays, not implicit shell evaluation. If a shell pipeline is truly required, make the shell invocation explicit in the experiment command.
- Never place API keys or bearer tokens in callback argv or `spec.json`. Use the target tool's existing authenticated session or a purpose-built wrapper that reads secrets from protected storage.
- Use a unique job per experiment. Do not register multiple callbacks for the same process unless the user explicitly requests fan-out.
