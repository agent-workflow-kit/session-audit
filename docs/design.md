# session-audit: design

Status: designed against architecture v1.31 and design guide v1.15. The normative rules are in [`interface.md`](interface.md); this document records why, and the internal structure. It never restates the interface's tables.

Section references (§N) point to `../awkit-docs/architecture.md` unless marked "design guide" (`../awkit-docs/design-guide.md`).

## Role and layer

Layer 3 (workflow), post-v1 (§4, §19). Analyzes Claude Code session transcripts: deterministic usage analytics from a store of counts, and model-written retros over a bounded, redacted digest. It is a skill-wrapper tool (§5.5, design guide §11) with the slot `audit`, kind `audit`, default skill `session-audit:audit` bundled in `plugin/`. It works fully standalone: the only sibling it calls is claude-session, and only the retro half requires it.

## Uses

| Tool | Binding | Used for | Required |
|---|---|---|---|
| claude-session | A (call by name, parse `--json`) | `list` and `status` for harness snapshots during `ingest`; `exec`, `status`, `forget --purge`, `transcript --paths` for retro runs | Optional for `ingest`, `sessions`, `usage`; required for `retro run` |

Not used: agent-spawn, wtctl, termctl (a headless retro needs no window or worktree), memoctl (nothing is written to notes), notice-route (hooks are wired by the user or the harness), forgectl, ticketctl. A missing claude-session makes `retro run` exit 12 with `details.reason: tool_missing` and the install command in the message.

Consumed by: self-improve (post-v1) through `retro show --json`; awkit through the read commands, `validate --json` and the cron block. session-audit knows neither.

## Resolved open topics

The architecture left four topics open (§20). They are resolved as follows.

| Topic | Resolution | Interface |
|---|---|---|
| What is audited | Every Claude Code transcript on disk, with claude-session records only as optional enrichment; usage, tools, errors, denials and cost per session, agent and day; retros over chosen scopes | [Ingest rules](interface.md#ingest-rules), [Harness enrichment](interface.md#harness-enrichment) |
| When it runs | `ingest` hourly from cron or on demand; retros only on explicit command, or from an opt-in weekly schedule; never at session end | [Cron](interface.md#cron), [Retro lifecycle](interface.md#retro-lifecycle) |
| Output | Counts in a store; retros as a closed JSON artifact plus findings and improvement candidates, read with `retro show --json` | [Retro artifact](interface.md#retro-artifact) |
| How improvements are applied | They are not, here. Candidates are suggestions; self-improve turns them into proposed edits a human accepts (§12.1) | [Overview](interface.md#overview) |

## Decisions

| # | Decision | Why | Interface |
|---|---|---|---|
| D1 | Read every transcript on disk, harness data only as enrichment | Usage is most useful across everything run by hand and by the harness; the tool must work with no harness installed; claude-session exposes no transcript listing, tokens or per-session cost | [Ingest rules](interface.md#ingest-rules) |
| D2 | A store of derived facts with no conversation text | Claude Code deletes transcripts after about 30 days and claude-session sessions leave its commands after `forget`; counts survive, text would add a secret and injection surface | [Ingest rules](interface.md#ingest-rules) item 8 |
| D3 | `ingest` from cron, retros on demand | `ingest` is free, idempotent and must run regularly anyway; a retro costs money and reads untrusted text; a retro per session end would recurse through the same hooks | [Cron](interface.md#cron) |
| D4 | The retro session sees only a pre-built, redacted, fenced digest in a sandboxed third-party-reading session | Transcripts hold third-party text and secret-shaped strings; the built-in `audit` kind is trusted and unsandboxed; a prompt-injected retro must not reach credentials or the network | [Digest](interface.md#digest), [Retro launch](interface.md#retro-launch) |
| D5 | A closed artifact whose evidence references the tool checks; numbers come from the store | Models invent facts; a reference to an item that is not in the digest is dropped; a consumer gets one stable contract | [Retro artifact](interface.md#retro-artifact), [Retro validation](interface.md#retro-validation) |
| D6 | Headless only in 0.x, straight through `claude-session exec` | A retro is a batch job; the dependency stays one tool, shareable without tmux or worktrees; interactive discussion is self-improve's role | [Retro launch](interface.md#retro-launch), [Deviations](interface.md#deviations-from-the-design-guide) |
| D7 | Tokens exact, dollars carry a basis | Cost lines do not always reconcile with tokens; mixing figures would mislead | [Cost](interface.md#cost) |
| D8 | Join to harness records only on the exact transcript path or session id, and snapshot once final | Guessing from directory names is spoofable; harness facts vanish at `forget` | [Harness enrichment](interface.md#harness-enrichment) |
| D9 | Event-focused, budgeted digest with text only for failures | Successful tool output is where secrets concentrate and is rarely informative for a retro | [Digest](interface.md#digest) |
| D10 | No `audit.jsonl`; hash-only `log.jsonl`; hooks `pre-retro`, `post-retro`, `post-attention` | No external action is taken; a spend check before a model run is the one real veto point | [Hooks](interface.md#hooks), [Log](interface.md#log) |
| D11 | Standard config location with ownership checks, no repo layer | The threat (a trusted session editing config) is covered by the check plus the profile's write deny; a repo layer would let an environment value weaken a repo's safety | [Config](interface.md#config) |
| D12 | Sessions, agents and usage rows with one global dedupe by message id | The format writes one line per content block; copied history after a fork would double count | [Counting rules](interface.md#counting-rules) |
| D13 | Explicit retro scope, hard bounds, idempotent key | Cost is predictable; cron and repeats are safe | [Retro lifecycle](interface.md#retro-lifecycle), [Config](interface.md#config) |
| D14 | Blocking run, no resume, label-based recovery | Follows review-run's shape with less surface | [Retro lifecycle](interface.md#retro-lifecycle) |
| D15 | A hardened incremental reader | Transcripts can be unterminated, corrupt, huge or planted by a same-user process | [Ingest rules](interface.md#ingest-rules) |
| D16 | No outputs outside the store | Notes written from a session that read untrusted text would reach later trusted sessions | [Safety](interface.md#safety) |
| D17 | Retention keys with a daily prune | Rows are small; model text and digests are not | [Retention](interface.md#retention) |
| D18 | No TUI in 0.x | The read commands cover reading; a TUI is a separate review surface | [TUI](interface.md#tui) |
| D19 | Identity echoes, a marker, a closed shape and a skill preflight | The wrapper pattern for artifacts: claude-session accepts any artifact as `done`, so the tool must check | [Slot contract](interface.md#slot-contract), [Retro validation](interface.md#retro-validation) |
| D20 | The retro session's own transcripts are excluded from ingest and removed after collecting | The digest would otherwise stay in Claude Code's store and be audited in turn | [Retro launch](interface.md#retro-launch) item 6 |

## Live verification

Facts relied on, with the run that established them. Each becomes a regression test in the plan (`live` where it needs the real tool).

| Fact | Established by | Test |
|---|---|---|
| Transcripts are written per session under per-project directories with subagent files beside them; a project directory name is derived from the first-seen working directory; per-line working directories drift | Free read-only survey of a real corpus (Claude Code 2.1.x) | Synthetic corpus, parser tests |
| One API message is one line per content block (same message id); naive sums overcount about 2 times; deduping by message id (largest output) matches a run's reported usage exactly | Survey and a real run | Counting-rule unit test, `live` reconcile test |
| Timestamps are not monotonic (about a fifth of lines step backwards), so min and max are used | Survey | Synthetic corpus |
| About 30 days of transcripts are retained; resumed sessions append to the same file; compaction is a boundary line | Survey | Synthetic corpus |
| Cost appears only in dedicated cost lines and can disagree with tokens | Survey (one of five sessions far off) | `cost_mismatch` flag test |
| Corrupt lines, NUL bytes and unterminated final lines exist; a 140 MB file parses in under a second with line-by-line reading | Survey | Reader robustness tests |
| Secret-shaped strings occur in tool output | Survey (counts only) | Redaction tests |
| Transcripts record no labels or environment; a harness session is linkable only through its recorded transcript path or session id | Survey | Join tests |
| The retro `exec` call works end to end for kind `audit` with third-party-reading settings: exit 0, frozen artifact, `cost_usd`, `num_turns`, sandbox active; about 40 s and a cent | Real runs (small model, synthetic digest) | `live` retro round trip |
| `implicit_done: true` hides failures; a malformed artifact and a missing report both end `done`; a misspelled skill ends `needs-input` | Real runs | Validation tests, `live` |
| A report command containing `$CSESSION_DIR` is denied in the sandbox; a literal absolute path works | Real runs | Skill test |
| The four plugin placeholder entries are needed for sandboxed Bash; claude-session adds them in a per-run copy | Real runs | `live` |
| Injected instructions in a digest were not followed in all five runs; fake secrets never reached an artifact; the built-in `ReportFindings` tool was once called, so the tool list is restricted | Real runs | `live` injection test |
| `forget` keeps files for 30 days; `forget --purge` removes them; neither removes Claude Code's own transcript of the run | Real runs | `live` cleanup test |
| Evidence references: no invalid reference over five runs; finding counts vary between runs | Real runs | `live` |

Still to verify (conservative default until then): macOS paths and sandbox behavior; a retro of more than 20 sessions (refused by default); real-run behavior of a custom skill without a marker; how claude-session behaves when a harness session's transcript path points to a file that was rotated away.

## Modules

| Module | Responsibility |
|---|---|
| `cli` | argparse subcommands, global flags, exit-code mapping, JSON envelopes |
| `config` | YAML load with drop-ins, Pydantic validation, ownership checks, price table |
| `gateway` | the only place that spawns processes, touches the filesystem outside the store or reads the clock: `claude-session` calls, file opens, crontab |
| `reader` | safe open, incremental line reading, offset and inode bookkeeping |
| `parse` | line-type tolerant parsing into events: usage, tools, errors, denials, timing, subagent links |
| `count` | counting rules, dedupe by message id, day bucketing, cost basis |
| `store` | SQLite schema, migrations, leases, queries |
| `harness` | claude-session `list` and `status` snapshots and the matching rule |
| `digest` | item selection, budget, redaction, sanitizing, fencing, manifest |
| `retro` | lifecycle, key, launch through the gateway, validation, recovery |
| `artifact` | closed-shape validation and evidence checks |
| `hooks` | hook execution, process groups, payloads |
| `cron` | managed block read, write, dry run |
| `usage` | grouping and totals for `usage` and `export` |
| `plugin/` | the bundled skill, reference marker and permission profile resource |

## Data model

SQLite in WAL mode, additive migrations only. Tables:

- `files`: path, inode, size, offset, first-line hash, last read, status.
- `sessions`: session id, project, entrypoint, version, first and last timestamp, active seconds, counters, flags, parse and unknown-line counts.
- `agents`: agent id, parent session, parent tool-call id, type, counters.
- `usage`: day, session, agent or main, model, four token counters, per row cost basis fields.
- `messages_seen`: message id hash, row reference, winning output count. Pruned with the session.
- `harness`: csession id, session id, kind, labels, per-run outcome, end reason, cost, turns, skill, repo, captured at, final.
- `retros`: id, key, state, reason, scope, skill, model, timestamps, session id, cost, turns, outcome, again-of, lease fields, counts, dropped.
- `retro_artifacts`: retro id, artifact JSON (validated), summary.
- `meta`: schema version, ingest bookkeeping, consecutive failure count.

Untrusted text lives only in `retro_artifacts` and the stored digest files.

## Concurrency

One `ingest` at a time (a lease; a second caller exits 0). Each file is read in one transaction that writes its rows and offset together, so a crash leaves a consistent previous state. Retro leases carry the holder's PID and start time with a heartbeat. A SQLite write lock is never held across a call to claude-session or a hook. `prune` and `retro delete` take the store's write lock for one transaction.

## Error handling

A bad line, an unsafe file or an unknown shape is counted and skipped, never fatal. A source that cannot be read at all (an unusable root, an unwritable store) is exit 12 with a reason. A retro failure keeps its state and reason in the store and is reported by exit code. Every `claude-session` call is checked against its exit code and JSON envelope; an unknown newer schema fails closed (exit 12 `schema_newer`).

## Test strategy

- All process, file-outside-store, clock and crontab access go through `gateway.py`; tests substitute fakes (`tests/fakebin/` holds a fake `claude-session`).
- A synthetic transcript corpus with duplicate-block lines, corrupt, unterminated and NUL lines, backwards timestamps, subagents, compaction, resume, a fork with copied history, unknown line types and a symlink, FIFO and hard-linked file. Golden `--json` files for every command; every real output validated against `schemas/`.
- Counting, cost basis and day bucketing property tests.
- Redaction and sanitizer tests with a fuzz corpus (including forged closing fence tags).
- Retro lifecycle tests against the fake `claude-session`: every outcome code, artifact check and recovery path.
- `live` tests (never in CI): the real reader on a real directory, and one real retro round trip. Each fact in Live verification is a named test.
- `make verify` (ruff, pyright, pytest without `live`, markdown lint) must pass before every commit.

## Dependencies

`pyyaml` (required: the standard library cannot read YAML) and `pydantic` (config and artifact validation, as the design guide specifies). Nothing else: the reader, parser, store and cron block use the standard library.

## Platform

Linux and WSL are tested; macOS is expected to work and is checked locally. See [Platform notes](interface.md#platform-notes).

## Open items for implementation

- The exact secret-shaped patterns and their tests (kept as data in the package, extendable by config).
- Whether `usage --by` needs more than two dimensions (not for 0.x).
- Pins to request from claude-session for `cost_usd`, `num_turns`, `started_at`, `ended_at`, `claude_session_ids[].transcript_path` and `run.skill`.
