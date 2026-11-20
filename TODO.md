# TODO

## `correctness-plan-out-check-recomputes-today-on-cold-resume`

In `project-orchestrator.md.j2` Loop step 2b, a `build`/`spike` reply's
`out` must sit under the cycle dir parent and basename prefix the launch
handed, and a fresh driver session rebuilds those from today's date (step 2b
hands "`DD-<project slug>--` (today's date)"). A plan-mode `ESCALATE` resumed
cold on a later day — Resume's `plan escalated` route re-enters step 2b's
stop — therefore replies with a request path carrying the plan day's prefix
and is routed as a failure. Affects `project-burndown` and
`project-burndown-auto`, and `-auto`'s respond step (Loop step 2c item 2)
when the last `plan written` line names `none` and a `done` plan becomes a
build or spike after a cold resume of a `plan review escalated: respond`
stop. The check must not depend on the day the session runs. No record line
carries the day the launch was handed: the plan and dispositions paths sit
in the project dir, dated by the project's start, and the record header has
no date. Where the check's expected parent and prefix come from, and how
it treats records written before the fix, is the fix's design. It is one
change for every step that checks an `out`, not a patch on one of the two
paths.

## `correctness-step5-guard-misses-named-cluster-mates-double-note`

A `todo-triage retriage` of a cluster mate already sent back by an earlier
`retriage` of its cluster writes and registers the same note again, so the
next triage session sees the user's note twice.

## `correctness-previous-review-drops-all-but-last-note`

A pass reviewed more than once with a note carries several review notes, but
the previous-pass summary the triage session reads reports only the last one;
the earlier notes are dropped from what the sketcher and forester see.

## `changelog-author-cannot-cut-release`

The workflow cannot ship a cycle as a release. The changelog author may not
cut a release section: step 2 of its definition ("edit entries implementers
already added") and step 4 ("write into the unreleased section, and only
that section; never touch a released one") conflict once the implementer
has already cut `## Unreleased` into `## <version> — <date>`, and no role
creates a tag. Release cycles therefore rely on the user checking
`## Unreleased` by hand and tagging by hand at the pin stop and the closing
stop. Example: the cycle in
`ccc-harness-adr/2026/10/22-publish-and-install-docs/` (design-delta-1, D1
and D2). The fix is workflow design — a changelog author that can cut a
release section, a tag made by a role at the ship gate — and needs the
user's decision on what the workflow should do, not a patch.

## `experimenter-subagent-mcp-reach`

The spike `experimenter`'s in-session subagents (the `subagents` tool set
in `templates/toolsets.toml`) likely hold every MCP server — every
connected account — of the operator's Claude session, and every built-in
tool the catalogue withholds from all roles. Basis: one run on Claude Code
2.1.329 in which a subagent of an agent restricted to `Read`, `Agent`,
`Task` listed its own tools. That is the model's self-report, so likely,
not proven. The servers it listed included Forgejo (posting, editing and
closing pull requests), Discord, Playwright's unsafe code runner, Google
Drive and Calendar, plus push notifications and cron and remote triggers
that outlast the session.

Only wording in the experimenter's definition restrains this, and wording
is no defence against prompt injection from third-party material a spike
reads.

The reach was accepted for now by the user proxy at the ship gate of the
2026-11-20 experimenter-subagents cycle, not by the owner in person.

Until it is closed, the safe way to run a spike is from a Claude session
with those servers disconnected. To withdraw the reach entirely, revert
the `subagents` grant and regenerate.

The fix is a `ccc` launch option that starts a Claude agent with no MCP
servers, and the experimenter using it.

**Done when** the `ccc` option exists and the experimenter uses it, or the
owner rules the reach acceptable.

The entry for `ccc`, not yet filed in ccc; the owner files it:

```
## `claude-no-mcp-launch`

`ccc` starts a Claude agent with every MCP server of the operator's session connected, and restricts the agent only through the `tools` list of its `--agents` entry. That list does not appear to bind in-session subagents: on Claude Code 2.1.329 a subagent started by an agent whose list was `Read`, `Agent`, `Task` reported holding every built-in tool and every tool of every connected MCP server (the model's self-report, one run, recorded by ccc-agentgen's experimenter-subagents cycle of 2026-11-20). ccc-agentgen's spike `experimenter` now runs in-session subagents, so its subagents can act on the operator's connected accounts (Forgejo, Discord, Google, remote triggers) with only prompt wording against it. Wanted: a per-agent-type way to launch Claude with no MCP servers (for example `--strict-mcp-config` with an empty config; to be verified that this also strips them from subagents), and ccc-agentgen's experimenter using it. To settle first: whether it is an agent-definition key or site config, and whether an allowlist of servers is wanted rather than all-or-nothing. Done when an experimenter session's subagent reports no MCP tools, or the owner has ruled the reach acceptable. Code site: where `src/harness/claude.rs` builds the `--agents` entry.
```

## `in-session-restatements-still-written-on-fable`

Three pieces of restatement — text that repeats what a document already
records and needs no judgement — are written by roles pinned to Fable:

- the forester's **Report**, the closing section of the last ship document
  (`templates/roles/forester.md.j2`, **The report**);
- the forester-log entries that transcribe the user's rulings, `By: user`
  (`templates/partials/forester-log.md.j2`);
- the request body the triage forester writes for a burndown unit, mostly
  carried forward from the ruling and the sketch
  (`templates/roles/triage-forester.md.j2`, **Mode: select**).

The cycle digest, the one launch whose whole product was a summary, has
its own Opus role, `cycle-digester`. These three were not moved the same
way because none is a launch: each is a page or less written inside a
session whose context a ruling already paid for, so a separate Opus launch
to write it costs more than the Fable output it saves. A cut is worth
making only if the text can be produced without a new launch — for
example mechanically, by a tool, as `todo-triage summary` already writes
the session summaries of the TODO workflows. The work is to find that
route for each of the three, or to show that none exists, and to say what
the mechanical version loses for the reader of each; that is a design.

## `lint-warns-on-documentation-examples`

`todo-triage lint` reports a "matches no entry" warning for every
`TODO(slug)` token in a tracked file that is an example rather than a
code site — for example the grammar examples in `templates/`, `README.md`
and `CHANGELOG.md`, the format, help and doc strings in
`src/todotriage/`, and the test fixtures under `tests/` — 62 warnings at
`40595d1`, so the lint cannot be read as clean without inspecting each
one, and a real orphan would be invisible among them. The same happens on
any tree whose docs show the convention (claude-usage-mqtt's `CLAUDE.md`
gives two). The only exclusion the tool has, `init --ignore SLUG` in a
store's `store.yaml`, suppresses only the other warning (an entry with no
code site); the site scan never consults it, even when `lint` is given
`--store`. The fix is a way for examples to be excluded from the
site scan — a path exclude, a rule on the token's form, or scanning comment
syntax only — and which one is the user's decision on how the tool should
read a tree.

## `auto-forester-definitions-over-their-size-budget`

The `-auto` forester and project-forester definitions grew when the user proxy
and the shared rule **What you may rule** were added, and the streamlining
that was to pay for the growth was not done. The request behind that change
floated stronger forester wording at an unchanged size, paid for by
streamlining elsewhere, as a trade it was unsure was worth making; its design
estimated under 250 words of growth each.
Rendered, `wc -w`, before → after:

- `review-chain-auto/forester.md`: 8467 → 8937 (+470)
- `review-chain-stack-auto/forester.md`: 8018 → 8486 (+468)
- `project-burndown-auto/forester.md`: 8553 → 9023 (+470)
- `project-burndown-auto/project-forester.md`: 7967 → 8643 (+676)

The work is a streamlining pass over the decide renderings of
`templates/roles/forester.md.j2` and `templates/roles/project-forester.md.j2`,
with the partials they include, that brings each rendered definition back to
at most its "before" size without weakening **What you may rule** or the
stop-answer rules. About 350 of the added words are the shared rule
(`templates/partials/proxy-authority.md.j2`), which replaced a criteria list
of about 150; the rest is stop-answer inputs, the stop-answer paragraph, the
report line, the plan's **Rulings** and the respond-mode paragraph. The room
to cut is mostly elsewhere, in the older paragraphs of the forester template,
each of which records a rule an earlier incident added — so the pass has to
weigh those rules, and say which still earn their words, not just trim prose.

## `stack-sweep-commit-is-not-a-fixup`

In the stack chains the comment sweep commits with an imperative subject, so
the pr-assembler classes it as the open PR's own commit
(`templates/roles/pr-assembler.md.j2`, step 1). A sweep that follows a
`fixup!` commit edits comments in an assembled PR's code: on a round with an
open PR those edits land in the wrong PR, and on a fixup-only round they trip
"the round has own commits and no open entry". The prepass reviewer's rule
that a fix to an assembled PR's code in the open PR's own commit is a
`scope-` finding points the same way. The work is deciding what shape a
sweep commit takes in a stack — one `fixup!` per PR whose code it touched, or
a class of its own the assembler folds by path — and teaching the
comment-rewriter, the pr-assembler and the prepass reviewer that shape.

## `todo-burndown-auto-driver-stops-reach-the-user`

In `todo-burndown-auto` the units run with the user proxy at the embedded
chain's stops, but the burndown driver's own stops still go straight to the
user: the triage forester's `ESCALATE` at selection and at a follow-up, and
the `incomplete` close (`burndown-orchestrator.md.j2`). The driver also has
no `attended` word of its own, so a unit can be made attended only at one of
its relayed stops. Giving the driver the user proxy means a reply table per
stop and a decision on whether a user proxy may skip a unit or retry an
incomplete close on the user's behalf.

## `triage-commands-read-past-lint-errors`

Only `attempt-close` and `prune --write` refuse a tracker with error-level
lint issues. Every other command that reads it (`override`, `record`,
`begin`, `summary`, `ready`, `list`) runs on what did parse, so an entry
dropped by an error such as an unparseable heading counts as gone: an
`override` stores its ruling with `entry_hash: null` and empty
`blocked_on_entries`, and `summary` writes it into the store as resolved.
A sectioned `## Open` file is already refused by every command but `lint`.
The fix is deciding which commands refuse on which errors — the read-only
status views may be worth keeping usable on a broken file — and that is
the user's call on how the tool should behave.
