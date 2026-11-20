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
- **Session**: one top-level transcript, identified by its file name (a lowercase UUID); a line's `sessionId` is never used to join (a line whose `sessionId` differs is still counted in its file's session). A resumed or compacted session is one session. A session id is shown in full and accepted as a unique prefix (see Prefixes under Exit codes). An agent belongs to the session of its directory.
- **Agent**: a subagent transcript linked to its parent session by the path of its directory; its parent tool call is the `toolUseId` of its `agent-<id>.meta.json`. An agent file read before its parent's main file creates the session row from the path.
- **Event**: one counted fact (a message's tokens, a tool call, a tool error, a denial, an interruption, a turn duration, a compaction) with its 15-minute UTC slot, session, agent (or main), model and tool. Everything `usage`, `sessions` and `show` print is aggregated from counted events at query time (see Counting rules).
- **Harness snapshot**: facts copied from claude-session when a transcript matches one of its sessions (see Harness enrichment). Tagged `source: harness`.
- **Retro**: a stored result of `retro run`, identified by `ra-` followed by 8 lowercase base32 characters (`^ra-[a-z2-7]{8}$`).
- **Digest**: the bounded, redacted, fenced text built from the store and transcripts for one retro. Items have ids `s<N>.i<NNN>` (session number within the retro, item number); metrics have ids `s<N>.<metric>` and `total.<metric>` (see Digest, Numbering).
- **Id and label grammars**: what users type is a session id prefix, a retro id prefix and a label. Label keys are `^[a-z0-9][a-z0-9_.-]{0,63}$` and label values 0 to 256 characters from `A-Za-z0-9._:/#@+=,-` (claude-session's grammar). Actors are `human:<name>` or `auto:<name>`. Every other id grammar is exact in [`schemas/defs.json`](../schemas/defs.json).
- **Store**: `state.db` (private SQLite, WAL) holding events, sessions, agents, harness snapshots, retros and ingest bookkeeping. See Files.

## Global flags

Where meaningful on every command: `--json`, `--config PATH`, `--verbose`, `--quiet`, `--dry-run`, `--yes`, `--version`. `--version` prints the version from package metadata and never `0.0.0`; it works with a broken config or a newer store.

Commands that change the store accept `--actor ACTOR` (default `human:<user>` from a terminal, otherwise `auto:unknown`; `<user>` is `me.name`). The actor is written to `log.jsonl`. No command takes an external action, so `--decision-id` is accepted only on `retro run` and records the ids that hook output returned.

Errors go to stderr. With `--json` they are also printed on stdout as `{"error": {"code": "...", "message": "...", "details": {...}}}` (`schemas/cli/error.json`). A refusal (exit 20) carries `details.reason`.

## Exit codes

| Code | Meaning in session-audit |
|---|---|
| 0 | Success. `ingest` also exits 0 for a partial run that hit its budget (`complete: false`) and for "already running" |
| 1 | Unexpected internal error |
| 2 | Usage error or invalid config. Reasons with a name: `retro run --mode interactive` in 0.x (`details.reason: mode_unsupported`), `ingest --rebuild` or `retro delete` without a terminal and without `--yes` (`details.reason: confirmation_required`). Also a bare skill name, a session prefix shorter than 8 characters, a retro prefix shorter than 4 characters after `ra-`, `--limit` outside 1 to 1000, a flag out of range (`details.field` names it) and `--rebuild` combined with `--path`, `--since` or `--session` |
| 10 | Not found: a session, retro or artifact id |
| 11 | A `pre-retro` hook vetoed the run (retro `canceled`, reason `hook_vetoed`; `details.retro_id` names it) |
| 12 | The requested operation failed for a reportable reason. `details.reason` is one of `claude_session_missing`, `claude_session_too_old`, `sandbox_unavailable`, `store_unwritable`, `no_readable_roots` (every transcript root unreadable), `skill_not_found`, `launch_settings_mismatch`, `session_not_started` (a retro session that never started; `details.end_reason` is claude-session's token), `claude_session_error` (any claude-session result outside the Outcome mapping table) or `crontab_unavailable`; `details.retryable` says whether trying again can help. A retro row exists only when the failure happened after step 14 of the Order of checks |
| 13 | Needs a human: a `pre-retro` hook escalated (reason `hook_escalated`), or the retro session reported blocked or needs-input (reasons `session_blocked`, `session_needs_input`); retro state `escalated` |
| 14 to 18 | The retro session's outcome passed through unchanged from claude-session: 14 exited-unreported, 15 killed, 16 timeout, 17 lost, 18 reported failed. `details.outcome` is the outcome token and `details.retro_id` names the retro |
| 20 | Refused, with `details.reason` (see Refusal reasons) |
| 21 | Ambiguous selector (a session or retro prefix) |
| 22 | The retro session finished but its artifact is missing or invalid (retro state `failed`); `details.reason` is one of the validation reasons (`no_artifact`, `artifact_invalid`, `identity_mismatch`, `marker_mismatch`, `no_valid_findings`) and `details.retro_id` names the retro |
| 129, 130, 143 | Interrupted by SIGHUP, SIGINT or SIGTERM after stopping its own work cleanly |

**Prefixes.** Prefixes are case-insensitive (lowercased). Exit 21 `details.matches` lists at most 20 ids (sorted) and `details.matched` gives the total.

**Error envelope** (`schemas/cli/error.json`). `code` is a closed enum mapped to the exit code in the table above: `internal`, `usage`, `config_invalid`, `not_found`, `vetoed`, `failed`, `needs_human`, `session_ended`, `refused`, `ambiguous`, `artifact_invalid`; `schemas/defs.json#/$defs/error` links here. `message` is a fixed string of the tool, never transcript, model or input text. `details` is a closed object that holds only ids, counts and enums, with these keys: `reason`, `retryable` (boolean), `matched` and `limit` (integers), `retro_id`, `id`, `matches`, `field` (the config key, flag or environment variable at fault, never its value), `hook` (`pre-retro`, `post-retro` or `post-attention`), `outcome` (a claude-session outcome token) and `end_reason` (a claude-session token). Required keys: `reason` for `refused` (a Refusal reason), `failed` (the exit 12 reasons) and `artifact_invalid` (the validation reasons); `retryable` for `failed`; `matches` for `ambiguous`; `id` for `not_found`.

With `--json`, a non-zero exit prints only the envelope. This includes exits 11, 13, 14 to 18 and 22 of `retro run`, where a retro exists and `details.retro_id` names it (the retro itself is read with `retro show`). Exits 129, 130 and 143 print no envelope.

## Refusal reasons

| `reason` | When |
|---|---|
| `scope_too_large` | An explicit `--session` list names more sessions than `retro.max_sessions` (the count is in `details.matched`); a filter scope is never refused for size (see `retro run`) |
| `daily_limit` | `limits.retros_per_day` retros already started today |
| `config_unsafe` | The config, its directory or a drop-in is unsafe (see Config), or a scheduled retro runs with a lowered sandbox |
| `live_sessions_only` | Every matched session changed within `retro.quiet_after` |
| `empty_scope` | The scope matches no stored session, or every skip was not `live` and nothing is left |
| `store_newer` | `state.db` was written by a newer version |
| `already_running` | `retro run` found a non-final retro with the same key and a live lease; `details.retro_id` names it |
| `in_use` | `retro delete` on a retro whose run is still live |
| `cron_block_malformed` | `cron install` or `cron uninstall` found a managed block that is not exactly one start line followed later by exactly one end line |

`confirmation_required` is not a refusal: it is a usage error (exit 2).

## Session filters

`sessions`, `usage`, `retro run` and `export` share these filters; each command links here.

- All flags AND together. Repeated `--session` ids are a union among themselves; repeated `--label` pairs AND (a pair matches any value of its key in the harness labels).
- `--since` and `--until` select sessions whose [`started_at`, `ended_at`] overlaps the window (a session with null timestamps never matches a time filter); in `usage` they select events by their slot time instead.
- `--project P` matches the stored project (the first non-empty `cwd`, see Session object) by path boundary with bound parameters: `project = P or substr(project, 1, len(P) + 1) = P || '/'`, after stripping one trailing `/` from `P`. There is no wildcard and no symlink resolution.
- `--kind` and `--outcome` match only sessions with a harness snapshot. `--model` is an exact model name among the session's models. `--entrypoint` filters on `interactive` or `headless`.
- Labels are unauthenticated hints: a label scope is not a confidentiality boundary (see Safety).

## Time arguments

Linked from `ingest`, `sessions`, `usage`, `retro run`, `retro list` and `export`.

- `WHEN` is an ISO 8601 time with `Z` or an offset, a date `YYYY-MM-DD` (midnight in `time_zone`), or a duration ago `<n>(s|m|h|d)`. Bare digits are refused (exit 2).
- `DURATION` flags (`--budget`, `--timeout`): `<n>(ms|s|m)` or bare seconds, as in Config.
- `cron install --ingest-every`: `<n>m` with n in {5, 6, 10, 12, 15, 20, 30}, `<n>h` with n in {1, 2, 3, 4, 6, 8, 12, 24}, `60m` as `1h`, or a bare integer meaning minutes from the same set (awkit passes integers as text). Anything else exits 2 naming `--ingest-every`. Default `1h`. This is a recorded deviation from design guide §5 (an interval flag, not a duration key).
- Config durations: as in Config; `retention.*` and `schedule.retro.scope.since|until` also take `d`.
- Timestamps in every output are UTC with a `Z` suffix.

## Commands

### `ingest`

`session-audit ingest [--since WHEN] [--path PATH]... [--session CSESSION_ID] [--no-harness] [--rebuild] [--budget DURATION] [--dry-run] [--json]`

Reads new transcript bytes into the store and, when claude-session is installed, captures harness snapshots. Idempotent. Never recovers retros. The ingest lease is a design guide §4.3 lease on the store (holder PID, start time, expiry 120 s, renewed every 30 s); a dead holder's lease is taken over at once, so a killed ingest never wedges later runs. A second caller exits 0 with `already_running: true`.

- `--since`: a session's files are all considered when any of them (main or subagent) was modified after the time (see Time arguments). Default: every file under the roots.
- `--path`: restrict to files under this root (repeatable; resolved by components below a configured root; a path outside every root exits 2 naming `--path`).
- `--session CSESSION_ID`: takes a claude-session id (other commands take Claude Code session ids). Captures that session's harness snapshot and ingests its transcript files only, found through `claude-session status --json` (`claude_session_ids[]`). When the lease is held, the id is inserted into `pending_captures` (one short transaction) and the command exits 0 with `queued: true`; the holder drains `pending_captures` before it releases the lease, and every ingest drains it at start. An id claude-session reports unknown (exit 10) exits 10. This is the target of the hook recipe in Harness enrichment.
- `--no-harness`: skip claude-session entirely for this run.
- `--rebuild`: re-read every transcript still on disk from the start and rebuild its events (see Upgrades). Rows of transcripts no longer on disk are kept. Requires `--yes` when stdin is not a terminal. With `--path`, `--since` or `--session` it exits 2; with `--dry-run` it prints how many files would be reread (`files.rewritten`); under a held lease it exits 0 `already_running` with a warning naming `--rebuild`.
- `--budget`: overall time budget for the run (default `ingest.budget`, 300 s). A run that hits it stops cleanly, exits 0 with `complete: false`, and the next run resumes.
- `--dry-run`: reads and parses but writes nothing and never calls claude-session; prints what would change.

`--json` output: `schemas/cli/ingest.json` (the key list is in the schema). Ingest counts files (`seen`, `new`, `changed`, `unchanged`, `unsafe`, `excluded`, `vanished`, `rewritten`, `remaining`), lines (`parsed`, `errors`, `oversized`, `unknown`), sessions, harness and `warnings` (free strings, untrusted because they can name transcript paths). An `already_running` result has every count zero and `complete: false`; `queued` is true only for a queued `--session` capture.

### `sessions`

`session-audit sessions [--since WHEN] [--until WHEN] [--project PATH] [--label K=V]... [--kind K] [--outcome O] [--entrypoint interactive|headless] [--model M] [--sort start|tokens|cost|active] [--limit N] [--json]`

Lists stored sessions, newest first by default, filtered as in Session filters. Reads the store only. `--limit` default 100, 1 to 1000 (else exit 2). Output: `{"sessions": [...], "truncated": bool, "store": {...}}` (`schemas/cli/sessions.json`). Each row has the fields of the Session object. `store` is `{last_ingest_at, last_complete_ingest_at}` (timestamps or null), so a reader knows how fresh the numbers are.

### `usage`

`session-audit usage --by DIM[,DIM] [filters as in Session filters] [--tz ZONE] [--limit N] [--json]`

Deterministic analytics over counted events (see Counting rules). Dimensions: `day`, `week`, `month`, `project`, `model`, `kind`, `entrypoint`, `tool`, `session`, `label=<key>`. At most two dimensions. `--tz` overrides `time_zone` for day, week and month bucketing. Output: `{"by": [...], "rows": [...], "totals": {...}, "truncated": bool, "store": {...}}` (`schemas/cli/usage.json`). `key` is an array with one entry per dimension in `by` (null when a value is missing), in the same order, never a joined string. `totals` has the row fields without `key`.

- Rows aggregate counted events in the filter set; `totals` are computed once over the same filter set (distinct sessions), never as the sum of rows.
- Per row: `sessions` is the distinct count of sessions contributing; `tokens` the four counters of message events; `tool_calls`, `tool_errors`, `denials` and `interruptions` counts of those events; `active_seconds` the sum of turn events (null when the row has none); `cost.estimated_usd` from each message event's tokens and the price table; `cost.unknown_tokens` tokens of models with no price; `cost.reported_usd` only when no dimension splits a session (dimensions `project`, `kind`, `entrypoint`, `session`, `label=<key>`), else null; `flags` the union of the contributing sessions' flags.
- Dimension `tool`: rows carry `tool_calls`, `tool_errors`, `denials` and `sessions`; `tokens`, `cost`, `active_seconds` and `interruptions` are null (a tool call owns no tokens). It combines with any other dimension.
- Dimension `label=<key>`: a session with several values of the key contributes to each value's row; rows may overlap; `totals` do not.
- Order: by `key` ascending when the first dimension is `day`, `week` or `month`; otherwise by `tokens.input + output + cache_read + cache_write` descending (null last), then `key` ascending. `--limit` default 100, 1 to 1000 (else exit 2); `truncated` is true when more rows matched. `--sort` does not apply.
- `--since` and `--until` select events by their slot time, not whole sessions.
- A row never mixes cost bases silently.

### `show`

`session-audit show SESSION_ID [--json]`

One stored session: the Session object, its agents and its usage rows. An agent is `{agent_id, parent_tool_call_id, agent_type, models, tokens, tool_calls, tool_errors, denials, interruptions}`. A usage row is counted events aggregated per (day in `time_zone`, session, agent, model): `{day, session_id, agent_id (null for the main agent), model, tokens, tool_calls, tool_errors, denials, interruptions, active_seconds}`, and is also the line shape of `export usage`. Exit 10 when unknown, 21 when the prefix is ambiguous. `--json`: `schemas/cli/show.json`, `{session, agents, usage, store}`; the harness snapshot is inside `session`.

### `retro run`

`session-audit retro run SCOPE [--skill NAME] [--mode headless] [--model M] [--timeout DURATION] [--max-turns N] [--again] [--decision-id ID]... [--dry-run] [--actor ACTOR] [--json]`

Runs one retro. Blocks until the retro is final. `SCOPE` is one or more of `--session ID` (repeatable), `--label K=V` (repeatable), `--project PATH`, `--since WHEN`, `--until WHEN`, `--kind K`, `--outcome O`, filtered as in Session filters. At least one scope flag is required (exit 2 otherwise). The flow is the Order of checks under Retro lifecycle, Retro launch and Retro validation. Before launch it prints to stderr (unless `--quiet`) the kept and skipped session counts and the number of distinct projects.

- `--skill`: a plugin skill `plugin:skill` from the bundled plugin or from `retro.plugin_dirs`; default `retro.skill` (`session-audit:audit`). Bare names exit 2 (see Deviations).
- `--mode`: only `headless` is accepted in 0.x. `interactive` exits 2 with `details.reason: mode_unsupported`.
- `--again`: run the model even when a stored retro has the same key; the new retro records `again_of`.
- `--max-turns` is bounded like `retro.max_turns` (1 to 1000) and `--timeout` like `retro.timeout` (1 s to 2 h); out of range is exit 2 naming the flag.
- `--dry-run`: applies steps 1 to 4 and 6 to 13 of the Order of checks; it never recovers, writes, runs hooks or launches; `daily_remaining` is `limits.retros_per_day` minus today's starts (it is not a refusal in a dry run). It prints the plan (matched sessions, skipped sessions with reasons, item counts, size, the key, the claude-session command's flags without the prompt) and exits 0. It never prints digest text. The plan is `{dry_run: true, scope, matched, skipped, items, items_by_kind, digest_bytes, key, skill, model, launch_flags, daily_remaining}`: `matched` lists the full ids of the sessions that would be digested, `skipped` has the `{session_id, reason}` rows, `items_by_kind` counts items per item kind, and `launch_flags` is the list of claude-session flags without the prompt. The dry-run key equals the key a real run computes over the same transcript bytes (see Freshness).
- A filter scope (no `--session`) keeps the `retro.max_sessions` sessions with the newest `ended_at` and lists the rest under `skipped` with reason `over_cap`; the dry-run plan shows them. Only an explicit `--session` list over the cap is refused (`scope_too_large`).

Exit codes: see Exit codes. When a stored `done` retro with the same key exists and `--again` is absent, the command exits 0 and prints that retro with `reused: true` (no hook, no limit, no row). A second run whose key matches a non-final retro with a live lease exits 20 `already_running`.

`--json` output: `schemas/cli/retro-run.json`: the Retro object plus `reused`, or the dry-run plan. A non-zero exit prints the error envelope instead (see Exit codes).

### `retro list`

`session-audit retro list [--state S]... [--since WHEN] [--key K] [--skill S] [--session ID] [--limit N] [--json]`

Lists retros, newest first. Reads the store only; never checks liveness. Output `{"retros": [...], "truncated": bool}` (`schemas/cli/retro-list.json`). Each row has a stable string `id`, `state`, `reason` (null unless the retro is `failed`, `escalated` or `canceled`), `created_at`, `updated_at`, `finished_at`, the `scope`, `findings` and `candidates` counts, `key` and `superseded` (true when a later retro with the same key exists). Rows carry no model text (no title). `--limit` default 100, 1 to 1000.

### `retro show`

`session-audit retro show RETRO_ID [--digest] [--json]`

Prints one retro. Default is readable text, with the mapping from session refs to sessions; `--json` prints the Retro object including the artifact (`schemas/cli/retro-show.json`). This is the contract other tools read. For a non-final retro the artifact fields are null or empty and `state` says so. `--digest` also prints the stored digest if it has not been pruned (with `--json` the Retro object gains a `digest` string, null when pruned; without `--digest` the key is absent); the digest is untrusted text, sanitized and printed inside its fence. Exit 10 when unknown.

### `retro delete`

`session-audit retro delete RETRO_ID... [--yes] [--json]`

Deletes retros and their digests. Confirms on a terminal; without a terminal it needs `--yes` (exit 2 `confirmation_required`). Exit 20 `in_use` for a retro whose run is live. It is all or nothing: an unknown id exits 10 before anything is deleted, so `deleted` lists exactly the ids requested. It never removes `retro_starts` or `retro_sessions` rows.

### `prune`

`session-audit prune [--dry-run] [--json]`

Applies the retention rules (see Retention) and runs the housekeeping step of Retro lifecycle (recovery, cleanup retries). Retention is configured policy, so it does not ask for confirmation; automation may run it. `prune --dry-run` lists planned recoveries and touches nothing. Output `schemas/cli/prune.json`: `{dry_run, tables, recoveries}`. `tables` counts deleted rows per table (`sessions`, `agents`, `events`, `harness_snapshots`, `files`, `retros`, `digests`, `log_entries`, `retro_starts`); `recoveries` lists the retro ids recovered (or, with `--dry-run`, to be recovered).

### `export`

`session-audit export {sessions|usage|retros} [--since WHEN] [--json]`

Prints store rows as JSON lines (one object per line, oldest first, no limit; the output streams). Output holds the same fields as the matching read commands and no text beyond a retro's artifact. `export sessions` lines are the Session object, `export usage` lines the usage row of `show`, and `export retros` lines the full Retro object with its artifact text (`schemas/cli/export-*.json`). The output is always JSON lines; `--json` is accepted and changes nothing.

### `validate`

`session-audit validate [--probe] [--json]`

Offline and read-only by default; never prompts and never recovers or changes anything. Checks the config and its ownership, the transcript roots, the store (opened read-only; a pending migration is reported, never applied), the price table age (warns when `as_of` is older than 180 days), the permission profile file, the bundled plugin directory and whether claude-session is installed and at least MIN_CS (see Retro launch). Output `{"ok": bool, "checks": [{"name", "ok", "reason"}], "warnings": [...]}` (`schemas/cli/validate.json`). It prints its document on exits 0, 2 and 12, also with a config that does not parse (the `config` check fails and the checks that need config are reported `ok: false` with reason `config_invalid`). Check names: `config`, `config_ownership`, `transcript_roots`, `store`, `price_table`, `permission_profile`, `plugin`, `claude_session`, and under `--probe` also `probe_kinds` and `probe_sandbox`. `reason` is null when the check is ok and otherwise one of `config_invalid`, `config_unsafe`, `no_readable_roots`, `store_unreadable`, `store_newer`, `permission_profile_missing`, `plugin_missing`, `claude_session_missing`, `claude_session_too_old`, `probe_failed`, `sandbox_unavailable`; a check has no message field. With `harness.enabled: on` and no claude-session the `claude_session` check fails; with `auto` it passes. `warnings` is a closed enum: `price_table_old`, `migration_pending`, `root_unusable`, `ingest_stale` (`last_complete_ingest_at` is null or older than 24 hours while a managed cron block exists), `retros_unrecovered` (an expired non-final retro exists), `cleanup_incomplete`, `sandbox_lowered`, `env_file_unsafe`, `no_cron_block` (no managed block exists: digests are then pruned only by `retro run`), `claude_session_missing` (only when `schedule.retro` is set and `harness.enabled` is `auto`). Exit 0 for ok (including warnings), 2 for an invalid config, 12 for a failed check. `--probe` additionally runs `claude-session kinds --json` and the sandbox availability check.

### `cron`

`session-audit cron install [--ingest-every INTERVAL] [--env-file PATH] [--dry-run] [--json]` and `session-audit cron uninstall [--dry-run] [--json]`. See Cron and Time arguments. Output `schemas/cli/cron.json`: `{command, outcome, dry_run, block}`. `outcome` is `installed`, `replaced` or `unchanged` for install and `removed` or `unchanged` for uninstall; `block` is the managed block's lines (empty when there is none; for `uninstall --dry-run` the lines that would be removed).

## Ingest rules

These rules are normative for the reader.

**1. Roots.** The transcript roots are `~/.claude/projects` and, when set, `$CLAUDE_CONFIG_DIR/projects`. The config key `transcript_roots` replaces both. Nothing outside the roots is read.

**2. Discovery.** Only these paths are transcripts (copy):

```text
main:      <root>/<project>/<uuid>.jsonl                                  session id = <uuid> (lowercase UUID)
subagent:  <root>/<project>/<uuid>/subagents/agent-<id>.jsonl
           <root>/<project>/<uuid>/subagents/workflows/<wf>/agent-<id>.jsonl   (no deeper)
meta:      the agent file's sibling agent-<id>.meta.json, read with a 64 KiB cap (agentType, toolUseId; empty or bad -> null)
never read: tool-results/, memory/, journal.jsonl, any other name or depth
scan caps: at most 100,000 directory entries scanned per root per run (warning scan_capped when hit)
```

**3. Opening and reading.** The mechanics are (copy):

```text
root: resolved once (realpath allowed for the root itself), must be owned by the user and not group or other writable
below the root: every directory opened with openat(O_RDONLY|O_DIRECTORY|O_NOFOLLOW); file opened with
    openat(O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC); fstat on the descriptor: S_ISREG, st_nlink == 1, st_uid == me;
    then clear O_NONBLOCK; a FIFO or special file is files.unsafe and never blocks
--path: resolved by components below a configured root after opening (never a string prefix check); outside -> exit 2
line read: readline(8 MiB + 1); a longer record is skipped to the next LF and counted in lines.oversized
partial tail: left for the next run; a tail longer than 8 MiB sets files.skip_to_lf and the offset moves to EOF
budgets: ingest.file_budget (default 60 s) per file and --budget per run; checked between lines
```

A root writable by another user makes that root unusable (reported, other roots continue). Any other violation skips the file and counts it as `files.unsafe`.

**4. Lines.** Only lines ending in a line feed are parsed. A line that is not a JSON object is counted in `lines.errors` and skipped. NUL bytes inside a line make it an error line. Unknown line types count in `lines.unknown`.

**5. Values.** Every line is parsed, validated and stored inside one guard (copy):

```text
every line is parsed, validated and stored inside one guard: any exception (JSONDecodeError, RecursionError,
    ValueError, OverflowError, UnicodeError) makes it an error line (lines.errors); never fails the file or the run
token counters, durationMs: int (not bool), 0 <= n <= 2**40; else the line is an error line
timestamps: ISO 8601; outside [2020-01-01, file mtime + 1 day] -> treated as missing
strings: fix_surrogates, then model, tool, agent_type and version capped at 128 characters, project at 4096
tools: at most 200 distinct tool names per session; further names count under the tool name "other"
sums: computed in Python integers; a stored sum is clamped to 2**62
```

`fix_surrogates` replaces each lone surrogate with U+FFFD at every point a string enters the store, a digest, a manifest, an artifact or output.

**6. Progress.** For each file the store keeps what change detection needs (copy):

```text
stored per file: dev, inode, size, offset, head_sha (first line, or first 4 KiB when no LF), tail_sha (the 4 KiB ending at offset)
reread from the start when: (dev, inode) changed, or size < offset, or head_sha differs, or tail_sha of the bytes before offset differs
otherwise read from offset
```

A reread rebuilds the file's events in one transaction (see Counting rules); rows are written in one transaction per file read.

**7. Self-exclusion.** The tool never matches its own retro transcripts by a directory it owns. It records every retro's `csession_id` before launch in `retro_sessions(csession_id, retro_id, created_at)`, kept for `retention.sessions` even after `retro delete` or `prune` of the retro. A transcript file is skipped and counted in `files.excluded` when its project directory name ends with `-<csid>-work` for a `csid` in `retro_sessions` (claude-session's session directory `…/sessions/<csid>/work` encoded by Claude Code), or when its session id is one of the Claude session ids collected for a retro. Other claude-session sessions (`review`, `gate-check`, `rank` use the same `…-work` shape) are not excluded. A file ingested before its exclusion became known cannot happen for retros (the id is stored before launch).

```text
def excluded(project_dir_name, session_id):
    m = re.fullmatch(r".*-(cs-[a-z0-9]{8})-work", project_dir_name)
    return (m is not None and m.group(1) in retro_csession_ids) or session_id in retro_claude_ids
```

**8. Unknown shapes.** Unknown line types, unknown keys and missing keys are never fatal. A session that lacks the fields a rule needs gets a quality flag (`no_usage`, `no_timestamps`) instead; unknown line types are counted in the Session object's `unknown_line_types`.

**9. Store content.** The store holds counts, ids, timestamps, model names, tool names, project paths and harness facts. It holds no prompt, no assistant text, no tool input or output and no file contents. Where a raw value would be text it is replaced by a hash. Dedupe keys are hashes.

### Transcript field map

From spike A against Claude Code 2.1.284 to 2.1.328; every key is optional.

| Fact | Line | Key path |
|---|---|---|
| message tokens | `type: assistant` | `message.id`, `message.model`, `message.usage.{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, cache_creation.ephemeral_5m_input_tokens, cache_creation.ephemeral_1h_input_tokens}` |
| tool call | `type: assistant` | `message.content[]` with `type: tool_use`: `id`, `name`, `input` (summary only, see Digest) |
| tool error | `type: user` | `message.content[]` with `type: tool_result`, `is_error: true`: `tool_use_id` |
| denial | `type: user` | `toolDenialKind` present, or `permissionDecision.decision: reject` |
| interruption | `type: user` | `interruptedMessageId` present |
| rejection (digest item) | `type: user` | `toolDenialKind: user-rejected` |
| prompt (digest item) | `type: user` | text content, no `tool_result` block, not `isMeta`, not `isCompactSummary` |
| turn duration | `type: system`, `subtype: turn_duration` | `durationMs` |
| compaction | `type: system`, `subtype: compact_boundary` | `uuid` |
| reported cost | `type: cost-state` (main file) | `totalCostUSD` of the last such line |
| identity | every line | `uuid`, `timestamp`, `cwd`, `entrypoint`, `version` |
| agent link | `agent-<id>.meta.json` | `agentType`, `toolUseId` |

## Counting rules

**1. One fact, once.** Every counter is derived from events with a dedupe key (copy the rule):

```text
# dedupe keys (hashed, SHA-256 truncated to 16 bytes):
#   message: message.id         tool_call: tool_use.id        tool_error: tool_result.tool_use_id (is_error true)
#   denial, interruption, turn, compaction: the line's uuid (a line with no uuid: hash of (file id, line offset))
# within one file: message keeps the line with the largest output_tokens (tie: the earliest line); others the first line
def read_file(F, full_reread):
    with transaction():
        touched = set()
        if full_reread:
            touched |= {k for (k,) in q("SELECT key FROM events WHERE file_id=?", F)}
            q("DELETE FROM events WHERE file_id=?", F)
        for ev in parse_new_lines(F):
            upsert_within_file(ev)                          # UNIQUE(key, file_id); merge rule above; ts = min
            touched.add(ev.key)
        for k in touched:
            owner = q1("SELECT rowid FROM events WHERE key=? ORDER BY ts IS NULL, ts, file_path, line_offset LIMIT 1", k)
            q("UPDATE events SET counted = (rowid = ?) WHERE key=?", owner, k)
        update_offsets(F); refresh_session_totals(sessions_of(touched))
```

The owner of a key is the copy with the earliest timestamp, then the smaller file path, then the smaller offset; only the owner is counted. The rule does not depend on ingest order, so a rebuild, a rewrite of either file, or reading a fork before its source give the same counts. When an owner's session is pruned its copies are not re-evaluated until a file holding them is read again (residual risk). This also stops copied history after a fork or resume from counting twice.

**2. Attribution and tokens.** Events belong to the session and agent of the file whose copy owns the key. A session's `tokens` four counters are the main agent's only; `tokens.subagents` is the sum of all four counters over its agents; the Session `agents` count is the number of agent files. `usage` rows by `session` include agents. A session's `cost.estimated_usd` includes its agents; `cost.reported_usd` is the last `cost-state` line's `totalCostUSD` of the main file (cumulative, includes subagents) or the harness `cost_usd`, which `cost_mismatch` compares with the estimate including agents.

**3. Time.** Every event has a `slot`: its own timestamp in UTC floored to 15 minutes. An event with no usable timestamp takes the nearest preceding usable timestamp in the same file, else the file's mtime at read time, and its session gets `no_timestamps`. `usage` buckets slots into `day`, `week` (ISO 8601, Monday start, key `2026-W47`) and `month` (key `2026-11`; day key `2026-11-20`) in `--tz` or `time_zone`, so any zone and a changed `time_zone` re-bucket exactly (every real zone offset is a multiple of 15 minutes).

**4. Session time.** `started_at` and `ended_at` are the minimum and maximum timestamp of the session's counted events, falling back to all its lines when it has none (a fork then starts at its first own event); never the first and last line (timestamps can step backwards). `active_seconds` is the sum of recorded turn durations; it is null when no turn durations exist. `ended_at` is clamped at ingest to at most file mtime plus one day.

**5. Tools.** `tool_calls` counts tool-use events, `tool_errors` counts results marked as errors, `denials` counts recorded permission denials and `interruptions` counts recorded user interruptions. Tool-use blocks are paired with their results by id; an unpaired block is counted as a call only.

**6. Models.** An event with no model gets model `unknown`; message lines with model `<synthetic>` and zero usage are ignored.

**7. Quality flags.** A session carries `parse_errors` (a count), `unknown_line_types` (a count) and `flags` from the closed set `cost_mismatch`, `no_usage`, `no_timestamps`, `truncated_tail`, `old_rules`, `project_from_dir`. The flags are facts about the reading, not about the work. A session with no usage has `estimated_usd` 0 and the `no_usage` flag.

## Cost

Tokens are exact. A dollar figure always carries a basis.

| `basis` | Meaning |
|---|---|
| `reported` | A cost the transcript's cost lines or claude-session's `cost_usd` gave for that session |
| `estimated` | Token counts multiplied by the price table |
| `unknown` | A model that is not in the price table, or no usable tokens |

- `cost` objects have `reported_usd` (null means no reported cost, never 0 for missing), `estimated_usd` and `unknown_tokens`. They are never added into one number. `usage` totals show each basis separately.
- A session whose `reported_usd` and `estimated_usd` differ by more than `cost.mismatch_margin` (default 10 percent) gets the `cost_mismatch` flag; neither figure is changed.
- Message events store `cache_write_5m` and `cache_write_1h` (from `cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`; when absent, all of `cache_creation_input_tokens` counts as 5-minute); `tokens.cache_write` is their sum.
- The price table is the config key `prices` (USD per million tokens per model: `input`, `output`, `cache_read`, `cache_write` and optional `cache_write_1h`, default `cache_write`). A table is bundled with an `as_of` date and replaced or extended by config. A model matches by exact name first, then by the longest configured prefix.

## Harness enrichment

When claude-session is installed, `harness.enabled` is not `off` and `--no-harness` and `--dry-run` are absent, `ingest` captures harness facts (copy):

```text
capture (ingest, not --dry-run, not --no-harness):
  L = cs("list", "--json", "--since", meta.last_list_at - 1h if set)    # stdout capped at 16 MiB, else harness.errors
  for s in L where s.updated_at > harness_seen[s.csession_id] (or unseen), at most 500 per run:
      st = cs("status", s.csession_id, "--json")                         # 1 MiB cap
      harness_seen[s.csession_id] = s.updated_at
      ids = {c.claude_session_id for c in st.claude_session_ids}
      for each stored session whose session_id in ids: write snapshot (replacing an older one)
  meta.last_list_at = time of the list call
snapshot = {csession_id, kind: st.launch_kind, labels, repo, run: st.run.run, outcome, end_reason, cost_usd,
            num_turns, skill, updated_at, final: st.run.outcome is not null}, tagged source harness
```

- A transcript matches a harness session when its session id (the file name) is one of `status --json` `claude_session_ids[].claude_session_id` (see claude-session's `schemas/cli/status.json`). There is no path matching and no matching on directory names, working directories or prompts.
- A non-final run's snapshot is refreshed when `updated_at` changes; a resumed session's later final run replaces the snapshot. `status` and `list` may finalize claude-session runs through their liveness check; the tool calls them only in a real ingest (never in `--dry-run`).
- Labels are unauthenticated hints and are shown as such. A transcript with no match is a plain session with no harness data. A missing claude-session is not an error for `ingest` unless `harness.enabled` is `on` (exit 12 `claude_session_missing`); with `auto` it reports `harness.enabled: false`.
- `session-audit` never resumes, forks, stops or labels a harness session other than its own retro sessions.
- Optional hook recipe (documented, not installed): wire claude-session's `post-state-change` hook, for final events only, to `session-audit ingest --session <csession id>` so a session that is forgotten within the cron interval is still captured.

## Digest

The digest is built deterministically with no model, from the store and the transcripts of the sessions in scope. It is written to `digest.md` and described by `manifest.json` (see Slot contract). No untrusted string appears in `manifest.json` or the prompt file. In `digest.md` every untrusted string, the per-session header (project, models, tools, times) included, is inside the session's fence.

**Numbering.** Kept sessions are ordered by `started_at` (null last), then session id, and numbered `s1`, `s2`, … up to `s500` (`retro.max_sessions` maximum). Items are numbered per session in time order before any drop, `i` plus at least three digits (`i001`), so an id never changes with the budget.

**Metrics.** The metric names are `tool_calls`, `tool_errors`, `denials`, `interruptions`, `compactions`, `agents`, `active_seconds`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `estimated_usd`; `total.<name>` is the sum over kept sessions.

**Items**, per session, in time order: `prompt` (the user's prompt, cut to `digest.prompt_chars`, default 500), `assistant` (cut to `digest.assistant_chars`, default 300), `tool_call` (tool name plus a one-line argument summary, below), `tool_error`, `denial`, `interruption` and `rejection` (text only for these: cut to `digest.error_chars`, default 300), `compaction`, `subagent` (a summary item with the agent type, tool counts and tokens). Successful tool output is never included, only its byte size. The manifest `items` lists only kept items.

| Tool | Summary |
|---|---|
| `Bash` | the command, cut to `digest.arg_chars` |
| `Read`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | `file_path` |
| `Glob`, `Grep` | `pattern`, cut to `digest.arg_chars` |
| `WebFetch` | the URL's host only |
| `Agent`, `Task` | `subagent_type` |
| `Skill` | the skill name |
| any other (including `mcp__*`, `WebSearch`) | the tool name only |

**Budget.** A total cap (`digest.max_bytes`, default 200 KB) and a per-session cap (`digest.session_max_bytes`) apply. Per-session header bytes count toward both caps; a session whose header alone exceeds the per-session cap keeps only its header. Items are kept in this priority order (copy):

```text
priority(item) = 0 if kind in {tool_error, denial, interruption, rejection}
               = 1 if item lies in one of the session's 3 most expensive turns
               = 2 otherwise
turn = the items from one prompt item up to the next; turn cost = sum over counted message events in it of
       input + output + cache_read + cache_write (from the store)
per session: sort items by (priority, newest first); keep while session bytes + rendered size <= digest.session_max_bytes
total: sort every kept item of every session by (priority, newest first, ref); keep while total + size <= digest.max_bytes
render each session's kept items in time order; coverage counts the drops per cap
```

The manifest `coverage` is `{digest_bytes, items_total, items_kept, sessions: [{ref, items_total, items_kept, dropped: {session_cap, total_cap}}]}`: per session how many items existed, how many were kept and how many were dropped by the per-session cap and by the total cap. The stored Retro object repeats it.

**Text pipeline.** Every text part goes through this fixed order (copy):

```text
def digest_text(raw, cap):
    t = fix_surrogates(raw)                 # lone surrogates -> U+FFFD
    t = t[:65536]                           # pre-cap in code points before any regex
    t = unicodedata.normalize("NFC", t)
    t = redact(t)                           # bounded built-in patterns, literal masks, token prefixes
    t = cut(t, cap)                         # at a code point, never inside a [REDACTED:<kind>] marker
    t = sanitize(t)                         # see Sanitizing
    t = re.sub(r"(?i)</untrusted", "&lt;/untrusted", t)
    return t
```

**Redaction**, on every text part:

1. Secret-shaped strings: well-known token shapes, private-key blocks, URL userinfo (`scheme://user:secret@host`), `Cookie:` and `Set-Cookie:` values, `Authorization`-style headers, `--token X`, `--password X`, `-u user:pass`, JSON or YAML `"password": "x"` style keys, JWTs (three base64url parts starting `eyJ`), and assignments to names containing `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `PASSPHRASE`, `KEY`, `CREDENTIAL`, `AUTH`.
2. Literal values of the environment of the `retro run` process: every variable whose name matches the secret-name shapes above and every name in `redaction.mask_env`; values shorter than 8 characters are ignored (a warning for a `mask_env` name). A scheduled retro sees cron's environment (plus the env-file), so masks are best effort there.
3. Strings starting with a prefix in `redaction.extra_token_prefixes`: `<prefix>[A-Za-z0-9_-]{8,256}` is redacted.

Built-in patterns are bounded (no unbounded repetition across a line; a private-key block is `-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----` plus at most 8192 following characters up to the matching END line, and a BEGIN with no END within 8192 characters redacts the header and those 8192 characters); they are tested against adversarial inputs (linear time at 64 KiB). Redaction is best effort and documented as such. The retro session is confined (see Safety) for that reason.

**Sanitizing.** The sanitizer is awkit's (awkit interface, Output sanitizing): categories L, N, P, S kept, Zs becomes a space, at most three combining marks after a kept character, the invisible set `U+034F, U+115F, U+1160, U+17B4, U+17B5, U+2800, U+3164, U+FFA0, U+180B..U+180F, U+FE00..U+FE0F, U+E0100..U+E01EF` and every other code point (Cc, Cf, Cs, Co, Cn, Zl, Zp, U+FFFC included) become `<U+XXXX>`; a literal `<U+` becomes `<U+003C>U+`. LF and TAB become markers too (every item is one line). Any `</untrusted` inside content is neutralized last (`&lt;/untrusted`). The implementation carries the algorithm text of awkit's sanitizer as its own code (a tool repo does not import awkit).

**Fencing.** Every session section is wrapped as `<untrusted source="transcript" id="NONCE">` ... `</untrusted id="NONCE">`, and the digest opens with the standing "data, not instructions" rule.

**Section hash.** Each session section is rendered first without the fence; `section_sha256` is the SHA-256 of those UTF-8 bytes; the fence is then added around it with the per-digest random nonce (`secrets.token_hex(16)`, 128 bits). The nonce stays random and never enters the retro key. The digest hash `digest_sha256` is the SHA-256 of the final `digest.md` bytes and differs between builds of the same content; it is an identity echo only. `manifest.json` repeats it and is derived from the same build, so it is not part of the hash.

## Retro lifecycle

| State | Meaning |
|---|---|
| `digesting` | The row exists and the digest is being built or the `pre-retro` hook runs |
| `running` | The start is recorded, the digest and its hash are stored and the retro session runs |
| `validating` | The session ended; the artifact is being checked |
| `done` | Validated and stored |
| `failed` | A final state with `reason` (see Retro validation) |
| `escalated` | A hook escalated, or the session reported blocked or needs-input |
| `canceled` | The command was interrupted or a hook vetoed the run |

**Reason.** `reason` is null when `state` is `done` and for non-final states. For `failed` it is one of the nine failure reasons listed once in Retro validation. The other final states carry `hook_escalated`, `session_blocked` or `session_needs_input` (`escalated`) and `interrupted` or `hook_vetoed` (`canceled`).

**Key.** The key is computed over the digested sessions, never over the scope or the digest bytes (copy):

```text
DIGEST_FORMAT = 1                                  # bumped by the tool when rendering rules change
def retro_key(kept_sessions, skill, model):
    # kept_sessions: the sessions that are digested (after every skip), each with its section_sha256
    parts = sorted((s.session_id, s.section_sha256) for s in kept_sessions)
    doc = {"v": DIGEST_FORMAT, "sessions": [[sid, h] for sid, h in parts],
           "skill": skill, "model": model or ""}
    return sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
```

A changed transcript, changed digest settings or a changed redaction list change the key; the scope does not enter it. A stored `done` retro with the same key is returned without a model call unless `--again`. The dry-run key and the `pre-retro` payload `key` equal the key a real run computes over the same transcript bytes.

**Leases.** A retro lease is `(holder_pid, holder_start_time, expires_at)` on the retro row. The holder renews it every 30 s while it works, including while it waits for `claude-session` and for hooks (a renewal thread); it expires 120 s after the last renewal. A lease is live when it has not expired, or when its holder PID runs with the recorded start time. A dead holder's lease may be taken over at once.

**Daily limit.** A start is recorded in `retro_starts(local_day, retro_id)` in the same `BEGIN IMMEDIATE` transaction that moves a retro to `running` (Order of checks step 16) after counting today's rows. `retro delete` and `prune` of retros never remove `retro_starts` rows; `prune` removes rows older than two days. `local_day` is the date in `time_zone` at the start. Vetoed, escalated-by-hook, refused, reused and dry runs never start, so they never count; every run that reached `running` counts whatever its final state. A `daily_limit` refusal at step 16 deletes the row in the same transaction, so a refused run leaves no retro, like every other refusal. The limit is checked after the hook so a veto never consumes a start; a hook therefore runs even on a day whose limit is spent, which is accepted.

**`quiet_after`.** A session whose newest transcript file (main or subagent) changed within `retro.quiet_after` (default 30 minutes) is skipped with `reason: live`.

**No resume.** An `escalated` or `failed` retro is ended with `retro delete` or replaced with `retro run --again`. There is no resume in 0.x.

**Crash recovery and cleanup retries.** They run in the housekeeping step (start of every `retro run`, step 5, and every `prune`); never in `ingest` or `validate`; `--dry-run` only lists (copy):

```text
housekeeping (start of retro run, and prune; never ingest or validate; --dry-run only lists):
  for r in retros where state in (digesting, running, validating) and lease not live:
      if r.csid is null: final(r, "failed", "runner_lost"); delete_local(r); continue
      st = cs("status", r.csid, "--json")
      if exit 0 and st.labels lack "owner=session-audit" or "retro=" + r.id:   # sanity: never touch a foreign session
          final(r, "failed", "runner_lost"); r.warnings += ["recovery_skipped"]; continue
      if exit 0 and run is live: cs("stop", r.csid, "--grace", "10s", "--json")
      if exit 0: collect(r)
      cleanup(r); final(r, "failed", "runner_lost")      # fires post-retro and post-attention retro_failed, actor auto:recover
  for r in retros where cleanup == "pending" and state is final: cleanup(r)
```

Recovery acts only on the `csession_id` stored before launch, after checking its labels; it never looks a session up by label. It ignores `harness.enabled` and `--no-harness` (it is the retro's own session). The artifact of a failed retro is not copied (nothing reads an unvalidated artifact). `validate` only warns (`retros_unrecovered`, `cleanup_incomplete`). An orphan can live up to a day when no retro runs; it is bounded by `retro.timeout`, because claude-session ends the run itself, so recovery is bookkeeping and cleanup, not the only stop. Expired digests are also deleted by every housekeeping step, so a user without the cron block keeps no digest past `retention.digests` once any retro runs.

**Signals.** Handlers for SIGINT, SIGTERM and SIGHUP are installed before step 14 (copy):

```text
handlers for SIGINT, SIGTERM, SIGHUP installed before step 14 (row insert)
before launch: mark canceled/interrupted, delete_local, exit 130 | 143 | 129
during exec:   send the same signal once to the exact exec child PID the tool spawned (claude-session forwards it;
               the run ends killed or not-started launch_interrupted); wait up to 30 s; if exec still runs:
               cs("stop", csid, "--grace", "5s"); then collect, cleanup, final canceled/interrupted, exit 130 | 143 | 129
during validating or cleanup: finish cleanup (bounded 30 s), final canceled/interrupted, exit by signal
a second signal while handling: exit at once with the same code (housekeeping recovers later)
SIGPIPE: default disposition is ignored; a failed stdout write ends with exit 1
```

### Order of checks

One numbered order for `retro run` (copy as written):

```text
retro run:
 1  parse flags; no scope flag or --mode interactive -> exit 2
 2  load config; unsafe config -> 20 config_unsafe; store newer -> 20 store_newer
 3  claude-session present and >= MIN_CS -> else 12 claude_session_missing | claude_session_too_old
 4  skill preflight -> else 12 skill_not_found
 5  housekeeping (not with --dry-run): recover expired retros, retry pending cleanups, delete expired digests
 6  resolve scope: unknown --session id -> 10; ambiguous prefix -> 21; nothing matched -> 20 empty_scope
 7  refresh the matched sessions' files (--dry-run reads in memory instead)
 8  skips: live, transcript_missing, transcript_changed, empty; nothing left ->
        20 live_sessions_only if every skip is live, else 20 empty_scope
 9  cap: explicit --session list over retro.max_sessions -> 20 scope_too_large; filter scope -> keep newest N, rest skipped over_cap
10  build the digest in memory; compute key
11  stored done retro with key and no --again -> print it, reused: true, exit 0 (no hook, no limit, no row)
12  non-final retro with key and live lease -> 20 already_running
13  --dry-run -> print the plan (with daily_remaining) and exit 0; nothing is written, no hook runs
14  BEGIN IMMEDIATE: insert retro row (state digesting, lease, key); COMMIT
15  pre-retro hook (lease renewed meanwhile): veto -> canceled/hook_vetoed, exit 11; escalate -> escalated/hook_escalated, exit 13
16  BEGIN IMMEDIATE: count retro_starts for today; >= limits.retros_per_day -> delete the row, COMMIT, exit 20 daily_limit;
        else insert retro_starts row, state running; COMMIT; then write digests/<id>/ and the prompt
17  launch, wait, collect, validate, cleanup, final state, post-retro, exit (Retro launch, Outcome mapping, Retro validation)
```

### Freshness

The digest reads exactly the bytes the store counted (copy):

```text
refresh (step 7): ingest the matched sessions' files only (no harness), waiting at most 60 s for the ingest lease;
    on timeout, go on with stored offsets and add warning ingest_busy
digest read: each file is opened with the Ingest rules and read only up to its stored offset; the stored
    dev, inode, head_sha and tail_sha must match, else the session is skipped transcript_changed
skip reasons: live (newest mtime of the session's main and subagent files is within retro.quiet_after),
    transcript_missing (no file of the session is on disk), transcript_changed, empty (no item), over_cap
--dry-run: reads to the current end of each file in memory (same bytes a real run would ingest), writes nothing
```

`scope_too_large` (explicit list) counts matched sessions before skips.

## Retro launch

`retro run` launches exactly one headless session through claude-session (binding A) after the `pre-retro` hook allows it. The tool never launches through agent-spawn in 0.x. MIN_CS = 0.45.0 (the first release with `--tools`; it also has `--no-implicit-done`, `forget --purge` and `num_turns`). The tool runs `claude-session --version` and reads the first `\d+\.\d+\.\d+` token of its first line; a missing or lower version, or output with no version, is exit 12 `claude_session_too_old` (retryable false) before anything is stored. `validate` checks the same.

**0. Skill preflight** (Order of checks step 4; nothing is stored before it passes):

```text
def skill_preflight(skill):
    if ":" not in skill: exit 2 (bare skill names cannot load with setting sources none; deviation)
    plugin, name = skill.split(":", 1)
    for d in [bundled_plugin_dir] + config.retro.plugin_dirs:
        pname = manifest_name(d) or basename(d)          # .claude-plugin/plugin.json "name", read with a 64 KiB cap
        if pname == plugin and is_regular_file_nofollow(d / "skills" / name / "SKILL.md"):
            return d
    exit 12 skill_not_found (retryable false)            # before any row, digest or spend
```

The directory found is passed with `--plugin-dir` (the bundled plugin always is).

**1.** The tool writes `digest.md` and `manifest.json` under `digests/<retro_id>/` and the prompt file under `prompts/<retro_id>/`.

**2.** Before `start`, it runs `claude-session start --dry-run --json` with the same flags and refuses to launch unless the plan matches what the tool asked for (copy):

```text
plan = cs("start", *flags, "--dry-run", "--json")      # nothing allocated
if exit == 20 or plan.would_refuse:  -> exit 12 session_not_started, details.end_reason = "refused"
if exit == 12 or plan.would_fail:    -> exit 12 session_not_started, details.end_reason = plan.would_fail
expect = {"tools": sorted(retro.tools), "reads_third_party": True, "implicit_done": False, "setting_sources": []}
if sorted(plan.tools) != expect.tools or not plan.profile.reads_third_party or plan.implicit_done or plan.profile.setting_sources:
    -> exit 12 launch_settings_mismatch (retryable false); nothing stored
```

The plan field names are claude-session's `schemas/cli/exec-dry-run.json` names; where it does not expose `setting_sources`, only `tools`, `reads_third_party` and `implicit_done` are compared.

**3.** It runs `claude-session start --json` with the per-call settings below (all launch flags go to `start`; `exec --session-id` accepts none of them), stores the printed `csession_id` on the retro row (and in `retro_sessions`) in its own transaction, then runs the printed `argv` (absolute `claude-session`, `exec --session-id <id> --run <n>`) adding `--outcome-file <state>/outcomes/<retro_id>.json` and `--json`. `start` stages copies of the prompt and inputs, so the tool's own files are never read by the session.

**4.** Per-call settings (each replaceable by the config keys in Config, never by another tool's config):

- `--kind audit --reads-third-party`, which makes the run sandbox `required`, no setting sources, `mcp: none` and no environment inheritance, and taints the session;
- `--no-implicit-done`;
- `--skill <skill>`, `--plugin-dir <dir>` for each plugin directory in use, `--prompt-file`, `--input-file` for each input, `--cwd session-dir` (claude-session's empty per-session `$CSESSION_DIR/work/`);
- `--permission-profile <bundled profile file>`, `--sandbox required`, `--setting-sources ""`, `--env-allowlist-only` for each name in `retro.env_allowlist`;
- `--tools Read Write Bash Skill`;
- `--timeout` (default `retro.timeout`, 20 minutes) and `--max-turns` (default `retro.max_turns`, 12);
- `--model` only when set;
- `--label owner=session-audit --label retro=<retro_id>`.

**5.** No report or escalation instructions are added by the tool; claude-session appends its standard instructions.

**6.** The tool waits for the adopting `exec`, parses its outcome object from stdout (the outcome file is the fallback when stdout is not a complete JSON object), then collects, validates and cleans up (below).

**7.** The retro's `cost_usd`, `num_turns` and `sandbox_active` (from `status --json` `run.sandbox_active`, boolean or null) are stored on the retro and are not part of the usage store (the transcript is self-excluded).

The prompt file holds only: the retro id, the run token, the digest hash, the names of the skill's output rules and the input paths relative to the session directory (`inputs/1/digest.md`, `inputs/1/manifest.json`); the skill joins them with the absolute session directory claude-session's bootstrap prompt and context line give it. It carries no transcript text.

### Outcome mapping

Every claude-session result maps to one retro state, reason and exit (Exit codes and Retro validation link here):

| claude-session result | Retro state | Retro reason | session-audit exit, `details` |
|---|---|---|---|
| `start --dry-run` would refuse or fail; `start` exit 12 `launch_failed` | none (no row past step 13) or `failed` when the row exists | `session_not_started` | 12 `session_not_started`, `details.end_reason`, `retryable` |
| adopting `exec` with `phase: launch` (exit 11, 12, 13) | `failed` | `session_not_started` | 12 `session_not_started`, `details.end_reason` (the claude-session token), `retryable` |
| exit 0, outcome `done` | validated: `done`, or `failed` | null, or a validation reason | 0, or 22 with the reason |
| exit 13, session phase, outcome `blocked` / `needs-input` | `escalated` | `session_blocked` / `session_needs_input` | 13 `needs_human` |
| exit 14, 16, 17, 18 | `failed` | `session_failed` (the token is in `outcome`) | passed through, `details.outcome` |
| exit 15 not caused by the tool's own signal | `failed` | `session_failed` | 15 |
| exit 15 or launch_interrupted after the tool forwarded a signal | `canceled` | `interrupted` | 129, 130, 143 |
| exit 1, 2, 10, 20, 21, an unparseable outcome, or `status` failing at collection | `failed` | `claude_session_error` | 12 `claude_session_error`, retryable false |

```text
retryable(end_reason) = end_reason in {"launch_interrupted", "never_adopted"}   # every other launch end reason: false
```

### Collect and cleanup

Collect first, then forget, then delete; retry only what can be retried (copy):

```text
def collect(r):                                   # after exec returns, or in recovery after stop
    st = cs("status", r.csid, "--json")            # exit 10: the session is gone; skip to delete_local
    r.claude_ids = [(c.claude_session_id, c.transcript_path) for c in st.claude_session_ids]
    tp = cs("transcript", r.csid, "--paths", "--json", "--subagents")
    r.run_files = [p.path for p in tp.files if p.exists]          # main and subagent files
    store(r.claude_ids, r.run_files, r.cleanup = "pending")       # one transaction, before forget
    copy frozen artifact (Retro validation) if st.run.report.artifact
def cleanup(r):
    rc = cs("forget", r.csid, "--purge", "--json")
    if rc == 20: cs("stop", r.csid, "--grace", "5s", "--json"); rc = cs("forget", r.csid, "--purge", "--json")
    purge_ok = rc == 0 or rc == 10                # 10: already forgotten earlier
    if rc == 12: r.warnings += ["purge_incomplete"]  # cannot be retried: claude-session deletes it at delete_after
    files_ok = (not retro.remove_run_transcripts) or remove_run_files(r)    # retryable
    delete prompts/<id>/ and outcomes/<id>.json      # tool-owned, retryable
    r.cleanup = "done" if files_ok and rc in (0, 10) else ("incomplete" if rc == 12 and files_ok else "pending")
def remove_run_files(r):                          # each check on the opened directory descriptor
    for path in r.run_files + [main path of each claude id]:
        root, proj, rest = split under a transcript root (else skip, warning)
        require proj matches r"-(cs-[a-z0-9]{8})-work$" with group == r.csid
        require rest is "<claude_id>.jsonl" or "<claude_id>/subagents/**/agent-*.jsonl" for a collected claude_id
        open root, then proj, with O_NOFOLLOW|O_DIRECTORY; lstat via fstatat(AT_SYMLINK_NOFOLLOW): regular, nlink 1, owner me
        unlinkat(dirfd, name)
    for each collected claude_id: remove <proj>/<claude_id>/ (fd-based, no symlink followed, same device) and
        <claude config dir>/session-env/<claude_id>/ with the same checks; rmdir proj if empty
    return every step succeeded
```

`pending` cleanups are retried by the housekeeping step; `incomplete` means only claude-session's own copy remains until its `reconcile` removes it, and `validate` warns `cleanup_incomplete`. With `retro.remove_run_transcripts: false` the Retro object's `warnings` includes `transcripts_kept`.

## Slot contract

The slot is `audit`; the default skill is `session-audit:audit`, bundled in `plugin/`. The skill is written in the implementation plan's skill issue.

The bundled permission profile is `plugin/profiles/audit-retro.yaml`, a claude-session profile file (claude-session interface, Permission profiles). claude-session generates the `Read` of `$CSESSION_DIR`, the `Write`/`Edit` of `artifacts/` and `work/`, the `claude-session report` Bash allow rules and permission mode `dontAsk` (anything not allowed is denied). The file adds only `permissions.deny`: `Agent`, `Task`, `WebFetch`, `WebSearch`, `NotebookEdit`, `mcp__*`, `Bash(memoctl:*)`, `Bash(agent-spawn:*)`, `Bash(forgectl:*)`, `Bash(ticketctl:*)`, `Bash(notice-route:*)`, `Bash(awkit:*)`, `Bash(session-audit:*)`, and `sandbox.network.allowedDomains: []`.

The marker is the single line of `plugin/skills/audit/references/marker.txt` (pattern `^[a-z0-9-]{16,64}$`), changed in every release that changes the skill. The tool reads it from its installed package; the skill reads it with the Read tool. A custom skill's marker is `retro.skill_marker`. The marker proves the skill's reference file was read; it is not an injection defense (an injected session can copy it).

**Inputs** under `$CSESSION_DIR/inputs/<run>/`:

- `digest.md`: the fenced digest (Digest).
- `manifest.json`: `{"retro_id", "run_token", "digest_sha256", "sessions": [{"ref", "csession_id" or null, "items": [ids], "metrics": [names]}], "coverage": {...}, "limits": {"max_findings", "max_candidates", "text_limits"}, "finding_categories", "target_classes"}` (`schemas/slot/manifest.json`). `coverage` is the object defined in Digest (Budget). `csession_id` is null for a session with no harness match. `items` lists the kept item ids of the session; `metrics` lists bare metric names (`tool_calls`, not `s1.tool_calls`). An evidence ref `s<N>.<name>` is valid when `<name>` is in that session's list, and `total.<name>` when any session lists it. `text_limits` has `summary`, `title`, `detail`, `target_hint` and `proposal`. `digest_sha256` is the SHA-256 of `digest.md` only (see Digest). The manifest holds ids, counts and enums only; there is no `project`.

**Output**: one artifact named `audit.json` in `$CSESSION_DIR/artifacts/`, attached with `claude-session report done --summary-file ... --artifact audit.json`. The skill must use literal absolute paths in the report command and must read its inputs with the Read tool (Bash is denied for anything but the report command).

The standard instructions, escalation and reporting come from claude-session; the skill must not restate them.

## Retro artifact

`schemas/slot/artifact.json` is the closed shape the model writes. Exactly these keys:

| Key | Type | Rule |
|---|---|---|
| `retro_id` | string | Must equal the run's retro id |
| `run_token` | string | Must equal the run's token |
| `digest_sha256` | string | Must equal the digest hash |
| `marker` | string | Must equal the expected marker whenever one exists (bundled skill, or `retro.skill_marker` for a custom skill); absent otherwise |
| `summary` | string | At most 2000 characters; untrusted |
| `findings` | array | At most `limits.max_findings` (50) |
| `candidates` | array | At most `limits.max_candidates` (20) |

A finding has `id` (`f1` to `f50`), `category` (`went_well`, `went_badly`, `friction`, `cost`), `severity` (`low`, `medium`, `high`), `title` (at most 120 characters), `detail` (at most 1500 characters) and `evidence` (1 to 8 items, each `{"kind": "item"|"metric", "ref": "<id>"}`). A candidate has `id` (`c1` to `c20`), `target_class` (`skill`, `instructions_file`, `config`, `permission_profile`, `workflow`), `target_hint` (at most 200 characters), `proposal` (at most 1500 characters) and `findings` (ids of the findings that support it; each must exist). All model text is untrusted and marked `x-untrusted` in the schema. The whole file is at most 256 KB. No other keys are allowed at any level. A candidate must name at least one finding, and `title`, `detail`, `target_hint` and `proposal` must be non-empty. The schema cannot express the 256 KB cap, the uniqueness of finding and candidate ids (tool-enforced), the equality of the identity keys with the stored values, or that `marker` is required when a marker exists; the tool enforces those in Retro validation.

The stored Retro object adds tool-written fields the model cannot set: `scope`, `scope_resolved`, `coverage` (from the manifest), `counts` and `metrics` (from the store), `sessions`, identity, state, `cost_usd`, `num_turns`, `outcome`, `sandbox_active`, `cleanup`, `warnings`, `redacted`, timestamps and `again_of`, and for each candidate a tool-written `risk` (`high` for `permission_profile`, `config`, `instructions_file`; `medium` for `skill`, `workflow`) and `sessions` (the distinct session refs its findings cite). Candidate text is a prompt-injection channel into later trusted sessions: a consumer must fence it and a human must review it.

## Retro validation

A retro run is a success only when every check holds; otherwise it is `failed` (exit 22 for a validation reason, unless the session itself ended another way, in which case its code is passed through; see Outcome mapping). The checks run in this order (copy):

```text
1  run.report.implicit true, or report.artifact null              -> no_artifact
2  report.artifact.name != "audit.json", report.artifact.size > 262144  -> artifact_invalid
3  copy the frozen file (open O_NOFOLLOW, regular, read at most 262145 bytes); size or SHA-256 != report.artifact -> artifact_invalid
4  strict JSON parse: UTF-8, no duplicate keys, no NaN/Infinity, nesting depth <= 16, integers |n| < 2^53;
   closed schema; finding ids unique, candidate ids unique                              -> artifact_invalid
5  retro_id, run_token, digest_sha256 equal the stored values                          -> identity_mismatch
6  marker equals the expected marker whenever one exists (bundled skill or retro.skill_marker) -> marker_mismatch
7  references:
     drop every finding with any evidence ref not in the manifest
     remove dropped finding ids from every candidate's findings; drop a candidate citing an id that never existed
     drop a candidate left with no findings
     if the model wrote at least one finding and none survive                         -> no_valid_findings
     zero findings written by the model is a valid done retro
     ids are kept as written (no renumbering); dropped.findings and dropped.candidates count both causes
8  text: normalize and redact every model string; never a failure
```

Check 8: every model string (`summary`, `title`, `detail`, `target_hint`, `proposal`) goes through `fix_surrogates`, NFC, the redactor (see Digest) and, for each literal value v used by the masks, a check for `base64(v)` (three alignments), hex and URL-encoded `v`; each hit becomes `[REDACTED:<kind>]` and counts in the Retro object's `redacted`. Control, escape and bidi characters are kept as stored text but every display sanitizes them.

The failure reasons, listed once, are `runner_lost`, `session_not_started`, `claude_session_error`, `session_failed`, `no_artifact`, `artifact_invalid`, `identity_mismatch`, `marker_mismatch` and `no_valid_findings`; the last five are the validation reasons (exit 22). A failed check 7 drops only the findings (or candidates) with the bad reference and records `dropped` counts; any other failed check discards the whole artifact. `counts.findings` equals the stored findings length (tool-enforced).

Numbers in the stored retro (`counts`, `metrics`, cost) come from the store and the outcome object. Any number the model writes is not used.

## `--json` outputs

Schemas live in [`schemas/cli/`](../schemas/cli/), the slot schemas in [`schemas/slot/`](../schemas/slot/). Every command that prints data has one. Definitions shared by several schemas (Session, Retro, scope, coverage, ids, the error envelope) are in [`schemas/defs.json`](../schemas/defs.json); every schema file other than `config.json` references it, and `config.json` is self-contained.

| Command | Schema | Shape |
|---|---|---|
| `ingest` | `ingest.json` | the ingest report (see `ingest`) |
| `sessions` | `sessions.json` | `{sessions: [Session], truncated, store}` |
| `usage` | `usage.json` | `{by, rows, totals, truncated, store}` |
| `show` | `show.json` | `{session, agents, usage, store}` |
| `retro run` | `retro-run.json` | Retro plus `reused`, or the dry-run plan |
| `retro list` | `retro-list.json` | `{retros: [RetroRow], truncated}` |
| `retro show` | `retro-show.json` | Retro with artifact and, with `--digest`, the fenced digest |
| `retro delete` | `retro-delete.json` | `{deleted: [ids]}` |
| `prune` | `prune.json` | `{dry_run, tables, recoveries}` |
| `validate` | `validate.json` | `{ok, checks, warnings}` |
| `cron install\|uninstall` | `cron.json` | `{command, outcome, dry_run, block}` |
| `export` | `export-*.json` | one object per line |
| error | `error.json` | the envelope |

The schemas hold the field lists; this section keeps only rules a schema cannot express.

- **Session object.** `project` is the first non-empty `cwd` of the main file (capped at 4096); when none, the project directory name with flag `project_from_dir`. `entrypoint` is `cli` mapped to `interactive`, a value starting `sdk-` to `headless`, anything else or missing to `unknown`, from the first line that has the key; `version` is the last seen. `harness` is null or the snapshot (`run` and `updated_at` included; `kind` is the launch kind; `outcome` and `end_reason` are lowercase tokens owned by claude-session, not enums here; `source` is always `harness`). Aggregation: a `usage` or totals `active_seconds` is null when every contributing session's is null; `ended_at >= started_at` and `tool_errors <= tool_calls` are tool-enforced.
- **Retro object.** `key` is computed as in Retro lifecycle (Key). `key`, `coverage`, `csession_id`, `cost_usd`, `num_turns`, `outcome`, `sandbox_active`, `cleanup`, `summary`, `model` and `finished_at` are nullable (a retro in `digesting` has no key or coverage yet). `scope` is descriptive: `{sessions, labels, project, since, until, kind, outcome}` as given, with `sessions` resolved to full ids, `since` and `until` kept as typed (a duration stays a duration) and the resolved times in `scope_resolved: {since, until}`. `counts` is `{sessions, skipped, items, findings, candidates}`. `metrics` maps metric ids (`s1.tool_calls`, `total.denials`) to numbers. `dropped` is `{findings, candidates}`. `skipped` rows are `{session_id, reason}` with reason `live`, `transcript_missing`, `transcript_changed`, `empty` or `over_cap`. `sessions` is `[{ref, session_id, csession_id}]` (the kept sessions, `csession_id` from the harness snapshot or null), kept for the retro's life. `cleanup` is `pending`, `done` or `incomplete`. `warnings` is a closed set: `purge_incomplete`, `transcripts_kept`, `recovery_skipped`, `ingest_busy`. `redacted` counts redactions in model text. `outcome` is a lowercase token from claude-session. `reason` and `state` are coupled (see the schema `allOf`): non-final states carry null; `done` null with non-null `key`, `coverage` and `summary`; `failed` a failure reason; `escalated` `hook_escalated`, `session_blocked` or `session_needs_input`; `canceled` `interrupted` or `hook_vetoed`.
- **Untrusted fields.** Fields that carry transcript or model text are marked `x-untrusted` in the schemas: Retro `summary`, finding `title` and `detail`, candidate `target_hint` and `proposal`, harness `labels` values, `skill` and `repo`, session `project`, `version`, agent `agent_type`, model names, usage keys, ingest `warnings` and the `retro show` `digest`. A consumer must treat all of them as untrusted.
- **Display.** Every non-JSON output passes each untrusted string (every field marked `x-untrusted`, every path, model, tool and label value, every `warnings` string) through the sanitizer at print time. `--json` output is ASCII (`ensure_ascii=True`), so control and bidi characters are escaped.

Sessions, retros and `usage` output must be validated by tests against these schemas.

## Hooks

Hooks are configured in the tool's own config (and drop-ins; there is no `--hook` flag), run via `sh -c` in their own process group, and receive a JSON payload on stdin. Environment: `SESSION_AUDIT_HOOK`, `SESSION_AUDIT_ID`, `SESSION_AUDIT_OBJECT_TYPE`. Hooks inherit the tool's environment. Default timeout 30 seconds: SIGTERM to the hook's process group, 5 s, SIGKILL. Payloads carry ids, counts and enums, never transcript or model text, and are closed objects. Schemas in [`schemas/hooks/`](../schemas/hooks/).

| Hook | When | Payload (besides `tool`, `hook`, `object_type`, `object_id`, `event_id`, `labels`) | Effect |
|---|---|---|---|
| `pre-retro` | After the row is created (Order of checks step 15) and before the session is launched | `scope`, `sessions`, `digest_bytes`, `items`, `skill`, `model`, `key`, `max_turns`, `timeout_seconds` | exit 0 continue; 13 escalate (retro `escalated`, reason `hook_escalated`, exit 13); other non-zero, a timeout or a signal: veto (retro `canceled`, reason `hook_vetoed`, exit 11) |
| `post-retro` | When a retro reaches a final state | `retro_id`, `state`, `reason`, `findings`, `candidates`, `cost_usd` | cannot undo; failure logged and warned |
| `post-attention` | A closed `reason` | `reason` (`retro_failed`, `ingest_failing`), `object_type` (`retro` or `ingest`), `details` (a closed set of ids and enums) | cannot undo |

Entries of one hook run in config order (drop-ins appended in lexical order). `pre-retro`: the first entry that exits non-zero decides (13 escalate, any other non-zero, a timeout or a death by signal veto); later entries do not run.

`pre-retro` stdout: at most 64 KiB read; it must be empty or one JSON object (no duplicate keys, no NaN); `decision_id` is a string or a list of strings matching the decision-id grammar; at most 32 ids per retro (hook and flag ids together); anything else is ignored with a stderr warning and a `hook_output_ignored` count in the log entry, never a veto. Every returned id is recorded in the retro's log entry.

`--decision-id ID` on `retro run` is caller supplied, validated against the same grammar, and merged with hook ids (flags first).

The lease is renewed while hooks run (see Retro lifecycle, Leases).

`labels` in every payload is always `{}` (retros have no labels; design guide §8 allows a tool with no labels to send an empty map).

`post-retro.state` is one of the four final states (`done`, `failed`, `escalated`, `canceled`) and `reason` follows the Retro lifecycle rule. `post-retro` fires once per retro reaching a final state, including `hook_vetoed`; never for `reused`, refusals or dry runs.

`event_id` is `ev-` plus 26 lowercase base32 characters (random). `post-` hooks are best effort and at most once, may run `async: true`, and carry a unique `event_id`. There is no outbox in 0.x.

In payloads, `sessions` in `pre-retro` is a count (never ids or text) and `scope` is the retro scope object. `post-attention.details` is `{retro_id, failure_reason, csession_id}` for `retro_failed` and `{consecutive_failures, first_failed_at, last_failed_at}` for `ingest_failing`; `object_id` is the retro id, or the literal `ingest` for an `ingest` attention.

`post-attention` fires `retro_failed` when a retro ends `failed`, and `ingest_failing` by this rule (copy):

```text
a "full ingest" is one without --path, --session, --since, --rebuild or --dry-run
full ingest exits 1 or 12 -> meta.fail_count += 1; meta.last_failed_at = now; first_failed_at set if null
full ingest exits 0 (complete or not; already_running excluded) -> fail_count = 0; first_failed_at = null
fire ingest_failing when fail_count >= ingest.fail_threshold and (meta.last_fired_at is null or older than 24 h);
    set meta.last_fired_at = now
```

`object_type` is `retro` for the first two hooks and may be `ingest` for `post-attention`. Payloads have no `repo` (a session is not tied to one), so a gate matches them in scope `*`.

The hooks exclude their own retro runs: a `post-state-change` hook of claude-session wired to this tool must exclude the kind `audit`.

## Config

Location `~/.config/session-audit/config.yaml` (respecting `XDG_CONFIG_HOME`), drop-ins `~/.config/session-audit/config.d/*.yaml` merged in lexical order (maps merge, hook lists append), `--config` overrides. Schema [`schemas/config.json`](../schemas/config.json), example `config.example.yaml`. Precedence: flags, then environment variables for the few keys that have one, then config, then built-in defaults. There is no `repos:` layer.

`retro run`, `retro delete`, `ingest`, `prune` and `cron` refuse with exit 20 `config_unsafe` when the config file, `config.d/`, a drop-in or any directory from the config directory up to `$HOME` is a symlink, not owned by the user (root-owned ancestors above `$HOME` are not checked), or group or other writable; read-only commands and `validate` report it. YAML is loaded with `yaml.safe_load` and a 1 MiB size cap per file.

| Key | Default | Meaning |
|---|---|---|
| `transcript_roots` | the two default roots | Replaces them |
| `time_zone` | the machine's zone | Day, week and month bucketing and the daily limit |
| `ingest.budget` | `300s` | Time budget per run (at most 3600 s) |
| `ingest.file_budget` | `60s` | Time budget per file (at most 600 s) |
| `ingest.fail_threshold` | `3` | Consecutive failed full ingests before `ingest_failing` |
| `harness.enabled` | `auto` | `auto` (use claude-session when installed), `on` (a missing claude-session makes `ingest` exit 12 `claude_session_missing` and `validate` fail the check), `off` |
| `harness.claude_session_cmd` | `claude-session` | Binding A command name |
| `prices` | bundled, with `as_of` | USD per million tokens per model |
| `cost.mismatch_margin` | `0.10` | Fraction for the `cost_mismatch` flag |
| `retention.sessions` | `400d` | Sessions, agents, events, harness snapshots |
| `retention.retros` | `180d` | Stored retros |
| `retention.digests` | `7d` | Stored digests |
| `retention.log` | `90d` | `log.jsonl` entries |
| `limits.retros_per_day` | `5` | Daily retro limit |
| `retro.skill` | `session-audit:audit` | The slot's skill (`plugin:skill`) |
| `retro.skill_marker` | none | Marker for a custom skill |
| `retro.plugin_dirs` | `[]` | Extra plugin directories for a custom skill; each passed with `--plugin-dir` |
| `retro.model` | none | Claude Code's default when none |
| `retro.timeout` | `20m` | Session timeout (at most 2 h) |
| `retro.max_turns` | `12` | Session turn bound |
| `retro.max_sessions` | `20` | Sessions per retro (see `retro run`) |
| `retro.quiet_after` | `30m` | Minimum age since last change of a session's transcript (at most 7 d) |
| `retro.permission_profile` | bundled file | Replaces the bundled profile file |
| `retro.sandbox` | `required` | The sandbox level passed per call; a lower value is passed (claude-session audits it `sandbox_lowered`), `validate` warns `sandbox_lowered`, and a scheduled retro (`--actor auto:schedule`) refuses with exit 20 `config_unsafe` |
| `retro.setting_sources` | `[]` | Must stay `[]` for a third-party-reading run (claude-session fails the launch `unsafe_setting_sources` otherwise); kept because design guide §11 requires the key |
| `retro.env_allowlist` | `[PATH, HOME, USER, LANG, TERM]` | The names passed with `--env-allowlist-only` |
| `retro.tools` | `[Read, Write, Bash, Skill]` | The `--tools` list |
| `retro.remove_run_transcripts` | `true` | Remove the retro session's own transcript files after collecting |
| `digest.max_bytes` | `200KB` | Total digest cap |
| `digest.session_max_bytes` | `20KB` | Per-session cap |
| `digest.prompt_chars` | 500 | Cut of a prompt item |
| `digest.assistant_chars` | 300 | Cut of an assistant item |
| `digest.arg_chars` | 200 | Cut of a tool argument summary |
| `digest.error_chars` | 300 | Cut of an error, denial, interruption or rejection item |
| `redaction.mask_env` | `[]` | Variable names whose set values are masked |
| `redaction.extra_token_prefixes` | `[]` | Strings such as an organization's token prefix; `<prefix>[A-Za-z0-9_-]{8,256}` is redacted |
| `schedule.retro` | absent (off) | Optional weekly retro from cron: `{weekday, hour, scope}` |
| `hooks` | none | Hook lists (see Hooks) |
| `me.name` | OS user name | Actor name |

Durations accept a number of seconds or the guide's grammar (`ms`, `s`, `m`; no unit means seconds, zero is refused); only the keys that name days (`retention.*`, and `since` and `until` of `schedule.retro.scope`) also accept `d`. There is no `h` unit: write `60m`. Maxima: `retention.*` 3650 d, `retro.timeout` 7200 s, `retro.quiet_after` 7 d, hook timeout 600 s, `ingest.budget` 3600 s, `ingest.file_budget` 600 s. Sizes (`digest.*_bytes`) are a byte count or a number followed by `B`, `KB` or `MB` (1 KB = 1024 bytes). `digest.*_chars` accept 20 to 100000, `retro.max_sessions` 1 to 500 and `limits.retros_per_day` 1 to 1000.

Shapes the table leaves out: each `hooks` entry is `{cmd, timeout, async}` (`async` only on `post-retro` and `post-attention`); `prices` is `{as_of, models: {<name>: {input, output, cache_read, cache_write, cache_write_1h?}}}` and extends the bundled table; `schedule.retro` is absent (off) or `{weekday, hour, scope}` with `weekday` 0 to 6 (0 Sunday, default 1), `hour` 0 to 23 (default 9) and `scope` keys `labels`, `project`, `since` (required, a duration ago with `d`), `until`, `kind` and `outcome`; `retro.sandbox` is `required`, `preferred` or `off` (claude-session's levels); `retro.setting_sources` items are `user`, `project` or `local`; `retro.plugin_dirs` is at most 8 absolute paths; `redaction.extra_token_prefixes` are strings of 3 to 64 characters `^[A-Za-z0-9_-]+$`, at most 64. `retro.skill_marker` and `retro.model` have no default (absent); `transcript_roots`, `time_zone`, `prices` and `me.name` have computed defaults, shown commented out in `config.example.yaml`. Invalid config fails fast with a message naming the key and never the value.

## Environment variables

- `SESSION_AUDIT_TIMEOUT` (`retro.timeout`) and `SESSION_AUDIT_MODEL` (`retro.model`) are the only config environment variables. Profiles, skills, prices and redaction cannot be set from the environment.
- Read but not owned: `CLAUDE_CONFIG_DIR` (a transcript root), `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `PATH`, `NO_COLOR`.
- Hook variables: `SESSION_AUDIT_HOOK`, `SESSION_AUDIT_ID`, `SESSION_AUDIT_OBJECT_TYPE`.
- No credential variable is read. The retro session gets only the names in `retro.env_allowlist` plus what claude-session always passes for Claude's own login.

## Labels

Retro sessions carry `owner=session-audit`, `retro=<retro_id>` and `kind=audit`. They are checked as a sanity guard before recovery touches a session; the tool never looks a session up by label. The tool filters sessions on harness labels (`--label K=V`) and gives no key a meaning.

## TUI

None in 0.x. awkit can read attention from `retro list --json`. A `browse` TUI is a 1.x item.

## Files

Under `~/.local/state/session-audit/` (respecting `XDG_STATE_HOME`; directories 0700, files 0600). Nothing under the state directory is ever a session-writable path.

- `state.db`: private SQLite (WAL, versioned additive-only migrations).
- `log.jsonl`: public, append-only, hash-only (`schemas/log.json`); `log.lock` guards appends and the prune rewrite.
- `digests/<retro_id>/`: `digest.md` and `manifest.json` while within `retention.digests`.
- `artifacts/<retro_id>/audit.json`: the validated artifact copy.
- `prompts/<retro_id>/` and `outcomes/<retro_id>.json`: deleted at cleanup.

There is no `audit.jsonl` (see Deviations).

## Log

`log.jsonl` records each completed `ingest`, each `retro` state change, each deleted retro and each `prune` with `ts` (millisecond UTC), `actor`, `action`, counts, ids and hashes. `action` is `ingest`, `retro_state`, `retro_delete` or `prune`. An `ingest` entry has `complete`, `files`, `lines`, `sessions`; a `retro_state` entry has `retro_id`, `state`, `reason`, `key`, `digest_sha256`, `again_of`, `csession_id`, `decision_ids` (the ids hooks returned), `hook_output_ignored`, `findings` and `candidates`; a `retro_delete` entry has `retro_id` and `key`; a `prune` entry has `tables` and `recovered` (count). There is no `tool`, `version` or `actor_source` field (`schemas/log.json`). It never holds transcript text, model text or a path value from a transcript. Entries older than `retention.log` are removed by `prune`. Appends and the prune rewrite take `log.lock` (an exclusive `flock`, waited for at most 5 s; design guide §4.3 leases cover store objects, a log file lock is a file write lock).

## Retention

`prune` applies these rules (copy):

```text
session age = coalesce(ended_at, last_ingested_at)        # ended_at is clamped at ingest to <= file mtime + 1 day
prune sessions with age < now - retention.sessions: delete their events, agents, harness rows and files rows,
    and record pruned_sessions(session_id, pruned_at); ingest skips a file of a pruned session unless its mtime > pruned_at
    (then it is read from the start as a new session); pruned_sessions rows older than 2 x retention.sessions are removed
prune retros with created_at < now - retention.retros, only in a final state (with artifact and digest)
prune digests older than retention.digests; log entries older than retention.log (rewrite under the log lock)
again_of may name a deleted retro (it dangles; retro show of that id exits 10)
```

It reports counts per table. It never touches transcripts, which belong to Claude Code. `retro delete` removes one retro and its files at once. Expired digests are also deleted by every `retro run` housekeeping step, and `validate` warns `no_cron_block` when no managed block exists.

## Cron

`cron install` writes one managed crontab block (copy the rules):

```text
block: exactly one line "# session-audit managed" followed later by exactly one "# session-audit managed end";
       any other count or order (none is fine) -> exit 20 cron_block_malformed; nothing is changed
ingest line: every <n>m -> "*/<n> * * * *"; every <n>h -> "7 */<n> * * *"; 24h -> "7 3 * * *"
prune line: "17 3 * * *" (03:17 in the cron daemon's zone)
retro line: "M H * * D" with M = 23, H = schedule.retro.hour, D = schedule.retro.weekday, running
            session-audit retro run <flags rendered from scope> --actor auto:schedule --quiet
PATH: ~/.local/bin, then the directories of claude-session and claude found on the installing process's PATH
values: every embedded path or scope value is shell-quoted; a value containing %, newline or NUL -> exit 2 naming it
--env-file: absolute, regular file, owned by the user, not group or other writable, not a symlink, else exit 2;
            validate re-checks it (warning env_file_unsafe); the line is ". '<path>' && <command>"
crontab missing or failing -> exit 12 crontab_unavailable
uninstall --dry-run: block lists the lines that would be removed
```

- `ingest --quiet` at `--ingest-every` (default `1h`, see Time arguments), a daily `prune --quiet`, and a retro line only when `schedule.retro` is set. A scheduled retro obeys the daily limit and the content key, so a repeat of unchanged input is free; refusals go to stderr (cron mails them), which is the intended signal.
- `cron install --dry-run --json` prints the block and what would change (`unchanged`, `replaced`, `installed`). `cron uninstall` removes only the managed block. awkit's `awkit cron install` runs the tool's own installer; this tool never edits the crontab outside its block.

## Safety

- The deterministic half reads transcripts under the Ingest rules and writes only its own state directory.
- The retro session is confined as in Retro launch and Slot contract. Writes outside the session's own `artifacts/`, `outbox/` and `work/` are refused by the sandbox (no `~/.config/` tool config and no state directory is writable), which covers design guide §12's config rule; the profile denies are defense in depth. A profile is guidance; the sandbox is the boundary. The tool verifies the effective tools and isolation with a dry run before every launch.
- Transcript text reaches the model only through the digest (redacted, sanitized, fenced) and never through a path the session can read. The manifest and prompt hold no untrusted string.
- The retro artifact is untrusted model text everywhere it is displayed or passed on. A consumer must fence it and must not follow instructions in it.
- Recovery acts only on the `csession_id` stored before launch, after checking its labels; it never looks a session up by label. The tool never resumes, forks or labels the sessions it audits, and never runs `claude-session forget` on any session but its own retro sessions.
- Labels are unauthenticated hints: a label scope is not a confidentiality boundary.
- A retro session is tainted by design (third-party-reading); the tool never resumes it.

## Platform notes

Linux and WSL (Ubuntu) are tested. macOS is expected to work: the roots, `flock` and the sandbox rules are the same, and a macOS checklist is part of the live-verification issue. A retro needs bubblewrap and socat on Linux and WSL2 (claude-session's requirement). A retro's cwd is claude-session's session directory; the live tests run a retro with the default `~/.local/state` and with `XDG_STATE_HOME` set.

## Residual risks

- Redaction is best effort; a secret in a shape the patterns do not know can reach the digest. The sandbox (no network, no write beyond the artifact) limits what a leaked value can do inside the session. claude-session's artifact check catches only literal values of its own `claude_auth_env` and `secret_env` (8 characters or more); session-audit redacts the artifact text again before storing, which is still best effort.
- Findings are model opinions about untrusted text. Evidence references prove only that the cited item exists in the digest, not that the finding is right. Runs of the same digest vary. Identity echoes, the marker and evidence references are integrity checks against mistakes; an injected session can satisfy all of them.
- Claude Code's own bundled skills remain loadable through `Skill` (claude-session: `Skill(...)` rules do not restrict skills); `--tools Read Write Bash Skill` removes the tools a skill would need to act, and the live test lists the offered tools and skills of a retro session.
- A forged transcript (a same-user process can write into `~/.claude/projects`) can add sessions with chosen counts and chosen text. The store treats every transcript as untrusted data; nothing it holds is an authorization. Counts are bounded so a forged line cannot overflow, wedge or make a session unprunable.
- Harness labels are unauthenticated, and a snapshot is a copy at one moment.
- `remove_run_transcripts` deletes files in Claude Code's store; it removes only the exact files of the tool's own retro session after verification.
- A `forget --purge` that returns `purged: false` cannot be retried by the tool; the digest copy in claude-session's session directory stays until claude-session's retention (30 days by default).
- When an event owner's session is pruned, its copies are not re-evaluated until a file holding them is read again.
- Transcript format changes in future Claude Code versions can make counts wrong without failing; the Session object's `unknown_line_types`, `lines.unknown` and the `no_usage` flag are the signals.

## Deviations from the design guide

- `--mode interactive` is not offered in 0.x (design guide §11 "must offer `--mode`"); it follows in 1.x through agent-spawn.
- The built-in `audit` kind row (trusted, sandbox off, environment inherited, implicit done) is not used by this tool: the retro launch passes stricter per-call settings (design guide §11). The row remains the fallback for a direct `claude-session exec --kind audit` by hand.
- `--skill` accepts plugin skills only (`plugin:skill`); bare user or project skill names cannot load in a session with no setting sources (design guide §11).
- `cron install --ingest-every` is an interval flag with minute and hour units, not a duration key (design guide §5).
- No `audit.jsonl`: the tool takes no external action (as memoctl and ticket-queue). A hash-only `log.jsonl` records completed changes.
- No `repos:` layer and no environment layer except two scalar keys.
- Hook delivery is best effort and at most once, with no outbox.

## Upgrades

The store is versioned and migrations are additive only. A newer `state.db` makes every command exit 20 (`store_newer`) except `validate`. The store records `meta.rules_version` and `files.rules_version`. After an upgrade that bumps it, every `ingest` first rereads files whose `rules_version` is lower (within its budget) before new bytes; `ingest --rebuild` sets every on-disk file to reread. A session whose files are all gone and whose rows came from a lower version carries `old_rules`. The config schema may add keys in a minor release; a removed key is a breaking change named in its release title.
