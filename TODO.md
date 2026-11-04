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
