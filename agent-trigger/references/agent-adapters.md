# Agent return adapters

Read only the section for the active host. The supervisor's portable contract is a durable `event.json` plus one callback process; session resumption is harness-specific.

## Codex

The built-in adapter executes:

```text
codex queue --thread <SESSION_ID> --message <PROMPT>
```

`queue` routes the prompt to the app-server that owns the existing session. If a turn is active, the owner holds it until that turn completes and then begins the queued follow-up. Do not use `codex exec resume` for a live Desktop or TUI session: it starts a second client which conflicts with the owner's single writer.

A custom product that already owns an app-server connection can implement the same adapter by calling `turn/start` for an idle thread or `turn/steer` for an active one. Keep this transport in the Codex adapter; the experiment monitor remains agent-independent.

The adapter resolves the executable in this order: `CODEX_CLI_PATH`, the Codex binary bundled with the macOS ChatGPT desktop app, then `codex` from `PATH`. This prevents an older global CLI from being selected ahead of the desktop host that owns the task. `doctor` prints the selected executable; verify it before launch.

In current Codex desktop/CLI environments, `CODEX_THREAD_ID` or `CODEX_SESSION_ID` may be inherited by shell commands, so `--agent auto` can configure the callback without placing the ID in the command line used to launch the monitor. These environment variables are an observed host integration detail, not part of the open Agent Skills standard. If they are absent, pass `--session-id` explicitly.

`doctor` fails closed when the selected Codex executable does not expose `queue`. Set `CODEX_CLI_PATH` to the executable that owns or can reach the session, update Codex, or provide a custom callback. Do not silently fall back to `exec resume`.

Official references:

- https://learn.chatgpt.com/docs/non-interactive-mode
- https://learn.chatgpt.com/docs/app-server
- https://learn.chatgpt.com/docs/automations

## Claude Code

Pass the session ID captured by Claude Code or its Agent SDK:

```bash
python3 <skill-dir>/scripts/agent_trigger.py start \
  --agent claude --session-id SESSION_ID --cwd /absolute/project -- \
  python3 train.py
```

The callback executes:

```text
claude -p --resume <SESSION_ID> <PROMPT>
```

Claude Code persists sessions and documents resumption by ID. Its hooks are deterministic lifecycle callbacks from Claude to external commands, but they do not by themselves turn an arbitrary experiment exit into a new user message. The monitor therefore resumes the session explicitly after the process event.

Official references:

- https://code.claude.com/docs/en/cli-usage
- https://code.claude.com/docs/en/sessions
- https://code.claude.com/docs/en/hooks

## Pi coding agent

Pass the session ID or session file shown by `/session`:

```bash
python3 <skill-dir>/scripts/agent_trigger.py start \
  --agent pi --session-id SESSION_ID --cwd /absolute/project -- \
  python3 train.py
```

The callback executes:

```text
pi -p --session <SESSION_ID> <PROMPT>
```

For a product that keeps Pi alive in RPC mode, a stronger adapter can instead write a JSONL `prompt` command to the owning RPC process. Do not write directly to a random process stdin; the host must own and expose that channel.

Official references:

- https://pi.dev/docs/latest/sessions
- https://pi.dev/docs/latest/usage
- https://pi.dev/docs/latest/rpc

## Generic command callback

Use `--callback-config /absolute/path/callback.json`. The file is read and copied into `spec.json` before the experiment starts, so it may be temporary.

```json
{
  "argv": ["my-agent-resume", "--session", "abc123", "--event", "{event_file}"],
  "stdin": "{message}",
  "cwd": "{cwd}",
  "timeout_seconds": 7200
}
```

Supported placeholders are:

- `{job_id}`
- `{phase}`
- `{exit_code}`
- `{job_dir}`
- `{event_file}`
- `{status_file}`
- `{stdout_log}`
- `{stderr_log}`
- `{cwd}`
- `{message}`

The callback is executed as an argv array with `shell=False`. The same values are also exported as `AGENT_TRIGGER_<NAME>` environment variables. A nonzero callback exit is recorded but never changes the experiment's terminal phase.

## Portability of the skill package

The folder follows the open Agent Skills format (`SKILL.md`, optional `scripts/`, and `references/`). Install or symlink the same folder into a discovery location supported by the host:

- Codex: `~/.agents/skills/agent-trigger` or a repository `.agents/skills/agent-trigger`
- Claude Code: `~/.claude/skills/agent-trigger` or `.claude/skills/agent-trigger`
- Pi: `~/.agents/skills/agent-trigger`, `~/.pi/agent/skills/agent-trigger`, or `.pi/skills/agent-trigger`

The package format is portable; the asynchronous return transport still requires a resumable session or custom callback in each host.
