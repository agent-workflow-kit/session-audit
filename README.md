# session-audit

Analyze Claude Code sessions after the fact: usage analytics from your transcripts, and model-written retros ("what went well and badly") with suggested improvements.

## Status

Designed, not implemented: the interface is [`docs/interface.md`](docs/interface.md) (0.1.0-design); the implementation plan is written next. Post-v1: not part of the v1 scope (architecture §19).

## Layer

L3 (workflow). Works fully standalone; no tmux, worktree tool or notifier is required.

## What it does

- Reads Claude Code's transcript files with a hardened incremental reader and keeps counts (tokens by model, tool calls, errors, denials, times, cost with a stated basis) in its own store. The store holds no conversation text, so usage history outlives Claude Code's roughly 30-day transcript cleanup.
- Answers usage questions from the store with no model call: `sessions`, `usage --by day|project|model|tool|label=...`, `show`, `export`.
- Runs retros on demand: builds a bounded, redacted, fenced digest of the sessions you choose, runs one sandboxed headless Claude session over it through claude-session, validates the artifact strictly and stores it. `retro show --json` is the contract other tools read.
- Takes no external action: it posts, pushes and writes notes nowhere.

## Commands

| Command | Purpose |
|---|---|
| `ingest` | Read new transcript bytes into the store; capture claude-session facts when installed (`--session` takes a claude-session id) |
| `sessions`, `usage`, `show` | Query the store |
| `retro run` | Run a retro over a scope (`--session`, `--label`, `--project`, `--since`, `--until`, `--kind`, `--outcome`) |
| `retro list`, `retro show`, `retro delete` | Read and remove stored retros |
| `prune` | Apply the retention keys |
| `export` | Print store rows as JSON lines |
| `validate [--probe]` | Offline configuration and environment check |
| `cron install\|uninstall` | A managed crontab block: `ingest` (every `--ingest-every`, default hourly), daily `prune`, and the optional weekly retro line |

Every command takes `--json`. The full interface, exit codes and refusal reasons are in [`docs/interface.md`](docs/interface.md).

## Dependencies

| Tool | Binding | Needed for | Install |
|---|---|---|---|
| claude-session, 0.45.2 or later | A (required for `retro run`, optional otherwise) | `retro run`; harness facts during `ingest` | `uv tool install git+https://github.com/agent-workflow-kit/claude-session` |

A retro also needs the sandbox prerequisites claude-session documents (bubblewrap and socat on Linux and WSL2) and claude-session 0.45.2 or later. A missing or older claude-session makes `retro run` exit 12 and name this install command.

## Quick start

```sh
uv tool install git+https://github.com/agent-workflow-kit/session-audit
session-audit ingest
session-audit usage --by day,model --since 30d
session-audit retro run --since 7d --project ~/dev/my-project --dry-run   # a retro needs claude-session; a dry run spends nothing
session-audit cron install --ingest-every 1h
```

## Configuration

`~/.config/session-audit/config.yaml` plus `config.d/*.yaml` (see `config.example.yaml` for every key and default). The safe defaults need no configuration. Useful keys: `time_zone`, `prices` (your own price table), `retro.model` (a cheaper model for routine retros), `redaction.mask_env` (names of variables whose values must never reach a digest), `retention.*`, `limits.retros_per_day`.

`retro run`, `retro delete`, `ingest`, `prune` and `cron` refuse to run when the config is a symlink, not yours, or writable by others.

## Environment variables

`SESSION_AUDIT_TIMEOUT` and `SESSION_AUDIT_MODEL` only. No credentials are read. `CLAUDE_CONFIG_DIR` adds a transcript root.

## Hooks

`pre-retro` (veto or escalate before a model run), `post-retro`, `post-attention` (`retro_failed`, `ingest_failing`). Payloads carry ids, counts and enums, never text. See the interface.

## Exit codes

The table is in [`docs/interface.md`](docs/interface.md#exit-codes), with the mapping from claude-session results in [Outcome mapping](docs/interface.md#outcome-mapping).

## Safety

A retro session reads only the digest, in a sandbox with no network, no setting sources, an environment allowlist and a permission profile that denies other awkit tools; it runs in claude-session's own session directory. Retro text is untrusted model output and must be fenced by anything that consumes it. See [Safety](docs/interface.md#safety).

## Documentation

- System architecture: [`../awkit-docs/architecture.md`](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- Tool design guide: [`../awkit-docs/design-guide.md`](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))
- This tool's design: [`docs/design.md`](docs/design.md)
- This tool's interface: [`docs/interface.md`](docs/interface.md)

Links into `awkit-docs` assume a sibling clone of that repository.

## License

MIT — see LICENSE.
