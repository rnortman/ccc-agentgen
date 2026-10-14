# Changelog

## Unreleased

- **The forester's answers to open questions now reach the design.** In
  `review-chain-auto`, `review-chain-split-auto`,
  `review-chain-planner-auto` and `todo-burndown-auto`, the forester's
  answers to a design's open questions used to stay in its ruling, which no
  other agent reads, so the design froze with the questions still open.
  Now the designer states a default for each open question and writes the
  design against it; the forester's approval means the defaults stand, and
  any other answer sends the design back to the designer, who writes it in,
  before the freeze. A question with no default is answered that way or
  escalated to you. In every workflow, designers now give each open
  question a default (or say why none is defensible), and your own answers
  at a design gate are written into the design rather than left as
  questions.
- **The plain-language (ELI5) rendering is made only when someone will
  read it.** In `review-chain-auto`, `review-chain-split-auto`,
  `review-chain-planner-auto` and `todo-burndown-auto`, where the forester
  approves the design, the orchestrator no longer renders the design before
  every forester design gate. Instead you can ask for a rendering at any
  stop — a gate, an escalation, the end of the cycle or afterwards — and
  get one document covering the design and every delta. Under
  `todo-burndown-auto` the stops you see are a unit's gates and
  escalations (a shipped unit closes without stopping): at a gate, ask in
  your answer; at an escalation, ask with `resume: <request>`. In the other
  chains with a forester, a Claude orchestrator now launches the rendering
  and the forester's brief together, so the design gate stops sooner; a
  Codex orchestrator runs them one after the other, as before.
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
- **Burndown can now do a blocked entry in the same cycle as the entry
  it waits on.** In `todo-burndown`, the forester (the agent that picks
  what to work on next) now sees, beneath each ready entry, the `blocked`
  entries with a priority that would become ready once it is done — up to
  two levels deep — in a new last column of `todo-triage ready` and an
  `unlocks` field in its JSON. The forester is encouraged to take such a
  chain, or other entries that share code, as one cycle, and `todo-triage
  attempt` accepts any selection whose every blocker is either already
  resolved or part of the selection. When it rejects one, it now names the
  blockers that are missing.
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
