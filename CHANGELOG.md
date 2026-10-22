# Changelog

## Unreleased

- **A cycle can change several repositories, and only the ones you bring
  into scope.** Name extra repositories with `in scope:`, `also in scope:`
  or `scope:` and a path, at launch or at any stop; `drop from scope:
  <path>` removes one. No agent writes, commits or files a TODO outside the
  scope; a change needed in another repository you own is never worked
  around but brought to you with a paste-ready `TODO.md` entry, and you
  choose to bring the repository in, file the entry, or stop. Designs carry
  a **Scope** section naming each repository's changes and, where one pins
  another, either a local override developed in this cycle or a follow-up
  cycle the closing stop names. The ship gate squashes each repository in
  pin order and stops after a pinned dependency for you to ship it and give
  its reference; the implementer then repoints the pin and the dependent
  ships. Codex subagents need `writable_roots` (or `--add-dir`) covering
  the repositories you may bring in; see **Operator configuration** in the
  README. The stack chains stay single-repository.
- **The forester no longer passes a gate by writing a concern into its
  report.** Anything you must know or do before a cycle ships has to reach
  you as notes or an escalation; at the ship gate the forester now reads the
  implementation log and holds the gate on any workaround, dropped item or
  cross-repository need with no other record. Its report names any item it
  let through with only the report as its record.
- **Burndown stops when a unit ships with its entries still in `TODO.md`,
  and offers a follow-up.** `todo-triage attempt-close` now prints a
  second line, `still in TODO.md: …`, naming the unit's entries that
  remain, records them on the attempt as `remaining`, and lists them in the
  session summary; `--dry-run` prints what it would record and changes
  nothing. The burndown driver no longer excludes an `incomplete` unit
  silently: it stops with the tool's output and takes `retry`, `skip` or
  `stop`. `retry` has the triage forester select a follow-up on the
  remaining entries; the forester reads the prior attempt's report and
  design, and asks the new cycle for the remainder only, or escalates when
  the remainder is work a cycle cannot do, such as a change in another
  repository. At every gate it relays the driver also shows the dry-run
  close, so a ship gate that would leave entries behind says so before you
  approve the squash.
- **Agents now treat the request as fallible input and push back on it.**
  Every request file says who wrote it on its first line: requests written
  by the burndown selector say so in a new uniform wording, a request you
  give in chat is transcribed under a line marking it as your own words, and
  a file you place by hand carries no line. Every agent that reads workflow
  documents is told to weigh each one by its author, to attribute an
  agent's text to that agent rather than to you, and to write down where it
  disagrees — even with your own words. The designer now opens every design
  with a **Request assessment**: who wrote the request, which of its
  premises hold, and what it asks that the cycle cannot ship or the project
  should not build. It drops or replaces such an ask with the reason, or
  puts it to you as a **No default** question; when no honest design exists
  (a false premise, an impossible ask, a harmful one) it stops on that
  question instead of inventing a design. The design reviewer checks the
  assessment, and the forester rules on what it dropped.
- **The request file is now `request.md`, not `user-request.md`.** Launch
  a cycle with `… request.md`; `todo-triage attempt` writes `request.md`.
  An orchestrator launched with only a path to a file of any other name
  stops at setup with one line naming the required name. **Cycles in
  flight across this upgrade** (every `review-chain*` cycle paused at a
  gate or stop, and every ADR dir `todo-triage summary` lists under Open
  attempts) run the new orchestrator text when resumed. Before resuming or
  relaunching one: if its artifact repo has no freeze commit for
  `user-request.md` yet, rename the file to `request.md`; if it is already
  frozen, leave it alone, since a rename would read as a change to the
  frozen set. A frozen burndown attempt whose session cannot be resumed
  cannot be relaunched under the old name; close it with `skip` and let a
  later burndown select the unit again.
- Agents that write and judge requests and designs are now told what a
  cycle ships — one squashed commit, or one commit per PR in a stack
  workflow — and burndown selections no longer ask for commit counts or
  layouts.
- **Entries you ruled yourself can be sent back for re-triage.**
  `todo-triage retriage <slug> --note …` now works on a ruling made with
  `override` (it used to refuse, and the only way back was another
  `override`); the `triage-orchestrator` runs it for the entries you name
  when you launch it, with your message as the note, and takes them first. A named entry
  already sent back is skipped with a one-line notice; one whose ruling
  still awaits your approval is shown to you and the session stops until
  you approve it or send it back.
- **New `review-chain-stack-followup` and `review-chain-stack-followup-auto`
  workflows answer review comments on a stack of open PRs.** Point one at
  the working dir of the cycle that last shipped the PRs, a stack cycle or
  an earlier follow-up. It reads every PR's review threads from Forgejo, designs an answer to each together with
  the code changes they call for, and posts the answers on the PRs. The
  changes land in the PRs they belong to, or in new PRs on top. Nothing is
  pushed unless you say `force-push the stack`, which force-pushes every
  branch with a lease and opens the new PRs; "open the PRs" opens them
  without pushing. The `-auto` variant posts the answers and publishes the
  stack on the forester's approval. Comments posted after you tell the
  workflow to proceed wait for the next cycle. The PRs need not live on
  `origin`: name the remote in the request (or when launching) and the
  workflow reads and publishes there; a follow-up of a follow-up keeps the
  previous cycle's remote. Requires the forgejo MCP server in the Claude
  account the workflow runs under.
- **A stack follow-up can rebuild its stack on a new base and drop PRs.**
  Name a local branch when launching ("onto `<branch>`") and every PR is
  rebuilt on top of it, each reviewed again, keeping its existing branch. A PR
  the design drops is closed on Forgejo at publish, and the remaining PRs'
  base branches are updated to match; PR titles and descriptions are never
  edited. To remove a PR without moving the stack, name a branch at the
  stack's current base. A follow-up that names no base moves and drops
  nothing, but its publish now also fixes any PR whose base on Forgejo is not
  the PR below it in the stack.
- **New `review-chain-stack` and `review-chain-stack-auto` workflows ship a
  stack of reviewable PRs instead of one squashed commit.** The design
  splits the work into PRs of about 1,000 added lines (2,000 at most; both
  configurable), each with its branch name and title. Each PR is built and
  deep-reviewed on its own, then becomes one commit on its own branch,
  stacked on the previous PR's, with a commit message you can use as the
  PR's title and description and its own changelog line. A defect found
  later in an earlier PR is fixed in that PR, so every PR reads as if it was
  right the first time. Nothing is pushed: you post the branches when the
  cycle ends. The workflow's documents must live in a separate repository
  from your code; it refuses to start otherwise.
- **Stack workflows no longer build on the branch you launched them from.**
  `review-chain-stack` and `review-chain-stack-auto` used to move whatever
  branch was checked out — `main` included — to the top of the stack. They
  now create their own `<request>-stack-work` branch at the start and leave
  yours where it was. If a branch of that name is left over from an earlier
  run, the workflow stops before starting; delete it and relaunch. The work
  branch is left in place at the end; delete it once the stack is pushed.
- **Stack workflows recover from a late design change without stopping, and
  write PR descriptions for people.** In `review-chain-stack` and
  `review-chain-stack-auto`, a design change made after a PR's review but
  before it is committed now goes back into that PR: it is built, the new
  commits are reviewed, and then the PR is assembled. Before, the workflow
  tried to assemble a PR that was missing part of its spec, refused, and
  stopped for you. Each PR's commit message is now a title and one or two
  paragraphs saying what it does and why, with no review guide, deviation
  list or TODO catalogue. The designer also runs each PR's done commands
  before writing them, so a broken build shows up at the design gate
  instead of partway through implementation.
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
