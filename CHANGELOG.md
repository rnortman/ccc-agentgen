# Changelog

## Unreleased

- **Blocked TODO entries no longer need re-triage once unblocked.** In the
  `todo-triage` workflow, a `blocked` ruling (an entry waiting on another
  entry) can now carry a priority, P1–P4. When every entry it waits on has
  left `TODO.md`, it is ready for burndown at that priority straight away,
  instead of going stale and back through triage. Without a priority,
  nothing changes. A ruling with a priority must name only other `TODO.md`
  entries as its blockers, not free-text dependencies.
- If a blocker is deleted rather than done, the waiting entry still comes
  back for re-triage, but only once the delete has been approved. A delete
  still awaiting approval no longer triggers it. This applies to all
  `blocked` rulings.
- `todo-triage ready` now shows each entry's blockers, and `todo-triage
  summary` has a new "Unblocked" section.

## 0.1.0 — 2026-10-13

Initial public release.
