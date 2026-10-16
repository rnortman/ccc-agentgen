# ccc-agentgen

Generates [ccc](https://github.com/rnortman/ccc) agent definitions from shared templates and
per-variant configuration, so one workflow can be emitted in several versions.

`templates/` and `variants/` are the product; `src/agentgen/` is the tool that
assembles them.

## Build a variant

```
uv run agentgen build review-chain
uv run agentgen build review-chain --out /path/to/dir
```

- A bare variant name resolves to `<root>/variants/<name>.toml`; a path is used
  as given. `--root` defaults to the current directory and must contain
  `templates/`, plus `variants/` when the variant is named rather than pathed.
- `--out` defaults to `<root>/build/<variant>/`. Every role is rendered and
  every check rule run before anything is written; a failure writes nothing.
- The output directory may be absent or empty. A build stamps it with an
  `.agentgen-build` marker; on a later build that marker is what allows the
  `*.md` in it to be cleared first, including stale harness overlays. A
  non-empty directory without the marker, or a marked directory holding
  anything but `*.md`, is an error.

Every role has a `<role>.md` definition. When a later allowed harness renders
a different body from the preferred/base render, agentgen also writes the
body-only `<role>.claude.md` or `<role>.codex.md` overlay.

## Install into ccc

Put reusable ccc preferences in the effective `<ccc-dir>/prefs/*.toml`. A
preference file holds `[agents.<type>]` tables and optionally
`default_harness`, nothing else: deploy refuses `agents_dir`, any site key
(`[claude]`, `[codex]`, `[mqtt]`, `[process]`, `[launch]`), and any unknown
top-level key. Site settings live in ccc's `<ccc-dir>/config.toml` and are
always read, whatever pair is selected. Then deploy every shipped variant and
compose the preference files:

```
uv run agentgen deploy
```

Every deploy regenerates `<ccc-dir>/configs/` from `prefs/ × variants`, so
deleted inputs do not leave stale generated configs and no preferences leaves
`configs/` empty. Each generated file is an agents config: `agents_dir` plus
the preference file's text. `prefs/`, `config.toml` and `agents.toml` are
untouched. A shell exporting a removed `CCC_AGENTS_CONFIG` names a nonexistent
file, and ccc refuses to run with an error naming `CCC_AGENTS_CONFIG`; run
`ccc-use off`, or restore preferences and redeploy before choosing a pair
again. `ccc-use` with no arguments reports the current selection and currently
existing pairs.

Source the exact `shell/ccc-env.sh` path printed by deploy from `.bashrc`. It
provides `ccc-use`, `ccc-use <variant> [prefs]`, and `ccc-use off`.
`ccc-use <variant> [prefs]` exports `CCC_AGENTS_CONFIG`; `ccc-use off` unsets
it, and ccc then reads the default `<ccc-dir>/agents.toml`. `ccc-use` never
sets `CCC_CONFIG`; when `CCC_CONFIG` names a file under `<ccc-dir>/configs/`,
which holds only agents configs, every `ccc-use` call warns and says to
`unset CCC_CONFIG`.
`[agents.<type>]` settings for a role absent from the selected variant are
harmless, while requesting a harness the definition disallows remains a runtime
ccc error.

## Layout

- `templates/roles/<role>.md.j2` — literal YAML frontmatter (ccc keys intrinsic
  to the role: `description`, `tools`, `codex_sandbox`) then a Jinja body.
- `templates/partials/*.md.j2` — shared fragments and macros the bodies include.
- `variants/<name>.toml` — `[defaults]` frontmatter for every role, `[vars]`
  knobs the templates read, and one `[roles.<id>]` table per included role.
  Frontmatter precedence, later wins: role template, `[defaults]`,
  `[roles.<id>]`. `name` is always the role id, and a role id is lowercase
  letters, digits and `-`, as ccc's agent types are.

The `harness` policy may appear at the template, `[defaults]`, or role tier.
It accepts a scalar, comma-separated string, or list; entries are ordered, so
the first is allowed and preferred and later entries are allowed fallbacks.
Whole values replace earlier tiers. A blank value means the policy is absent
at that tier. After precedence is resolved, keys for every harness excluded by
the winning policy are removed automatically, including `model`, `effort`,
`tools`, `codex_model`, `codex_effort`, and `codex_sandbox`. A more-specific
tier must override `harness` itself if it wants to allow that harness.
Template bodies receive `harness` for the current render and `harnesses` for
the effective ordered policy. Overlays are written
only when a later allowed harness's body differs from the preferred/base body.

Fourteen variants ship: eleven review chains and three TODO workflows (see
**TODO triage and burndown**). `review-chain` is the full chain with one `implementer`
role. `review-chain-lite` drops the ELI5 rendering, the review pre-pass, the
round-close scan, the explorer and the forester family, and runs cheaper
models. `review-chain-split` replaces the implementer with `planner` (chooses
the slice, writes a stand-alone plan), `coder-supervisor` (Claude-only; runs
the coder to a commit) and `coder` (Codex-only; implements the plan).
`review-chain-planner` keeps the planner but drops the supervisor: the
`implementer` lands each plan itself, sees the design, checks its own work, and
returns `replan` or `respawn` exactly as the supervisor does. It keeps the
`implementer` agent type so operator settings for `coder` apply only to the
split chain. The orchestrator renders the planned loop on `"planner" in roles`,
naming `coder-supervisor` or `implementer` as the worker on `"coder" in roles`;
without a planner it renders the self-scoping implementer loop.
`review-chain-auto`, `review-chain-split-auto` and `review-chain-planner-auto`
are those chains with the forester deciding instead of advising. In them the
plain-language rendering is not produced at the design gate — the forester
holds it — but the user can ask for one at any stop, and gets
`design-eli5-<K>.md` covering the design and every delta.

`review-chain-stack` and `review-chain-stack-auto` ship a stack of PR-sized
commits instead of one squashed commit. They are `review-chain` and
`review-chain-auto` with a `pr-assembler` in place of the `changelog-author`.
The design carries a **Stack** section — the ordered PRs, each with a
branch name, a title, the design items it delivers and an estimated size
(`[vars]` `pr_target_lines`, at most `pr_max_lines`) — reviewed and frozen
with the design and changed only by deltas, which never move, remove or
rename a PR already assembled. A round is one PR: it closes when the
implementer replies `pr-done`, and `pr_max_lines` and `max_increments` are
stops that go to triage, never closers. A fix to an earlier PR is a
`git commit --fixup` targeting that PR's branch, reviewed with the round.
After each round's review the `pr-assembler` restacks every fixup into its
PR with `rebase --autosquash`, squashes the round into the PR's one commit
with a message that is the PR's title and description, writes the PR's
changelog line and creates its branch; the orchestrator checks mechanically
that the top of the stack is the tree that was reviewed. At the ship gate
nothing is squashed and nothing is pushed, in either variant: the user
pushes the branches. The stack chains require the workflow's artifacts —
log, design, PR records — to live in a repository other than the code's,
because the assembler rewrites the code repository's history; the
orchestrator refuses to start when the working dir is inside the code
repository. The stack orchestrator creates a dedicated work branch,
`<request slug>-stack-work`, at setup and never moves the branch it was
launched on; the stack branches are the product.

`review-chain-stack-followup` and `review-chain-stack-followup-auto` run a
second cycle over a stack whose PRs are open and reviewed on the code host.
They are the stack variants plus a `pr-liaison` role, the only role that
talks to the code host: it reads every PR's review threads into
`pr-comments-<K>.md`, posts the answers the design writes, and publishes
the rewritten stack. Name the working dir of the cycle that last shipped
the stack (the directory of its `user-request.md`), a stack cycle or an
earlier follow-up, when you launch; the liaison finds the stack from its
records and refuses a stack whose local branches, remote branches
and PR heads disagree, or whose PRs are not all open. The stack's PRs are
looked for on the remote the request names (or your launch words name),
else the remote the previous follow-up published to, else `origin`; every
push of the cycle goes to that one remote.

Launching a follow-up that also names a **base** — a local branch, "onto
`<branch>`" — re-creates the stack on it: each inherited PR is rebuilt in
its own round (its shipped commit replayed, this cycle's changes added),
reviewed whole and squashed onto its existing branch; an entry the design
drops is closed on the host. Publish re-points each PR's base on the host
and never touches a PR's title or body. The base is taken at the branch's tip:
to remove a PR in place, name a branch created at the stack's current base,
the bottom branch's parent, since a branch such as `main` has usually moved
past it. That costs one round per surviving PR, and a surviving PR grown
past the size limit is re-split into new PRs. A follow-up that names no base never moves or
removes a PR. If you push by hand, run publish without a push afterwards:
it writes the record the next follow-up reads and closes the removed PRs.

The design answers
every thread in a **Responses** section and lands its code changes as
fixups into the PRs they belong to, or as new PRs on top. The liaison is
pinned to Claude (`harness: claude`): it calls the forgejo MCP server's
tools by their Claude Code names (`mcp__forgejo__forgejo_list_prs`,
`_get_pr`, `_get_review_comments`, `_get_pr_comments`, `_post_review`,
`_open_pr`, `_update_pr`, `_close_pr`), which a role reaches only when its `tools:` list names them,
and the Codex configuration carries no forgejo server. In the plain
variant the responses are posted at every design gate the review chain
cleared; your word to proceed freezes the design as it stands, and
comments posted after the last fetch wait for the next cycle. A gate
reached through a review escalation posts nothing, and your word to
proceed there posts the responses and then freezes. The plain variant never pushes on its
own: after the cycle, `force-push the stack` force-pushes every stack
branch with a lease, in one atomic push, re-targets and closes PRs as the
stack says, and opens the new PRs, and "open the PRs" opens them
without pushing, giving the lease command to run by hand for the branches
not yet pushed. In `-auto` the responses are posted once the forester
approves the design, and the forester's ship approval force-pushes the
stack with a lease, re-targets and closes PRs as the stack says, and opens
the new PRs.

## Forester family

Two product-owner-proxy roles, both pinned to the most capable models and
both working from documents, never code:

- `forester` — sits at the gates the user would hold. `[vars]`
  `forester_gates` picks which of `design`, `triage`, `scan`, `ship`;
  `forester_autonomy` is `advise` (writes a brief, the user still decides) or
  `decide` (its ruling is the gate's outcome, the user returns for its report
  at cycle end or on escalation). Directions it gives go to
  `userproxy-notes-*` files, each opening with a disclaimer that an agent, not
  the user, wrote it. `forester_gate_rounds` caps the forester↔designer loop.
  In `decide` mode a design-gate approval accepts every open question at the
  default the design states; any other answer goes down as userproxy notes
  and the design returns revised; a question without a default cannot be
  approved past. In `decide`
  mode with the `ship` gate, a forester-approved squash pushes the current
  branch to `origin` without asking; on `main` it does not, and the cycle
  ends `shipped` with the HEAD unpushed; a push on `main` is a later
  instruction, never a gate.
- `forester-commentator` — after every review wave with findings, before the
  responder, writes `commentary-<step>.md`: questions (reachability, cost to
  maintain, fit to the request), not dispositions. Tools are `Read` and
  `Write` only so it cannot reach the code.

Shared partials: `forestry` (the owner's perspective), `desk` (what is read),
`forester-log`, `forester-escalation`, `userproxy`.

Every role ends its reply with one line the orchestrator routes on:

```
[agent] status=<OK|RETRY|ESCALATE> result=<token|-> out=<abs-path|-> commit=<hash|->
```

`RETRY` is a relaunch the orchestrator performs by table lookup; `ESCALATE`
hands a document up the supervision chain. `tests/test_golden.py` holds the
closed `result` vocabulary per role.

Under Claude Code the Bash tool's background ceiling is two hours unless
`BASH_MAX_TIMEOUT_MS` raises it, and a ceiling hit kills the whole subtree.
For drivers, raise it: export `BASH_MAX_TIMEOUT_MS=86400000` in the
environment the top-level driver is started from; every `ccc` and every
harness child beneath it inherits it. Do not set it through a `--settings`
in the ccc config's `[claude] extra_args`: that replaces the `Stop` hook ccc
injects on interactive Claude launches. A launch that returns without a
reply — `ccc` exiting non-zero, or the spawner's shell tool stopping it at
its limit — is a **cut-off launch**, and the spawner resumes it itself
(`ccc resume <id> "Continue."`) before anything reaches the user: a failed
one at most twice, a ceiling hit at most four times.

## TODO triage and burndown

Two workflows over a repo's `TODO.md`, and the `todo-triage` tool they run on.
Rulings and every document behind them live in a **store** outside the code
repo, so nothing in the repo points at it and no agent adding a TODO can see or
imitate the annotations. Suggested location: a sibling directory, e.g.
`../<repo>-notes/todo-triage/`. Every command takes `--repo`
(default: cwd) and `--store` (all but `lint`), before or after the command
name; `todo-triage init --tag <tag>` creates `store.yaml`.

Install the tool where agents can reach it (ccc adds nothing to `PATH`), from
the root of your clone of this repository:

```
uv tool install .
```

That puts `todo-triage` in `~/.local/bin`. `agentgen deploy` does not install
it.

- **`todo-triage`** — `ccc-use todo-triage`, then `ccc launch
  triage-orchestrator --cwd <repo>` and name the store. For each entry nobody
  has ruled on, in file order: a `sketcher` ground-truths it against the code
  and argues for and against, a `contrarian` second-guesses the sketch, and
  the `triage-forester` rules as the product owner would — `do` with a
  priority 1–4, `defer`, `blocked`, or `delete`. The tool validates and records
  each ruling with its date; an entry whose text changes, or whose ruling ages
  past `stale_days`, comes back for re-triage. A `blocked` ruling may carry a
  priority too: the entry is then ready for burndown at that priority,
  without re-triage, once every blocker has left `TODO.md`; `todo-triage
  ready` shows, beneath each ready entry, the entries it unlocks, two levels
  deep. The session ends
  with a summary and `todo-triage prune`'s list of entries ruled `delete`
  (and approved) with their `TODO(slug)` comments. On your word, the
  `site-remover` removes every such comment, rewriting what a comment also
  said about the code where that earns its place, and stages its edits with
  a report at the path `todo-triage path --prune` prints; then `todo-triage
  prune --write` removes the entries from `TODO.md`, refusing while any of
  their comments remains. The orchestrator stages `TODO.md` and shows you
  the staged diff; one commit of both is made only on your word, with your
  message or a default. Comments left behind by entries pruned in earlier
  sessions — a slug gone from `TODO.md` whose standing ruling is an approved
  `delete` — are listed and removed the same way; `todo-triage override` to
  another disposition keeps a slug's comments where they are.
- **Review, between the two.** An agent's ruling is pending until you rule
  on it; burndown and `prune` take only approved rulings and your own.
  `todo-triage inbox` lists the pending ones. Per entry: `todo-triage
  approve <slug> [--note TEXT]` accepts the ruling as it stands; `todo-triage
  retriage <slug> --note TEXT` sends it back, and the next triage session
  takes it first and answers your note; `todo-triage override <slug>
  --disposition …` replaces it with your own ruling. A ruling over a cluster
  is approved or sent back as one, for every entry it covers. To walk the
  inbox in conversation instead, `ccc-use todo-triage`, then `ccc launch
  triage-clerk --cwd <repo>` and name the store: the `triage-clerk`
  tells you in plain prose what each pending ruling proposes and why, with a
  link to it, answers your questions from the documents, the code or an
  explorer, and runs those commands on your word.
- **`todo-burndown`**, **`todo-burndown-auto`** — `ccc-use` one, then `ccc
  launch burndown-orchestrator --cwd <repo>` with the store, the ADR root and
  any of `--priority P`, `--max-priority P`, `--slugs a,b,c`, `--count N`,
  `--until HH:MM`. The triage forester picks a ready entry, or several that
  share code — a cluster, a ready entry with the `blocked` entries it
  unlocks, or entries whose code sites overlap (`todo-triage ready`) — and
  writes
  a self-contained request; the tool opens an ADR directory for it; the review
  chain's `orchestrator` runs it as a sub-driver. Each variant embeds a review
  chain whole — `review-chain` or `review-chain-auto` — because the inner
  orchestrator resolves its roles from the same `agents_dir`. In the plain
  variant the inner chain's gates are relayed to you; in `-auto` its forester
  holds them.

Instructions to agents that create files inside the code repo (scratch
notes, observation files) are checked by the repo's own hooks like any other
file: put such files in the ADR directory, or follow the repo's convention
for local files (for example, a `*.local.md` pattern in `.gitignore`).

The ADR directory may sit in a different git repository from the code (an
ADR sidecar repo). Then the orchestrator commits there itself, pathspec-limited
to the ADR directory: the frozen set at each freeze, and the whole directory
at every stop that ends its turn and at the shipped end, so a cycle abandoned
at a stop has its record committed up to that stop. A run that ends without
reaching a stop (a crash, a usage-limit exit) has committed nothing since its
last stop; resuming it commits at the next one. No other agent commits in the
sidecar repo.

The review-chain `orchestrator` ends every stop with a status line —
`shipped` when the cycle is over, `gate` when it awaits the user — which is
what the burndown driver routes on and a human operator can ignore.

The three drivers — `orchestrator`, `burndown-orchestrator`,
`triage-orchestrator` — each carry **User directives**: the user's chat words
can tweak its workflow; nothing else can.

`TODO.md` entries describe work and nothing else; `todo-triage lint` flags
in-band status markers (`NOT NOW`, `Triaged`, `[blocked-on …]` and the like)
so they can be removed by hand before an entry is ruled.

## Checks

Rendered definitions are validated against ccc's contract before writing: the
accepted frontmatter key set, `name` equal to the file stem, a non-empty
description, a model for every allowed harness, the per-harness effort
vocabularies, the sandbox values, and body size. Body size and spawn-reference
checks apply to every emitted body, including overlays; an overlay may not
begin with a frontmatter fence. One rule is ours: every `ccc run <role>` spawn
reference in a rendered body must name a role the variant includes.

## Tests

```
uv run pytest
```

`tests/test_golden.py` builds every shipped variant and asserts against the
emitted files. When `ccc` is on `PATH` it also checks routing refusal and the
selected `prompt=` file, as well as the resolved model, effort, and sandbox,
and that `ccc which` reports `agents config:` on stderr. Without `ccc` that
test is skipped. Set
`AGENTGEN_REQUIRE_CCC=1` wherever `ccc` is expected, and the skip becomes a
failure instead. Bash switcher tests use Bash when available; set
`AGENTGEN_REQUIRE_BASH=1` to make missing Bash a failure.
