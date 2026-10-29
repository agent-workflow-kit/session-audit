# session-audit: design

Status: Seeded from awkit architecture v1.4; not yet designed.

Section references (§N) point to `../awkit-docs/architecture.md` unless marked "design guide" (`../awkit-docs/design-guide.md`).

## Role and layer

Analyze Claude session transcripts and outcomes: retros and usage analytics.

Layer 3 (workflow). Must work fully standalone; no upward awareness.

Post-v1: not part of the v1 scope (§4, §19). It may be built in parallel if there is capacity.

Skill-wrapper tool (§5.5, design guide §11). Default slot: `audit` (bundled as `session-audit:audit` in `plugin/`).

## Responsibilities

From §5.3 (`session-audit`), verbatim:

Skill-wrapper: analyze transcripts and outcomes; retros ("what went well / badly"); usage analytics. Default mode headless.

## Uses

None specified in the architecture yet; to be decided in the design session (binding: TBD (A/B/C)).

## Key flow

No flow is defined for this tool in §10 (post-v1).

## Relevant cross-cutting sections

- §3.2, §3.3: layering rules and edge bindings
- §5.5: capability slots: `audit`, kind `audit`
- §8: session model: transcripts, outcomes, retention
- §12: autonomy and escalation
- §13: security: fencing of transcript content, environment
- §14: audit logs as an input
- design guide §11: skill-wrapper pattern

## Constraints (decided)

From §20 (`session-audit, self-improve`):

none.

Packaging: The bundled plugin is shipped inside the package and located via `importlib.resources.files('session_audit') / 'plugin'`.

## Open topics

From §20 (`session-audit, self-improve`):

what is audited, when, output, how improvements are applied.

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. What is audited (transcripts, outcomes, costs, audit logs), and from which sources?
2. When does an audit run (on demand, scheduled, per session end)?
3. What is the output (format, location, consumers such as self-improve)?
4. How are improvements derived from audit findings handed off?
