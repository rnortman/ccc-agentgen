# Changelog

## Unreleased

- **Deleting a TODO entry now removes its code comments too.** In the
  `todo-triage` workflow, pruning entries ruled `delete` used to leave
  their `TODO(slug)` comments behind in the code. Now a new
  `site-remover` agent removes those comments first, rewriting any part
  that still describes the code; `todo-triage prune --write` refuses to
  touch `TODO.md` while any of them remain; and the comment edits and the
  `TODO.md` change go into one commit, made only when you say so.
  Comments left behind by earlier prunes are found and removed the same
  way.
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
- `todo-triage prune` (without `--write`) now exits 3 when there is
  nothing to prune, like `next`, `ready` and `inbox`.
- `todo-triage show` prints a `ruling document:` line naming the
  document of the ruling that stands, or `(ruled by the user)` when you
  ruled it with `todo-triage override`.

## 0.1.0 — 2026-10-13

Initial public release.
