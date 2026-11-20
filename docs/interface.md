# session-audit: interface

Status: 0.1.0-design. This file is the single normative source for the tool's rules; `docs/design.md`, the README and the architecture link to its headings and never restate its tables. Section numbers written as "architecture §N" and "design guide §N" point at `awkit-docs/architecture.md` and `awkit-docs/design-guide.md`.

Key words: **must**, **should**, **may** as in the design guide.

## Overview

session-audit analyzes Claude Code sessions after the fact. It has two halves that never mix.

- **Deterministic half** (`ingest`, `sessions`, `usage`, `show`, `export`, `prune`): reads Claude Code's transcript files with a hardened incremental reader, stores counts (never conversation text) in its own store, and answers usage questions from the store. No model is called.
- **Model half** (`retro run`, `retro list|show|delete`): builds a bounded, redacted, fenced digest of a chosen set of sessions, runs one headless, sandboxed model session over it through claude-session, validates the artifact strictly and stores the retro. The retro contains findings ("what went well and badly") and improvement candidates. It never changes anything outside the tool's own store.

The tool takes no external action: it does not post, push, merge, create tickets or write notes. Another tool (self-improve, later) may read a stored retro through `retro show --json`; session-audit does not know that tool exists.

Dependencies: claude-session (binding A; optional for the deterministic half, required for `retro run`). Nothing else. Third-party Python packages: `pyyaml` and `pydantic` only.

## Concepts

- **Transcript**: a JSONL file Claude Code writes, one per session, with subagent files beside it. session-audit only reads them. Their format is Claude Code's, not ours: every field is optional and unknown fields and line types are ignored.
- **Session**: one top-level transcript, identified by the `sessionId` Claude Code writes in its lines (a UUID). A resumed or compacted session is one session. A session id is shown in full and accepted as a unique prefix of at least 8 characters (exit 21 when ambiguous).
- **Agent**: a subagent transcript linked to its parent session by the parent's tool-call id. It has its own tool counts and usage rows.
- **Usage row**: one row per (day, session, agent or main, model) with four token counters: `input`, `output`, `cache_read`, `cache_write`.
- **Harness snapshot**: facts copied once from claude-session when a transcript matches one of its sessions (see Harness enrichment). Tagged `source: harness`.
- **Retro**: a stored result of `retro run`, identified by `ra-` followed by 8 lowercase base32 characters (`^ra-[a-z2-7]{8}$`; a unique prefix of at least 4 characters after `ra-` is accepted).
- **Digest**: the bounded, redacted, fenced text built from the store and transcripts for one retro. Items have ids `s<N>.i<NNN>` (session number within the retro, item number); metrics have ids `s<N>.<metric>` and `total.<metric>`.
- **Store**: `state.db` (private SQLite, WAL) holding sessions, agents, usage rows, harness snapshots, retros and ingest bookkeeping. See Files.

## Global flags

Where meaningful on every command: `--json`, `--config PATH`, `--verbose`, `--quiet`, `--dry-run`, `--yes`, `--version`. `--version` prints the version from package metadata and never `0.0.0`.

Commands that change the store accept `--actor ACTOR` (default `human:<user>` from a terminal, otherwise `auto:unknown`; `<user>` is `me.name`). The actor is written to `log.jsonl`. No command takes an external action, so `--decision-id` is accepted only on `retro run` and records the ids that hook output returned.

Errors go to stderr. With `--json` they are also printed on stdout as `{"error": {"code": "...", "message": "...", "details": {...}}}` (`schemas/cli/error.json`). A refusal (exit 20) carries `details.reason`.

Free text that could be untrusted (a retro title filter, a reason) is accepted as `--<name>-file PATH|-` where it is longer than a label value; this tool has no command that takes agent-generated text in argv.

## Exit codes

| Code | Meaning in session-audit |
|---|---|
| 0 | Success. `ingest` also exits 0 for a partial run that hit its budget (`complete: false`) and for "already running" |
| 1 | Unexpected internal error |
| 2 | Usage error, including `retro run --mode interactive` in 0.x (`details.reason: mode_unsupported`) |
| 10 | Not found: a session, retro or artifact id |
| 11 | A `pre-retro` hook vetoed the run |
| 12 | The requested operation failed for a reportable reason: claude-session missing or too old, sandbox unavailable, store unwritable, every transcript root unreadable, or a retro session that never started (`details.reason`, `details.retryable`) |
| 13 | Needs a human: a `pre-retro` hook escalated, or the retro session reported blocked or needs-input (retro state `escalated`) |
| 14 to 18 | The retro session's outcome passed through unchanged from claude-session: 14 exited-unreported, 15 killed, 16 timeout, 17 lost, 18 reported failed |
| 20 | Refused, with `details.reason` (see Refusal reasons) |
| 21 | Ambiguous selector (a session or retro prefix) |
| 22 | The retro session finished but its artifact is missing or invalid (retro state `failed`); `details.reason` names the failed check |
| 130, 143 | The command was interrupted by SIGINT or SIGTERM after stopping its own work cleanly |

## Refusal reasons

| `reason` | When |
|---|---|
| `scope_too_large` | A retro scope matches more sessions than `retro.max_sessions` (the count is in `details.matched`) |
| `daily_limit` | `limits.retros_per_day` retros already started today |
| `config_unsafe` | The config, its directory or a drop-in is a symlink, not owned by the user, or group- or world-writable |
| `live_sessions_only` | Every matched session changed within `retro.quiet_after` |
| `empty_scope` | The scope matches no stored session |
| `store_newer` | `state.db` was written by a newer version |
| `already_exists` | `retro run` found a stored retro with the same key and no `--again` (not an error: see `retro run`) |
| `in_use` | `retro delete` on a retro whose run is still live |

## Commands

### `ingest`

`session-audit ingest [--since WHEN] [--path PATH]... [--session CSESSION_ID] [--no-harness] [--rebuild] [--budget DURATION] [--dry-run] [--json]`

Reads new transcript bytes into the store and, when claude-session is installed, captures harness snapshots. Idempotent. Safe under concurrent invocation: it takes a lease; a second caller exits 0 with `already_running: true`.

- `--since`: only consider transcript files modified after this time (a duration meaning that long ago, or an ISO 8601 time, UTC when no offset). Default: every file under the roots.
- `--path`: restrict to files under this root (repeatable; each must be under a configured root).
- `--session CSESSION_ID`: capture the harness snapshot of this claude-session session and ingest its transcript files only. This is the target of the hook recipe in Harness enrichment.
- `--no-harness`: skip claude-session entirely for this run.
- `--rebuild`: re-read every transcript still on disk from the start and replace its rows. Rows of transcripts no longer on disk are kept. Requires `--yes` when stdin is not a terminal.
- `--budget`: overall time budget for the run (default `ingest.budget`, 300 s). A run that hits it stops cleanly, exits 0 with `complete: false`, and the next run resumes.
- `--dry-run`: reads and parses but writes nothing; prints what would change.

`--json` output: `schemas/cli/ingest.json`. It carries `complete`, `already_running`, `files` (`seen`, `new`, `changed`, `unchanged`, `unsafe`, `excluded`, `vanished`), `lines` (`parsed`, `errors`), `sessions` (`new`, `updated`), `harness` (`enabled`, `matched`, `captured`, `errors`) and `warnings`.

### `sessions`

`session-audit sessions [--since WHEN] [--until WHEN] [--project PATH] [--label K=V]... [--kind K] [--outcome O] [--entrypoint interactive|headless] [--model M] [--sort start|tokens|cost|active] [--limit N] [--json]`

Lists stored sessions, newest first by default. Reads the store only. `--project` is a path prefix matched on a path boundary. `--label` matches harness labels (AND across flags, a flag matches any value of its key). `--kind` and `--outcome` match harness facts. `--limit` default 100, at most 1000. Output: `{"sessions": [...], "truncated": bool}` (`schemas/cli/sessions.json`). Each row has the fields of the Session object (see `--json` outputs).

### `usage`

`session-audit usage --by DIM[,DIM] [filters as for sessions] [--tz ZONE] [--limit N] [--json]`

Deterministic analytics over the store. Dimensions: `day`, `week`, `month`, `project`, `model`, `kind`, `entrypoint`, `tool`, `session`, `label=<key>`. At most two dimensions. `--tz` overrides `time_zone` for day, week and month bucketing. Output: `{"by": [...], "rows": [...], "totals": {...}, "truncated": bool}` (`schemas/cli/usage.json`). Each row has `key`, `sessions`, `tokens` (the four counters), `cost` (see Cost), `active_seconds`, `tool_calls`, `tool_errors`, `denials`, `interruptions`, `flags` (a list including `cost_mismatch`). A row never mixes cost bases silently.

### `show`

`session-audit show SESSION_ID [--json]`

One stored session: the Session object, its agents, its usage rows by day and model, and its harness snapshot. Exit 10 when unknown, 21 when the prefix is ambiguous. `--json`: `schemas/cli/show.json`.

### `retro run`

`session-audit retro run SCOPE [--skill NAME] [--mode headless] [--model M] [--timeout DURATION] [--max-turns N] [--again] [--hook NAME=CMD]... [--decision-id ID]... [--dry-run] [--actor ACTOR] [--json]`

Runs one retro. Blocks until the retro is final. `SCOPE` is one or more of `--session ID` (repeatable), `--label K=V` (repeatable), `--project PATH`, `--since WHEN`, `--until WHEN`, `--kind K`, `--outcome O`. At least one scope flag is required (exit 2 otherwise). The flow, checks and states are in Retro lifecycle, Retro launch and Retro validation.

- `--skill`: any skill Claude Code accepts as a slash command; default `retro.skill` (`session-audit:audit`).
- `--mode`: only `headless` is accepted in 0.x. `interactive` exits 2 with `details.reason: mode_unsupported`.
- `--again`: run the model even when a stored retro has the same key; the new retro records `again_of`.
- `--dry-run`: builds the digest in memory and prints the plan (matched sessions, skipped sessions with reasons, item counts, size, the key, the claude-session command's flags without the prompt) and exits 0. Nothing is stored and no model is called. It never prints digest text.

Exit codes: see Exit codes. When a stored retro with the same key exists and `--again` is absent, the command exits 0 and prints that retro with `reused: true`.

`--json` output: `schemas/cli/retro-run.json` (the Retro object plus `reused`, `phase` and `outcome` when a session ran).

### `retro list`

`session-audit retro list [--state S]... [--since WHEN] [--limit N] [--json]`

Lists retros, newest first. Reads the store only; never checks liveness. Output `{"retros": [...], "truncated": bool}` (`schemas/cli/retro-list.json`). Each row has a stable string `id`, `state`, `created_at`, `updated_at`, `finished_at`, the scope summary, `findings` and `candidates` counts and `key`. `--limit` default 100, at most 1000.

### `retro show`

`session-audit retro show RETRO_ID [--digest] [--json]`

Prints one retro. Default is readable text; `--json` prints the Retro object including the artifact (`schemas/cli/retro-show.json`). This is the contract other tools read. `--digest` also prints the stored digest if it has not been pruned; the digest is untrusted text, sanitized and printed inside its fence. Exit 10 when unknown.

### `retro delete`

`session-audit retro delete RETRO_ID... [--yes] [--json]`

Deletes retros and their digests. Confirms on a terminal; without a terminal it needs `--yes`. Exit 20 `in_use` for a retro whose run is live.

### `prune`

`session-audit prune [--dry-run] [--json]`

Applies the retention keys (see Retention) in one transaction. Never touches transcripts. Retention is configured policy, so it does not ask for confirmation; automation may run it. Output `schemas/cli/prune.json`.

### `export`

`session-audit export {sessions|usage|retros} [--since WHEN] [--json]`

Prints store rows as JSON lines (one object per line, oldest first). Output holds the same fields as the matching read commands and no text beyond a retro's artifact. With `--json` the lines are the objects themselves (there is no wrapper object); without it the format is the same.

### `validate`

`session-audit validate [--probe] [--json]`

Offline and read-only by default; never prompts. Checks the config and its ownership, the transcript roots, the store (opens read-only, migrations needed), the price table age (warns when `as_of` is older than 180 days), the permission profile file, the bundled plugin directory and whether claude-session is installed and new enough. Output `{"ok": bool, "checks": [{"name", "ok", "reason"}], "warnings": [...]}` (`schemas/cli/validate.json`). Exit 0 for ok (including warnings), 2 for an invalid config, 12 for a failed check. `--probe` additionally runs `claude-session kinds --json` and the sandbox availability check.

### `cron`

`session-audit cron install [--ingest-every DURATION] [--env-file PATH] [--dry-run] [--json]` and `session-audit cron uninstall [--dry-run] [--json]`. See Cron.

## Ingest rules

These rules are normative for the reader.

1. **Roots.** The transcript roots are `~/.claude/projects` and, when set, `$CLAUDE_CONFIG_DIR/projects`. The config key `transcript_roots` replaces both. Nothing outside the roots is read.
2. **Opening.** Every file is opened without following symlinks (each path component, not just the last), must be a regular file with one link and owned by the user. A directory on the way that is a symlink, or a root writable by another user, makes that root unusable (reported, other roots continue). Any other violation skips the file and counts it as `files.unsafe`.
3. **Limits.** A record over 8 MiB is skipped and counted. A file has a read budget and the whole run has the `--budget`; hitting either stops reading cleanly and the next run resumes.
4. **Lines.** Only lines ending in a line feed are parsed. A trailing partial line is left for the next run. A line that is not a JSON object is counted in `lines.errors` and skipped. NUL bytes inside a line make it an error line.
5. **Progress.** For each file the store keeps the byte offset, the inode, the size and a hash of the first line. If the file shrank, its inode changed or the first-line hash differs, the file is read again from the start and its rows are replaced in one transaction. Rows are written in one transaction per file read.
6. **Self-exclusion.** Transcripts whose project directory is the retro scratch working directory (see Retro launch) are skipped and counted in `files.excluded`, so a retro never audits itself.
7. **Unknown shapes.** Unknown line types, unknown keys and missing keys are never fatal. A session that lacks the fields a rule needs gets a quality flag (`no_usage`, `no_timestamps`) instead.
8. **Store content.** The store holds counts, ids, timestamps, model names, tool names, project paths and harness facts. It holds no prompt, no assistant text, no tool input or output and no file contents. Where a raw value would be text it is replaced by a hash. Dedupe keys are hashes.

## Counting rules

1. **One message, once.** A Claude Code message is written as one line per content block with the same `message.id`. Usage is counted once per `message.id` across all files, taking the line with the largest `output` token count. This also stops copied history after a fork or resume from counting twice.
2. **Attribution.** Tokens belong to the session and agent of the file the winning line came from. A subagent's usage is shown under its agent rows and in the session's total as a separate part (`tokens.subagents`).
3. **Days.** A usage row's day is the day of the message's own timestamp in `time_zone` (default: the machine's zone). A session that crosses midnight splits.
4. **Time.** A session's start and end are the minimum and maximum timestamp seen, never the first and last line (timestamps can step backwards). `active_seconds` is the sum of recorded turn durations; it is null when no turn durations exist.
5. **Tools.** `tool_calls` counts tool-use blocks, `tool_errors` counts results marked as errors, `denials` counts recorded permission denials and `interruptions` counts recorded user interruptions. Tool-use blocks are paired with their results by id; an unpaired block is counted as a call only.
6. **Quality flags.** A session carries `parse_errors` (a count), `unknown_line_types` (a count) and `flags` such as `cost_mismatch`, `no_usage`, `no_timestamps`, `truncated_tail`. The flags are facts about the reading, not about the work.

## Cost

Tokens are exact. A dollar figure always carries a basis.

| `basis` | Meaning |
|---|---|
| `reported` | A cost the transcript's cost lines or claude-session's `cost_usd` gave for that session |
| `estimated` | Token counts multiplied by the price table |
| `unknown` | A model that is not in the price table, or no usable tokens |

- `cost` objects have `reported_usd`, `estimated_usd` and `unknown_tokens`. They are never added into one number. `usage` totals show each basis separately.
- A session whose `reported_usd` and `estimated_usd` differ by more than `cost.mismatch_margin` (default 10 percent) gets the `cost_mismatch` flag; neither figure is changed.
- The price table is the config key `prices` (USD per million tokens per model, four counters). A table is bundled with an `as_of` date and replaced or extended by config. A model matches by exact name first, then by the longest configured prefix.

## Harness enrichment

When claude-session is installed and `--no-harness` is absent, `ingest` calls `claude-session list --json` and, for each listed session whose snapshot is missing or not final, `claude-session status --json`.

- A transcript matches a harness session only when its `claude_session_id` or exact `transcript_path` equals a value in `claude_session_ids[]`. There is no matching on directory names, working directories or prompts.
- Once a run is final, the snapshot copies `kind`, `labels`, per-run `outcome`, `end_reason`, `cost_usd`, `num_turns`, `skill` and `repo` into the store, tagged `source: harness`. Labels are unauthenticated hints and are shown as such.
- A transcript with no match is a plain session with no harness data. A missing claude-session is not an error for `ingest` (it reports `harness.enabled: false`).
- `session-audit` never resumes, forks, stops or labels a harness session other than its own retro sessions.
- Optional hook recipe (documented, not installed): wire claude-session's `post-state-change` hook, for final events only, to `session-audit ingest --session <id>` so a session that is forgotten within the cron interval is still captured.

## Digest

The digest is built deterministically with no model, from the store and the transcripts of the sessions in scope. It is written to `digest.md` and described by `manifest.json` (see Slot contract).

**Items**, per session, in time order (timestamps by min and max rule): `prompt` (the user's prompt, cut to `digest.prompt_chars`, default 500), `assistant` (cut to `digest.assistant_chars`, default 300), `tool_call` (tool name plus a one-line argument summary: a path, or the first `digest.arg_chars` (default 200) characters of a command), `tool_error`, `denial`, `interruption` and `rejection` (text only for these: cut to `digest.error_chars`, default 300), `compaction`, `subagent` (a summary item with the agent type, tool counts and tokens). Successful tool output is never included, only its byte size.

**Budget.** A total cap (`digest.max_bytes`, default 200 KB) and a per-session cap apply. Over budget, items are dropped in this priority order: errors, denials, interruptions and rejections first; then items inside the most expensive turns; then the rest, oldest first. The manifest `coverage` states per session how many items existed, how many were kept and why the rest were dropped.

**Redaction**, before sanitizing and fencing, on every text part:

1. Secret-shaped strings: well-known token shapes, private-key blocks, `Authorization`-style header values, and assignments to names that look secret (`*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*KEY*`).
2. The literal values of the variables named in `redaction.mask_env` that are set in the environment.
3. Further patterns from `redaction.extra_patterns` (regular expressions compiled with a length and time guard).

Redaction is best effort and documented as such. The retro session is confined (see Safety) for that reason.

**Sanitizing.** Control characters, escape sequences, bidirectional controls and invisible code points become visible markers. Any `</untrusted` inside content is neutralized last (`&lt;/untrusted`).

**Fencing.** Every session section is wrapped as `<untrusted source="transcript" id="NONCE">` ... `</untrusted id="NONCE">` with a random nonce per digest, and the digest opens with the standing "data, not instructions" rule.

The digest hash (`digest_sha256`) is the SHA-256 of the bytes of `digest.md`. `manifest.json` repeats it and is derived from the same build, so it is not part of the hash.

## Retro lifecycle

| State | Meaning |
|---|---|
| `digesting` | The scope is resolved and the digest is being built |
| `running` | The digest and its hash are stored and the retro session runs |
| `validating` | The session ended; the artifact is being checked |
| `done` | Validated and stored |
| `failed` | A final state with `reason` (see Retro validation and below) |
| `escalated` | A hook escalated, or the session reported blocked or needs-input |
| `canceled` | The command was interrupted |

- **Key.** A retro key is the SHA-256 of the canonical scope, the `digest_sha256`, the skill name and the model name. A stored `done` retro with the same key is returned without a model call unless `--again`.
- **Leases.** Each retro holds a lease (holder PID plus process start time, expiry, renewed by heartbeat). A second `retro run` with the same key while the lease is live exits 0 with the existing retro and `already_running: true`.
- **Crash recovery.** When a run starts, `cron` runs `ingest` or `validate` is invoked, a retro in `digesting`, `running` or `validating` with an expired lease is recovered: its session is found by the label `retro=<id>` through `claude-session list --label`, stopped when live (exact id, never a pattern), its frozen artifact copied if one exists, the retro marked `failed` with `reason: runner_lost`, and the session forgotten with `--purge`.
- **Signals.** On SIGINT or SIGTERM the command stops its own session (the exact id it started), marks the retro `canceled` and exits 130 or 143.
- **No resume.** An `escalated` or `failed` retro is ended with `retro delete` or replaced with `retro run --again`. There is no resume in 0.x.
- **Daily limit.** The `limits.retros_per_day` count includes `failed`, `escalated` and `canceled` retros started today, excludes `reused` results and resets at local midnight in `time_zone`.
- **`quiet_after`.** A session whose transcript changed within `retro.quiet_after` (default 30 minutes) is skipped and listed under `skipped` with `reason: live`; if nothing remains the command refuses with `live_sessions_only`.

## Retro launch

`retro run` launches exactly one headless session through claude-session (binding A) after the `pre-retro` hook allows it. The tool never launches through agent-spawn in 0.x.

1. A fresh scratch working directory is created under the state directory (`scratch/<retro_id>/`, mode 0700, empty). The retro session runs there. It is the directory the self-exclusion rule in Ingest rules skips.
2. The tool writes `digest.md`, `manifest.json` and a prompt file under its state directory and passes the first two with `--input-file`.
3. The launch is `claude-session exec` with these per-call settings (each replaceable by the config keys in Config, never by another tool's config):
   - `--kind audit --reads-third-party`, which makes the run sandbox `required`, no setting sources, `mcp: none` and no environment inheritance, and taints the session;
   - `--no-implicit-done`;
   - `--skill <skill>`, `--plugin-dir <bundled plugin>`, `--prompt-file`, `--input-file` for each input, `--cwd` the scratch directory;
   - `--permission-profile <bundled profile file>`, `--sandbox required`, `--setting-sources ""`, `--env-allowlist-only` for each name in `retro.env_allowlist`;
   - `--tools Read Write Bash Skill`;
   - `--timeout` (default `retro.timeout`, 20 minutes) and `--max-turns` (default `retro.max_turns`, 12);
   - `--model` only when set;
   - `--label owner=session-audit --label retro=<retro_id>`;
   - `--outcome-file` under the state directory, and `--json`.
4. No report or escalation instructions are added by the tool; claude-session appends its standard instructions.
5. The tool waits (the `exec` call blocks), then reads the outcome object, runs `claude-session status --json` and copies the frozen artifact (the path in `run.report.artifact.path`) into its own state directory.
6. It runs the Retro validation checks. Whatever the result, it then runs `claude-session forget <id> --purge`, deletes the staged prompt, `--input-file` copies and the scratch directory, and, when `retro.remove_run_transcripts` is true (default), removes exactly the transcript files of its own session (found through `status --json` and `transcript --paths`, each verified to be a regular file under the scratch project directory of a root) so the digest does not outlive the retro in Claude Code's own store.
7. The retro's `cost_usd` and `num_turns` come from the outcome object. They are stored on the retro and are not part of the usage store (the transcript is self-excluded).

The prompt file holds only: the retro id, the run token, the digest hash, the skill's output rules by reference and the paths of the two input files. It carries no transcript text.

## Slot contract

The slot is `audit`; the default skill is `session-audit:audit`, bundled in `plugin/`.

**Inputs** under `$CSESSION_DIR/inputs/<run>/`:

- `digest.md`: the fenced digest (Digest).
- `manifest.json`: `{"retro_id", "run_token", "digest_sha256", "sessions": [{"ref", "csession_id" or null, "project", "items": [ids], "metrics": [names]}], "coverage": {...}, "limits": {"max_findings", "max_candidates", "text_limits"}, "finding_categories", "target_classes"}` (`schemas/slot/manifest.json`).

**Output**: one artifact named `audit.json` in `$CSESSION_DIR/artifacts/`, attached with `claude-session report done --summary-file ... --artifact audit.json`. The skill must use literal absolute paths in the report command and must read its inputs with the Read tool (Bash is denied for anything but the report command).

The standard instructions, escalation and reporting come from claude-session; the skill must not restate them.

## Retro artifact

`schemas/slot/artifact.json` is the closed shape the model writes. Exactly these keys:

| Key | Type | Rule |
|---|---|---|
| `retro_id` | string | Must equal the run's retro id |
| `run_token` | string | Must equal the run's token |
| `digest_sha256` | string | Must equal the digest hash |
| `marker` | string | Bundled skill only: must equal the marker read from the plugin's reference file (config `skill_marker` for a custom skill; absent otherwise) |
| `summary` | string | At most 2000 characters; untrusted |
| `findings` | array | At most `limits.max_findings` (50) |
| `candidates` | array | At most `limits.max_candidates` (20) |

A finding has `id` (`f1`, `f2`, ...), `category` (`went_well`, `went_badly`, `friction`, `cost`), `severity` (`low`, `medium`, `high`), `title` (at most 120 characters), `detail` (at most 1500 characters) and `evidence` (1 to 8 items, each `{"kind": "item"|"metric", "ref": "<id>"}`). A candidate has `id` (`c1`, ...), `target_class` (`skill`, `instructions_file`, `config`, `permission_profile`, `workflow`), `target_hint` (at most 200 characters), `proposal` (at most 1500 characters) and `findings` (ids of the findings that support it; each must exist). All model text is untrusted and marked `x-untrusted` in the schema. The whole file is at most 256 KB. No other keys are allowed at any level.

The stored Retro object adds tool-written fields the model cannot set: `scope`, `coverage` (from the manifest), `counts` and `metrics` (from the store), identity, state, `cost_usd`, `num_turns`, `outcome`, timestamps and `again_of`.

## Retro validation

A retro run is a success only when all of these hold; otherwise it is `failed` (exit 22 unless the session itself ended another way, in which case its code is passed through):

1. the outcome is `done`;
2. `run.report.implicit` is false;
3. the report's artifact is named `audit.json`;
4. the file parses as JSON and validates against the closed artifact schema;
5. `retro_id`, `run_token` and `digest_sha256` equal the stored values;
6. for the bundled skill, the marker equals the plugin reference file's marker;
7. every evidence `ref` exists in the manifest (an item id, or a metric name) and every candidate's `findings` ids exist.

A failed check 7 drops only the findings (or candidates) with the bad reference and records `dropped` counts; any other failed check discards the whole artifact. A retro with every finding dropped is `failed` with `reason: no_valid_findings`. A run that finishes with no artifact is `failed` with `reason: skill_not_found` when the run's `skill` shows the model could not find the skill, otherwise `reason: no_artifact`. Failure reasons: `runner_lost`, `no_artifact`, `skill_not_found`, `artifact_invalid`, `identity_mismatch`, `marker_mismatch`, `no_valid_findings`, `session_failed`.

Numbers in the stored retro (`counts`, `metrics`, cost) come from the store and the outcome object. Any number the model writes is not used.

## `--json` outputs

Schemas live in [`schemas/cli/`](../schemas/cli/), the slot schemas in [`schemas/slot/`](../schemas/slot/). Every command that prints data has one.

| Command | Schema | Shape |
|---|---|---|
| `ingest` | `ingest.json` | the ingest report (see `ingest`) |
| `sessions` | `sessions.json` | `{sessions: [Session], truncated}` |
| `usage` | `usage.json` | `{by, rows, totals, truncated}` |
| `show` | `show.json` | `{session, agents, usage, harness}` |
| `retro run` | `retro-run.json` | Retro plus `reused`, `already_running`, `skipped`, `phase`, `outcome`, or the dry-run plan |
| `retro list` | `retro-list.json` | `{retros: [RetroRow], truncated}` |
| `retro show` | `retro-show.json` | Retro with artifact and, with `--digest`, the fenced digest |
| `retro delete` | `retro-delete.json` | `{deleted: [ids]}` |
| `prune` | `prune.json` | counts per table and per retention key |
| `validate` | `validate.json` | `{ok, checks, warnings}` |
| `cron install\|uninstall` | `cron.json` | the block and whether it changed |
| `export` | `export-*.json` | one object per line |
| error | `error.json` | the envelope |

**Session object**: `session_id`, `project`, `entrypoint`, `version`, `started_at`, `ended_at`, `active_seconds`, `models`, `tokens` (`input`, `output`, `cache_read`, `cache_write`, `subagents`), `cost` (`reported_usd`, `estimated_usd`, `unknown_tokens`), `tool_calls`, `tool_errors`, `denials`, `interruptions`, `subagents`, `compactions`, `parse_errors`, `flags`, `harness` (null or `{csession_id, kind, labels, outcome, end_reason, cost_usd, num_turns, skill, repo, source}`).

**Retro object**: `id`, `state`, `reason`, `key`, `scope`, `coverage`, `skipped`, `skill`, `model`, `created_at`, `updated_at`, `finished_at`, `csession_id`, `cost_usd`, `num_turns`, `outcome`, `again_of`, `counts`, `metrics`, `dropped`, `summary`, `findings`, `candidates`. Untrusted fields are `summary`, `findings[].title`, `findings[].detail`, `candidates[].target_hint`, `candidates[].proposal`.

Sessions, retros and `usage` output must be validated by tests against these schemas.

## Hooks

Hooks are configured in the tool's own config (and drop-ins), run via `sh -c` in their own process group, and receive a JSON payload on stdin. Environment: `SESSION_AUDIT_HOOK`, `SESSION_AUDIT_ID`, `SESSION_AUDIT_OBJECT_TYPE`. Hooks inherit the tool's environment. Default timeout 30 seconds. Payloads carry ids, counts and enums, never transcript or model text. Schemas in [`schemas/hooks/`](../schemas/hooks/).

| Hook | When | Payload (besides `tool`, `hook`, `object_type`, `object_id`, `event_id`, `labels`) | Effect |
|---|---|---|---|
| `pre-retro` | After the digest is built and before the session is launched | `scope`, `sessions`, `digest_bytes`, `items`, `skill`, `model`, `key` | exit 0 continue; 13 escalate (retro `escalated`, exit 13); other non-zero veto (exit 11, retro not created) |
| `post-retro` | When a retro reaches a final state | `retro_id`, `state`, `reason`, `findings`, `candidates`, `cost_usd`, `reused` | cannot undo; failure logged and warned |
| `post-attention` | A closed `reason` | `reason` (`retro_failed`, `ingest_failing`), `object_type` (`retro` or `ingest`), `details` (a closed set of ids and enums) | cannot undo |

- `pre-retro` stdout may be a JSON object with `decision_id`; every returned id is recorded in the retro's log entry and state.
- `post-` hooks are best effort and at most once, may run `async: true`, and carry a unique `event_id`. There is no outbox in 0.x.
- `post-attention` fires `retro_failed` when a retro ends `failed` and `ingest_failing` when `ingest.fail_threshold` (default 3) consecutive cron ingests failed, then at most once per day while it keeps failing. An idle or healthy result fires nothing.
- `object_type` is `retro` for the first two hooks and may be `ingest` for `post-attention`. Payloads have no `repo` (a session is not tied to one), so a gate matches them in scope `*`.
- The hooks exclude their own retro runs: a `post-state-change` hook of claude-session wired to this tool must exclude the kind `audit`.

## Config

Location `~/.config/session-audit/config.yaml` (respecting `XDG_CONFIG_HOME`), drop-ins `~/.config/session-audit/config.d/*.yaml` merged in lexical order (maps merge, hook lists append), `--config` overrides. Schema [`schemas/config.json`](../schemas/config.json), example `config.example.yaml`. Precedence: flags, then environment variables for the few keys that have one, then config, then built-in defaults. There is no `repos:` layer.

A retro run refuses (exit 20, `config_unsafe`) when the config file, its directory or a drop-in is a symlink, not owned by the user, or group- or world-writable. `validate` reports the same fault.

| Key | Default | Meaning |
|---|---|---|
| `transcript_roots` | the two default roots | Replaces them |
| `time_zone` | the machine's zone | Day, week and month bucketing and the daily limit |
| `ingest.budget` | `300s` | Time budget per run |
| `ingest.fail_threshold` | `3` | Consecutive failed cron ingests before `ingest_failing` |
| `harness.enabled` | `auto` | `auto` (use claude-session when installed), `on` (a missing claude-session is a failed check), `off` |
| `harness.claude_session_cmd` | `claude-session` | Binding B command name |
| `prices` | bundled, with `as_of` | USD per million tokens per model |
| `cost.mismatch_margin` | `0.10` | Fraction for the `cost_mismatch` flag |
| `retention.sessions` | `400d` | Sessions, agents, usage rows, harness snapshots |
| `retention.retros` | `180d` | Stored retros |
| `retention.digests` | `7d` | Stored digests |
| `retention.log` | `90d` | `log.jsonl` entries |
| `limits.retros_per_day` | `5` | Daily retro limit |
| `retro.skill` | `session-audit:audit` | The slot's skill |
| `retro.skill_marker` | none | Marker for a custom skill |
| `retro.model` | none | Claude Code's default when none |
| `retro.timeout` | `20m` | Session timeout |
| `retro.max_turns` | `12` | Session turn bound |
| `retro.max_sessions` | `20` | Sessions per retro |
| `retro.quiet_after` | `30m` | Minimum age since last change of a session's transcript |
| `retro.permission_profile` | bundled file | Replaces the bundled profile file |
| `retro.sandbox` | `required` | The sandbox level passed per call |
| `retro.setting_sources` | `[]` | The setting sources passed per call |
| `retro.env_allowlist` | `[PATH, HOME, USER, LANG, TERM]` | The names passed with `--env-allowlist-only` |
| `retro.tools` | `[Read, Write, Bash, Skill]` | The `--tools` list |
| `retro.remove_run_transcripts` | `true` | Remove the retro session's own transcript files after collecting |
| `digest.max_bytes` | `200KB` | Total digest cap |
| `digest.session_max_bytes` | `20KB` | Per-session cap |
| `digest.prompt_chars`, `digest.assistant_chars`, `digest.arg_chars`, `digest.error_chars` | 500, 300, 200, 300 | Text cuts |
| `redaction.mask_env` | `[]` | Variable names whose set values are masked |
| `redaction.extra_patterns` | `[]` | Extra regular expressions |
| `schedule.retro` | off | Optional weekly window retro run from cron (`{every: 7d, scope: {...}}`) |
| `hooks` | none | Hook lists (see Hooks) |
| `me.name` | OS user name | Actor name |

Durations accept a number of seconds or the guide's grammar; keys that name days (`retention.*`, `schedule.retro.every`) also accept `d`. Invalid config fails fast with a message naming the key and never the value.

## Environment variables

- `SESSION_AUDIT_TIMEOUT` (`retro.timeout`) and `SESSION_AUDIT_MODEL` (`retro.model`) are the only config environment variables. Profiles, skills, prices and redaction cannot be set from the environment.
- Read but not owned: `CLAUDE_CONFIG_DIR` (a transcript root), `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `PATH`, `NO_COLOR`.
- Hook variables: `SESSION_AUDIT_HOOK`, `SESSION_AUDIT_ID`, `SESSION_AUDIT_OBJECT_TYPE`.
- No credential variable is read. The retro session gets only the names in `retro.env_allowlist` plus what claude-session always passes for Claude's own login.

## Labels

Retro sessions carry `owner=session-audit`, `retro=<retro_id>` and `kind=audit`. The tool filters sessions on harness labels (`--label K=V`) and gives no key a meaning.

## TUI

None in 0.x. awkit can read attention from `retro list --json`. A `browse` TUI is a 1.x item.

## Files

Under `~/.local/state/session-audit/` (respecting `XDG_STATE_HOME`; directories 0700, files 0600):

- `state.db`: private SQLite (WAL, versioned additive-only migrations).
- `log.jsonl`: public, append-only, hash-only (`schemas/log.json`).
- `digests/<retro_id>/`: `digest.md` and `manifest.json` while within `retention.digests`.
- `artifacts/<retro_id>/audit.json`: the validated artifact copy.
- `scratch/<retro_id>/`, `prompts/<retro_id>/`: deleted when the run ends.

There is no `audit.jsonl` (see Deviations).

## Log

`log.jsonl` records each completed `ingest`, `retro` state change, `retro delete` and `prune` with `ts`, `actor`, `action`, counts, ids and hashes. It never holds transcript text, model text or a path value from a transcript. Entries older than `retention.log` are removed by `prune`.

## Retention

`prune` deletes, in one transaction: sessions, agents, usage rows and harness snapshots older than `retention.sessions` (by `ended_at`), retros older than `retention.retros` (with their artifact and digest), digests older than `retention.digests` and log entries older than `retention.log`. It reports counts per key. It never touches transcripts, which belong to Claude Code. `retro delete` removes one retro and its files at once.

## Cron

`cron install` writes one managed crontab block:

- first line `# session-audit managed`, last line `# session-audit managed end`;
- an explicit `PATH` including `~/.local/bin` and the directories of `claude-session` and `claude`, and an optional user-owned env-file prefix (`--env-file`);
- `ingest --quiet` at `--ingest-every` (a duration such as `1h`; default `1h`, minimum `5m`);
- a daily `prune --quiet`;
- a retro line only when `schedule.retro` is set.

`cron install --dry-run --json` prints the block and what would change (`unchanged`, `replaced`, `installed`). A block with no end line is never replaced (exit 20). `cron uninstall` removes only the managed block. awkit's `awkit cron install` runs the tool's own installer; this tool never edits the crontab outside its block.

## Safety

- The deterministic half reads transcripts under the Ingest rules and writes only its own state directory.
- The retro session is confined as in Retro launch. The bundled permission profile must: allow Read of the session's `inputs/` and `artifacts/` and the plugin; allow Write only under `artifacts/`; allow Bash only for the literal `claude-session report` command; deny `Agent`, `Task`, `WebFetch`, `WebSearch`, `mcp__*`, `Edit` outside the generated path-scoped rules, `memoctl`, `agent-spawn`, `forgectl`, `ticketctl`, `notice-route`, `awkit` and `session-audit` itself; and deny writes to `~/.config/session-audit/` and `~/.local/state/session-audit/`. A profile is guidance; the sandbox is the boundary.
- Transcript text reaches the model only through the digest (redacted, sanitized, fenced) and never through a path the session can read.
- The retro artifact is untrusted model text everywhere it is displayed or passed on. A consumer must fence it and must not follow instructions in it.
- The tool never resumes, forks or labels the sessions it audits, and never runs `claude-session forget` on any session but its own retro sessions.
- A retro session is tainted by design (third-party-reading); the tool never resumes it.

## Platform notes

Linux and WSL (Ubuntu) are tested. macOS is expected to work: the roots, `flock` and the sandbox rules are the same, and a macOS checklist is part of the live-verification issue. A retro needs bubblewrap and socat on Linux and WSL2 (claude-session's requirement).

## Residual risks

- Redaction is best effort; a secret in a shape the patterns do not know can reach the digest. The sandbox (no network, no write beyond the artifact) limits what a leaked value can do inside the session; the artifact is then scanned by claude-session for current-environment secret values.
- Findings are model opinions about untrusted text. Evidence references prove only that the cited item exists in the digest, not that the finding is right. Runs of the same digest vary.
- A forged transcript (a same-user process can write into `~/.claude/projects`) can add sessions with chosen counts and chosen text. The store treats every transcript as untrusted data; nothing it holds is an authorization.
- Harness labels are unauthenticated, and a snapshot is a copy at one moment.
- `remove_run_transcripts` deletes files in Claude Code's store; it removes only the exact files of the tool's own retro session after verification.
- Transcript format changes in future Claude Code versions can make counts wrong without failing; `unknown_line_types` and `no_usage` flags are the only signal.

## Deviations from the design guide

- `--mode interactive` is not offered in 0.x (design guide §11 "must offer `--mode`"); it follows in 1.x through agent-spawn.
- The built-in `audit` kind row (trusted, sandbox off, environment inherited, implicit done) is not used by this tool: the retro launch passes stricter per-call settings (design guide §11). The row remains the fallback for a direct `claude-session exec --kind audit` by hand.
- No `audit.jsonl`: the tool takes no external action (as memoctl and ticket-queue). A hash-only `log.jsonl` records completed changes.
- No `repos:` layer and no environment layer except two scalar keys.
- Hook delivery is best effort and at most once, with no outbox.

## Upgrades

The store is versioned and migrations are additive only. A newer `state.db` makes every command exit 20 (`store_newer`) except `validate`. `ingest --rebuild` re-derives rows from transcripts still on disk after a counting-rule change; rows whose transcripts are gone are kept as they were and their `flags` include `old_rules`. The config schema may add keys in a minor release; a removed key is a breaking change named in its release title.
