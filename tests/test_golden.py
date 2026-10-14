"""Golden build: the shipped variants, emitted to disk and read back.

Unlike `test_templates.py` (synthetic variants, in-memory), these tests build
through the CLI and assert against the bytes on disk — what ccc actually loads,
and the shipped variant TOMLs rather than a synthetic role set.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agentgen.check import check_all, spawn_references
from agentgen.cli import main
from agentgen.deploy import deploy
from agentgen.emit import MARKER_NAME, read_build
from agentgen.model import HARNESS_KEYS, Variant, load_variant
from conftest import DRIVERS, SPAWNING_PARTIAL_SENTINEL, role_template_ids

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"
VARIANTS = REPO_ROOT / "variants"

FULL_STEM = "review-chain"
FULL_AUTO_STEM = "review-chain-auto"
LITE_STEM = "review-chain-lite"
SPLIT_STEM = "review-chain-split"
AUTO_STEM = "review-chain-split-auto"
SPLIT_STEMS = (SPLIT_STEM, AUTO_STEM)
PLANNER_STEM = "review-chain-planner"
PLANNER_AUTO_STEM = "review-chain-planner-auto"
PLANNER_STEMS = (PLANNER_STEM, PLANNER_AUTO_STEM)
TRIAGE_STEM = "todo-triage"
BURNDOWN_STEM = "todo-burndown"
BURNDOWN_AUTO_STEM = "todo-burndown-auto"

VARIANT_STEMS = sorted(p.stem for p in VARIANTS.glob("*.toml"))
REVIEW_CHAIN_STEMS = [stem for stem in VARIANT_STEMS if stem.startswith("review-chain")]

# The drivers a burndown variant adds on top of the review chain it embeds.
BURNDOWN_EMBEDS = {BURNDOWN_STEM: FULL_STEM, BURNDOWN_AUTO_STEM: FULL_AUTO_STEM}
BURNDOWN_ROLES = {"burndown-orchestrator", "triage-forester"}

# Set where the suite runs with `ccc` available, to turn the integration test's
# skip into a failure.
REQUIRE_CCC = os.environ.get("AGENTGEN_REQUIRE_CCC") == "1"

# The prose a dropped role's stage owns, beyond its id: artifact names and phase
# words survive `{% if "<id>" in roles %}` pruning unless they are guarded too.
STAGE_VOCABULARY = {
    "eli5-explainer": ("eli5",),
    "explorer": ("explorer",),
    "prepass-reviewer": ("prepass", "pre-pass"),
    "workflow-scanner": ("workflow-scan", "round-close scan"),
    "forester": ("forester", "userproxy"),
    "forester-commentator": ("commentary",),
    "coder": ("coder",),
    "coder-supervisor": ("supervisor",),
    "comment-rewriter": ("sweep",),
}

# Every model string the shipped variants may name. ccc validates efforts but
# treats a model as an arbitrary string, so a typo would surface only at run
# time; a new model has to be added here deliberately.
KNOWN_MODELS = {
    "claude-opus-5-5[1M]",
    "claude-opus-4-6[1M]",
    "claude-sonnet-5",
    "claude-fable-5-1[1M]",
}
KNOWN_CODEX_MODELS = {"gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}

# The one reply grammar every role speaks, as rendered into its body.
GRAMMAR_LINE_RE = re.compile(r"^\[agent\] status=.* result=.* out=.* commit=.*$", re.MULTILINE)
STATUSES = ("OK", "RETRY", "ESCALATE")

# The closed `result` vocabulary, per role. Kept here, not in `src/agentgen/`:
# the generator is workflow-agnostic; this is the review chain's contract, and
# the orchestrator routes on exactly these.
RESULTS = {
    "planner": {"complete", "final", "intermediate", "dispositioned", "delta-complete", "delta-remaining"},
    "coder-supervisor": {"done", "replan", "respawn"},
    "coder": {"done"},
    "implementer": {"done", "in-progress", "responded", "delta-complete", "delta-remaining"},
    # The implementer behind a planner lands plans, so it speaks the supervisor's
    # vocabulary instead of choosing its own slices.
    "implementer/planned": {"done", "replan", "respawn"},
    "designer": {"written", "resume", "delta"},
    "design-reviewer": {"findings", "no-findings"},
    "prepass-reviewer": {"findings", "no-findings"},
    "citizen-reviewer": {"findings", "no-findings"},
    "tracer-reviewer": {"findings", "no-findings"},
    "test-reviewer": {"findings", "no-findings"},
    "judge": {"approved", "rework"},
    "workflow-scanner": {"continue"},
    "comment-rewriter": {"swept", "no-changes"},
    "eli5-explainer": {"written"},
    "explorer": {"written"},
    "changelog-author": {"written"},
    "forester": {"written", "approve", "noted", "notes", "adopt", "restore"},
    "forester-commentator": {"written"},
    "sketcher": {"written"},
    "contrarian": {"written"},
    "triage-forester": {"ruled", "selected", "nothing"},
    # Interactive: the user reads the turn, the line only keeps the grammar closed.
    "triage-clerk": {"pending", "done"},
    "site-remover": {"removed", "no-sites"},
    # A driver that can itself run under a parent driver.
    "orchestrator": {"shipped", "gate"},
}

# Backticked words in a reply section that are grammar, not `result` tokens.
GRAMMAR_WORDS = {"status", "result", "out", "commit", "tree", "round", "clean", "dirty", "close", "open"}
BACKTICKED_TOKEN_RE = re.compile(r"`([a-z][a-z-]*)`")


class Build:
    """One variant built into a directory, with its definitions read back."""

    def __init__(self, stem: str, out: Path) -> None:
        self.stem = stem
        self.out = out
        self.variant: Variant = load_variant(VARIANTS / f"{stem}.toml")
        self.roles = read_build(out)
        self.by_id = {role.id: role for role in self.roles}

    def bodies(self, role_id: str) -> list[tuple[str, str]]:
        assert role_id in self.by_id, f"{self.stem} emitted no {role_id}.md"
        role = self.by_id[role_id]
        return [(f"{role_id}.md", role.body)] + [
            (f"{role_id}.{harness}.md", body) for harness, body in sorted(role.overlays.items())
        ]


@pytest.fixture(scope="module")
def builds(tmp_path_factory) -> dict[str, Build]:
    """Every shipped variant, built once through the CLI."""
    root = tmp_path_factory.mktemp("builds")
    result: dict[str, Build] = {}
    for stem in VARIANT_STEMS:
        out = root / stem
        assert main(["build", stem, "--root", str(REPO_ROOT), "--out", str(out)]) == 0
        result[stem] = Build(stem, out)
    return result


@pytest.fixture(params=VARIANT_STEMS)
def built(request, builds) -> Build:
    return builds[request.param]


@pytest.fixture
def lite(builds) -> Build:
    return builds[LITE_STEM]


@pytest.fixture
def split(builds) -> Build:
    return builds[SPLIT_STEM]


@pytest.fixture
def dropped_roles(builds) -> list[str]:
    """Roles the shipped lite variant omits, derived from the two TOMLs."""
    dropped = sorted(
        set(builds[FULL_STEM].variant.role_ids) - set(builds[LITE_STEM].variant.role_ids)
    )
    assert dropped, "the two shipped variants name the same roles; nothing is pruned"
    return dropped


def reply_section(body: str) -> str:
    index = body.rfind("## Reply")
    assert index >= 0, "no reply section"
    return body[index:]


def test_shipped_variants_are_committed():
    assert VARIANT_STEMS == sorted(
        [
            FULL_STEM,
            FULL_AUTO_STEM,
            LITE_STEM,
            PLANNER_STEM,
            PLANNER_AUTO_STEM,
            SPLIT_STEM,
            AUTO_STEM,
            TRIAGE_STEM,
            BURNDOWN_STEM,
            BURNDOWN_AUTO_STEM,
        ]
    )


def test_deploy_matches_each_shipped_golden_build(builds, tmp_path, monkeypatch):
    config = tmp_path / "ccc"
    (config / "prefs").mkdir(parents=True)
    (config / "prefs" / "default.toml").write_text('[agents.orchestrator]\neffort = "low"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    result = deploy(REPO_ROOT)

    expected_names = {load_variant(path).name for path in VARIANTS.glob("*.toml")}
    assert {variant.name for variant, _ in result.variants} == expected_names
    for stem, built in builds.items():
        deployed = config / "agents" / built.variant.name
        assert deployed.is_dir()
        for source in built.out.iterdir():
            assert (deployed / source.name).read_bytes() == source.read_bytes()


def test_emitted_files_are_exactly_the_variants_roles(built: Build):
    base_ids = []
    overlays = []
    for path in sorted(built.out.glob("*.md")):
        if path.name.endswith((".claude.md", ".codex.md")):
            overlays.append(path)
        else:
            base_ids.append(path.stem)
    assert sorted(base_ids) == sorted(built.variant.role_ids)
    assert all(path.name.rsplit(".", 2)[0] in base_ids for path in overlays)
    marker = (built.out / MARKER_NAME).read_text(encoding="utf-8").strip()
    assert marker == built.variant.name


def test_emitted_definitions_pass_the_check_rules(built: Build):
    """Re-run ccc's contract over what was written, not over the in-memory render."""
    result = check_all(built.roles)
    assert result.errors == []
    assert result.warnings == []


def test_every_emitted_role_sandboxes_workspace_write(built: Build):
    """`read-only` would deny the artifact write every role in this chain makes."""
    for role in built.roles:
        if "codex" in role.harnesses:
            assert role.frontmatter.get("codex_sandbox") == "workspace-write", role.id
        else:
            assert not (set(HARNESS_KEYS["codex"]) & role.frontmatter.keys()), role.id


def test_emitted_models_are_known_strings(built: Build):
    """ccc accepts any non-empty model string; a typo fails only at first run."""
    for role in built.roles:
        if "model" in role.frontmatter:
            assert role.frontmatter["model"] in KNOWN_MODELS, role.id
        if "codex_model" in role.frontmatter:
            assert role.frontmatter["codex_model"] in KNOWN_CODEX_MODELS, role.id


def test_shipped_variants_together_cover_every_role_template(builds):
    shipped = set()
    for build in builds.values():
        shipped |= set(build.variant.role_ids)
    assert shipped == set(role_template_ids(TEMPLATES))


def test_lite_bodies_carry_no_trace_of_a_dropped_stage(lite: Build, dropped_roles):
    """Pruning has to take the stage's vocabulary with it, not just its id."""
    missing = [d for d in dropped_roles if d not in STAGE_VOCABULARY]
    assert not missing, f"dropped roles with no vocabulary mapped: {missing}"
    forbidden = sorted(
        {d for d in dropped_roles} | {w for d in dropped_roles for w in STAGE_VOCABULARY[d]}
    )
    for role in lite.roles:
        for filename, body in lite.bodies(role.id):
            lowered = body.lower()
            for word in forbidden:
                assert word not in lowered, f"{filename}: {word}"


def test_planner_bodies_carry_no_trace_of_the_split_workers(builds):
    """The planner chains drop split's coder and supervisor; their prose goes
    too, or the planner writes for a coder that cannot see the design.
    """
    for stem in PLANNER_STEMS:
        build = builds[stem]
        dropped = sorted(set(builds[SPLIT_STEM].variant.role_ids) - set(build.variant.role_ids))
        assert dropped == ["coder", "coder-supervisor"], stem
        forbidden = sorted({w for d in dropped for w in STAGE_VOCABULARY[d]})
        for role in build.roles:
            assert not any(w in role.frontmatter.get("description", "").lower() for w in forbidden), (
                f"{stem}/{role.id}: description"
            )
            for filename, body in build.bodies(role.id):
                lowered = body.lower()
                for word in forbidden:
                    assert word not in lowered, f"{filename}: {word}"


def test_lite_designer_spawns_nobody(lite: Build):
    """`designer_spawns_explorers = false` has to remove the spawning text itself."""
    for filename, body in lite.bodies("designer"):
        assert spawn_references(body) == set(), filename
        assert SPAWNING_PARTIAL_SENTINEL not in body, filename


# Phrases the kill guidance must carry wherever a role spawns: both commands,
# the name-based-kill prohibition, and the flag ban.
KILL_GUIDANCE_PHRASES = ("ccc kill", "ccc status", "pkill", "Never `--all`")
# Phrases that must not reach a role that spawns nothing.
KILL_VOCABULARY = ("ccc kill", "ccc status", "pkill")


def _all_bodies(builds) -> list[tuple[str, str]]:
    return [
        (f"{stem}/{filename}", body)
        for stem, build in builds.items()
        for role in build.roles
        for filename, body in build.bodies(role.id)
    ]


def test_spawning_roles_carry_kill_guidance(builds):
    spawning = [(name, body) for name, body in _all_bodies(builds) if SPAWNING_PARTIAL_SENTINEL in body]
    assert spawning, "no shipped body includes the spawning partial"
    for name, body in spawning:
        for phrase in KILL_GUIDANCE_PHRASES:
            assert phrase in body, f"{name}: {phrase}"


def test_non_spawning_roles_know_nothing_of_kill(builds):
    """The guidance would only invite use in a role that has nothing to kill."""
    for name, body in _all_bodies(builds):
        if SPAWNING_PARTIAL_SENTINEL in body:
            continue
        for phrase in KILL_VOCABULARY:
            assert phrase not in body, f"{name}: {phrase}"


def _forester_holds_design(variant: Variant, autonomy: str) -> bool:
    return (
        "forester" in variant.roles
        and variant.vars.get("forester_autonomy") == autonomy
        and "design" in variant.vars.get("forester_gates", ())
    )


def _pair_at_gate(variant: Variant) -> bool:
    """The forester's brief and the rendering both precede the user's design gate."""
    return "eli5-explainer" in variant.roles and _forester_holds_design(variant, "advise")


def _eli5_on_request_only(variant: Variant) -> bool:
    """The forester holds the design gate, so nobody reads an automatic rendering."""
    return "eli5-explainer" in variant.roles and _forester_holds_design(variant, "decide")


PAIR_CLAUDE_PHRASES = (
    "Issue both `ccc run` commands before waiting on either",
    "Nothing is routed until both launches are over",
    "except the one pair",
)
PAIR_CODEX_PHRASE = "the forester first, the rendering second"


def test_drivers_launch_one_task_at_a_time(builds):
    """A Codex driver can only wait on one `ccc` at a time; a Claude
    orchestrator may overlap the design gate's brief and rendering, which
    share nothing and commit nothing, and no other launches.
    """
    for build in builds.values():
        for driver in DRIVERS & set(build.by_id):
            for name, body in build.bodies(driver):
                assert "run in parallel" not in body, f"{build.stem}/{name}"
                assert "no trailing `&`" in body, f"{build.stem}/{name}"
                if driver == "orchestrator":
                    assert "one after the other" in body, f"{build.stem}/{name}"
                    assert "never in parallel" in body, f"{build.stem}/{name}"
        if "orchestrator" not in build.by_id:
            continue
        role = build.by_id["orchestrator"]
        pair = _pair_at_gate(build.variant)
        for harness in role.harnesses:
            flat = " ".join(role.body_for(harness).split())
            where = f"{build.stem}/orchestrator/{harness}"
            for phrase in PAIR_CLAUDE_PHRASES:
                assert (phrase in flat) == (pair and harness == "claude"), f"{where}: {phrase}"
            assert (PAIR_CODEX_PHRASE in flat) == (pair and harness != "claude"), where


def test_eli5_is_rendered_only_where_the_user_holds_the_gate(builds):
    """Where the forester holds the design gate the rendering is made only on
    the user's request; elsewhere it is made at the gate and frozen with the
    design.
    """
    variants = [build.variant for build in builds.values()]
    assert any(map(_eli5_on_request_only, variants)) and any(map(_pair_at_gate, variants))
    for build in builds.values():
        if "orchestrator" not in build.by_id:
            continue
        eli5 = "eli5-explainer" in build.variant.roles
        on_request = _eli5_on_request_only(build.variant)
        for name, body in build.bodies("orchestrator"):
            where = f"{build.stem}/{name}"
            flat = " ".join(body.split())
            assert ("`design-eli5-<K>.md`" in flat) == eli5, where
            assert ("### Rendering on request" in body) == eli5, where
            assert ("`design-eli5.md`" in flat) == (eli5 and not on_request), where
            assert ("`design-eli5-delta-<N>.md`" in flat) == (eli5 and not on_request), where
            freeze = _section(body, "### Freeze")
            assert ("`design-eli5.md`" in freeze) == (eli5 and not on_request), where
            design = _section(body, "### Design ")
            assert "eli5-explainer" not in design, where
            assert ("and its rendering with it" in flat) == (eli5 and not on_request), where
            post_cycle = _section(body, "### Post-cycle turns")
            assert ("**Rendering on request**" in post_cycle) == eli5, where
            if not eli5:
                continue
            on_demand = _section(body, "### Rendering on request")
            for phrase in ("mode `spec`", "`design-eli5-<K>.md`", "`rendering failed:", "**Stop commit**"):
                assert phrase in on_demand, f"{where}: {phrase}"


def _explainer_modes_named(orchestrator_body: str) -> set[str]:
    """Every mode the orchestrator passes in an `eli5-explainer` launch."""
    flat = " ".join(orchestrator_body.split())
    modes = set()
    for launch in re.finditer(r"`ccc run eli5-explainer`(.*?)(?:\. |$)", flat):
        modes.update(re.findall(r"mode `(\w+)`", launch.group(1)))
    return modes


def test_orchestrator_names_only_modes_the_explainer_defines(builds):
    """The explainer's mode names are a contract with the orchestrator that
    launches it; a mode it does not define would leave it guessing.
    """
    named_anywhere = set()
    for build in builds.values():
        if "eli5-explainer" not in build.by_id or "orchestrator" not in build.by_id:
            continue
        defined = set()
        for _, body in build.bodies("eli5-explainer"):
            defined.update(re.findall(r"^- \*\*(\w+)\*\* —", _raw_section(body, "## Modes"), re.M))
        for name, body in build.bodies("orchestrator"):
            named = _explainer_modes_named(body)
            named_anywhere |= named
            assert named, f"{build.stem}/{name}"
            assert named <= defined, f"{build.stem}/{name}: {named - defined}"
    assert named_anywhere == {"spec", "delta"}


def _raw_section(body: str, heading: str) -> str:
    """The text from `heading` to the next heading of the same level, lines kept."""
    level = heading.split(" ", 1)[0]
    start = body.index(heading)
    end = body.find(f"\n{level} ", start + len(heading))
    return body[start:] if end == -1 else body[start:end]


ORCHESTRATOR_FAILURE_PHRASES = (
    "### Commit failure",
    "### Launch failure",
    "commit-failure-<K>.md",
    "-k<K>",
    "-d<N>",
    "git -C <working dir>",
    "### Stop commit",
    "--allow-empty",
    "**Cut-off launch**",
    "A recovery in progress is the same launch",
)


def test_orchestrator_routes_commit_and_launch_failures(builds):
    for stem in REVIEW_CHAIN_STEMS:
        for name, body in builds[stem].bodies("orchestrator"):
            for phrase in ORCHESTRATOR_FAILURE_PHRASES:
                assert phrase in body, f"{stem}/{name}: {phrase}"


def test_burndown_driver_keeps_no_abandoned_dir_list(builds):
    """The orchestrator commits its working dir at every stop; nothing is left
    for the driver to hand back to the operator.
    """
    for stem in BURNDOWN_EMBEDS:
        for name, body in builds[stem].bodies("burndown-orchestrator"):
            assert "abandoned ADR dirs" not in body, f"{stem}/{name}"


def test_rejected_stop_commit_reaches_the_burndown_operator(builds):
    """The orchestrator reports a rejected stop commit above its status line;
    the burndown driver's ESCALATE route relays those lines verbatim.
    """
    for stem, embedded in BURNDOWN_EMBEDS.items():
        for name, body in builds[embedded].bodies("orchestrator"):
            assert "`artifact commit rejected:" in body, f"{embedded}/{name}"
        for name, body in builds[stem].bodies("burndown-orchestrator"):
            flat = " ".join(body.split())
            route = flat[flat.index("- `ESCALATE` → STOP") :]
            route = route[: route.index("A missing status line")]
            assert "surface the lines it wrote above its status line, verbatim" in route, (
                f"{stem}/{name}"
            )
            assert "at an orchestrator gate or `ESCALATE` stop" in flat, f"{stem}/{name}"
            assert "The one exception" not in flat, f"{stem}/{name}"


def _section(body: str, heading: str) -> str:
    """The whitespace-normalised text from `heading` to the next `### `."""
    flat = " ".join(body.split())
    start = flat.index(heading)
    end = flat.find("### ", start + len(heading))
    return flat[start:] if end == -1 else flat[start:end]


def test_stop_commit_procedure(builds):
    """The stop commit is guarded, pathspec-limited, unstages on rejection,
    runs at every stop, and splits its rejection route at the shipped end.
    """
    for stem in REVIEW_CHAIN_STEMS:
        for name, body in builds[stem].bodies("orchestrator"):
            where = f"{stem}/{name}"
            stop = _section(body, "### Stop commit")
            assert "add -- . " in stop, where
            assert "diff --cached --quiet -- . " in stop, where
            assert "commit -m " in stop and ' -- . ; }' in stop, where
            assert " -a " not in stop, where
            assert "reset -q -- ." in stop, where
            assert "every gate, every `ESCALATE` stop, and the shipped end" in stop, where
            assert "at the shipped end → **Commit failure**" in stop, where
            assert "It changes neither" in stop, where
            failure = _section(body, "### Commit failure")
            assert "enters this procedure only at the shipped end" in failure, where
            assert "reset -q -- <the same pathspec>" in failure, where
            assert "**Stop commit** first" in body, where


PUSH_GATE_PHRASES = ("push offer", "push gate", "offer to push", "On `main`, never push")


def test_push_is_never_a_gate(builds):
    """The cycle ends at the squash: a push is automatic or the user's later
    word, never a stop the orchestrator waits on.
    """
    for stem in REVIEW_CHAIN_STEMS:
        for name, body in builds[stem].bodies("orchestrator"):
            where = f"{stem}/{name}"
            flat = " ".join(body.split())
            for phrase in PUSH_GATE_PHRASES:
                assert phrase not in flat, f"{where}: {phrase}"
            assert "### Post-cycle turns" in body, where
            assert "never push on your own" in flat, where
            assert "whether or not HEAD is pushed" in " ".join(reply_section(body).split()), where
            assert "never as a request or an offer" in flat, where
            for fact in ("`<branch> at <HEAD>, pushed to origin`", "`<branch> at <HEAD>, unpushed`"):
                assert fact in flat, f"{where}: {fact}"
            post_cycle = _section(body, "### Post-cycle turns")
            assert "the same `shipped` status line" in post_cycle, where
            assert "No turn after the shipped end reopens the cycle" in post_cycle, where


def _autopush(variant: Variant) -> bool:
    return (
        "forester" in variant.roles
        and variant.vars.get("forester_autonomy") == "decide"
        and "ship" in variant.vars.get("forester_gates", ())
    )


def test_autopush_pushes_only_off_main(builds):
    """Only a forester-decided ship gate pushes without asking, and never on `main`."""
    for stem in REVIEW_CHAIN_STEMS:
        autopush = _autopush(builds[stem].variant)
        for name, body in builds[stem].bodies("orchestrator"):
            where = f"{stem}/{name}"
            ship = _section(body, "### Ship gate")
            chain = " ".join(body[: body.index("## ")].split())
            assert ("push it to `origin` without asking" in ship) == autopush, where
            assert ("on a branch other than `main`" in ship) == autopush, where
            assert ("→ push" in chain) == autopush, where


def test_ship_gate_steps_are_numbered_in_order(builds):
    for stem in REVIEW_CHAIN_STEMS:
        for name, body in builds[stem].bodies("orchestrator"):
            start = body.index("### Ship gate")
            gate = body[start : body.index("## Procedures", start)]
            steps = [int(n) for n in re.findall(r"^(\d+)\. ", gate, re.MULTILINE)]
            assert steps == list(range(steps[0], steps[0] + len(steps))), f"{stem}/{name}: {steps}"
            assert steps[0] in (0, 1) and gate.rstrip().splitlines()[-1].strip().endswith(
                "The cycle is over."
            ), f"{stem}/{name}"


DIRECTIVES_HEADING = "## User directives"
DIRECTIVES_PROVENANCE = "legitimate by where it came from"


def test_drivers_carry_user_directives(builds):
    """Only the interactive drivers take steering from the user's chat words,
    and each states the provenance rule that keeps injected text inert.
    """
    for stem, build in builds.items():
        for role in build.roles:
            for name, body in build.bodies(role.id):
                where = f"{stem}/{name}"
                flat = " ".join(body.split())
                if role.id in DRIVERS:
                    assert DIRECTIVES_HEADING in body, where
                    assert DIRECTIVES_PROVENANCE in flat, where
                else:
                    assert DIRECTIVES_HEADING not in body, where
                    assert DIRECTIVES_PROVENANCE not in flat, where


def test_drivers_never_lists_bind_only_the_driver(builds):
    for stem in REVIEW_CHAIN_STEMS:
        for name, body in builds[stem].bodies("orchestrator"):
            never = _section(body, "## Never")
            assert "binds you, not the user" in never, f"{stem}/{name}"
            assert "**What a directive is not**" in never, f"{stem}/{name}"
            assert "**Destructive git**" in never, f"{stem}/{name}"
    for stem in BURNDOWN_EMBEDS:
        for name, body in builds[stem].bodies("burndown-orchestrator"):
            never = _section(body, "### Never")
            assert "binds you, not the user" in never, f"{stem}/{name}"
            assert "**Destructive git**" in never, f"{stem}/{name}"


def test_burndown_routes_a_directive_as_the_named_result(builds):
    """The user's "treat that as `shipped`" is routed by the driver itself,
    without resuming the orchestrator to make it say so.
    """
    for stem in BURNDOWN_EMBEDS:
        for name, body in builds[stem].bodies("burndown-orchestrator"):
            flat = " ".join(body.split())
            step = flat[flat.index("4. `ccc run orchestrator") :]
            step = step[: step.index("### Never")]
            routes = step.index("- `shipped` →")
            assert "At any stop below" in step[:routes], f"{stem}/{name}"
            assert step.count("as if the status line had read so") == 1, f"{stem}/{name}"
            assert step.index("as if the status line had read so") < routes, f"{stem}/{name}"


def test_spawning_roles_know_the_limit_exit_code(builds):
    for name, body in _all_bodies(builds):
        if SPAWNING_PARTIAL_SENTINEL in body:
            assert "4 the subagent hit a usage limit" in body, name


CUT_OFF_PHRASES = (
    "## Cut-off launch",
    "ccc status",
    "whose `started` is after you issued this launch",
    "`session=-` → there is no session",
    "for any reason other than its time limit",
    '"Continue."',
    "never under one hour",
    "long-running mode",
    "not a cut-off launch",
    "An **unrecovered launch** is",
    "At most **two** failures per launch",
    "At most **four** ceiling hits",
    "neither count ever resets",
)
CUT_OFF_RETIRED_PHRASES = (
    "hours rather than minutes",
    "exist for one situation",
    "Command timed out",
    "Ceiling hits do not count",
)
CUT_OFF_CLAUDE_PHRASES = ("run_in_background", "7200000")
CUT_OFF_CODEX_PHRASE = "timeout parameter at its maximum"


def test_spawning_roles_recover_a_cut_off_launch(builds):
    for name, body in _all_bodies(builds):
        if SPAWNING_PARTIAL_SENTINEL not in body:
            continue
        flat = " ".join(body.split())
        for phrase in CUT_OFF_PHRASES:
            assert phrase in flat, f"{name}: {phrase}"
        for phrase in CUT_OFF_RETIRED_PHRASES:
            assert phrase not in flat, f"{name}: {phrase}"


def test_spawning_roles_name_their_harness_ceiling(builds):
    """Each harness's body names its own long-running mode and nothing of the other's."""
    for stem, build in builds.items():
        for role in build.roles:
            for harness in role.harnesses:
                body = role.body_for(harness)
                if SPAWNING_PARTIAL_SENTINEL not in body:
                    continue
                flat = " ".join(body.split())
                where = f"{stem}/{role.id}/{harness}"
                if harness == "claude":
                    for phrase in CUT_OFF_CLAUDE_PHRASES:
                        assert phrase in flat, f"{where}: {phrase}"
                else:
                    assert CUT_OFF_CODEX_PHRASE in flat, where
                    for phrase in CUT_OFF_CLAUDE_PHRASES:
                        assert phrase not in flat, f"{where}: {phrase}"


CUT_OFF_ROUTES = (
    # (role, slice start, slice end, recovery pointer, the stop it must precede)
    (
        "orchestrator",
        "A missing or unparsable line",
        "**`commit=` is advisory",
        "recover it (**Cut-off launch**)",
        "**Launch failure**",
    ),
    (
        "burndown-orchestrator",
        "2. `todo-triage path --selection`",
        "3. `todo-triage attempt",
        "A cut-off launch → **Cut-off launch**",
        "STOP",
    ),
    (
        "burndown-orchestrator",
        "4. `ccc run orchestrator",
        "### Never",
        "A cut-off launch → **Cut-off launch**",
        "STOP",
    ),
    (
        "triage-orchestrator",
        "A cut-off launch is not that",
        "7. Passes completed",
        "→ **Cut-off launch**",
        "STOP",
    ),
    (
        "triage-orchestrator",
        "A cut-off launch → **Cut-off launch**; an unrecovered",
        "4. `todo-triage prune --write`",
        "→ **Cut-off launch**",
        "STOP",
    ),
    (
        "triage-clerk",
        "One explorer per question",
        "says drop it",
        "A cut-off explorer: **Cut-off launch**",
        "end turn",
    ),
)


def test_cut_off_routes_recover_before_stopping(builds):
    """Each of a role's cut-off routes points at recovery before its stop."""
    for stem, build in builds.items():
        for role_id, start, end, pointer, stop in CUT_OFF_ROUTES:
            if role_id not in build.by_id:
                continue
            for name, body in build.bodies(role_id):
                flat = " ".join(body.split())
                route = flat[flat.index(start) :]
                route = route[: route.index(end)]
                where = f"{stem}/{name}: {start}"
                assert pointer in route, where
                assert route.index(pointer) < route.index(stop), where
                assert "nrecovered launch" in route, where


def test_burndown_resumes_its_own_orchestrator_not_a_grandchild(builds):
    for stem in BURNDOWN_EMBEDS:
        for name, body in builds[stem].bodies("burndown-orchestrator"):
            flat = " ".join(body.split())
            assert "never one of the orchestrator's own subagents" in flat, f"{stem}/{name}"


def test_one_shot_roles_restate_their_status_when_resumed(builds):
    for name, body in _all_bodies(builds):
        if "One-shot, single pass." in body:
            assert "resumed with `Continue.`" in " ".join(body.split()), name


def test_triage_designer_changes_nothing_in_the_tree(builds):
    for stem in REVIEW_CHAIN_STEMS:
        for name, body in builds[stem].bodies("designer"):
            triage = body[body.index("## Mode: triage"):]
            assert "You change nothing in the repository" in triage, f"{stem}/{name}"


def test_split_variant_swaps_the_implementer_for_the_trio(builds):
    trio = {"planner", "coder", "coder-supervisor"}
    for stem in REVIEW_CHAIN_STEMS:
        build = builds[stem]
        emitted = {role.id for role in build.roles}
        if stem in SPLIT_STEMS:
            assert trio <= emitted and "implementer" not in emitted
        elif stem in PLANNER_STEMS:
            assert {"planner", "implementer"} <= emitted
            assert not ({"coder", "coder-supervisor"} & emitted)
        else:
            assert "implementer" in emitted and not (trio & emitted)


def test_orchestrator_spawns_only_the_variants_increment_roles(builds):
    """The three increment loops are alternatives; a body must render exactly one."""
    for stem in REVIEW_CHAIN_STEMS:
        build = builds[stem]
        for filename, body in build.bodies("orchestrator"):
            spawns = spawn_references(body)
            if stem in SPLIT_STEMS:
                assert {"planner", "coder-supervisor"} <= spawns, filename
                assert "implementer" not in spawns, filename
            elif stem in PLANNER_STEMS:
                assert {"planner", "implementer"} <= spawns, filename
                assert not ({"coder-supervisor", "coder"} & spawns), filename
            else:
                assert "implementer" in spawns, filename
                assert not ({"planner", "coder-supervisor", "coder"} & spawns), filename
    for filename, body in builds[SPLIT_STEM].bodies("coder-supervisor"):
        assert spawn_references(body) == {"coder"}, filename
    for stem in PLANNER_STEMS:
        for filename, body in builds[stem].bodies("implementer"):
            assert spawn_references(body) == set(), filename


def test_every_role_speaks_the_one_reply_grammar(built: Build):
    """One grammar line per body; the scripting goal erodes one role at a time.

    The orchestrator carries two: its subagents' in `## Replies`, its own in
    `## Reply`.
    """
    for role in built.roles:
        expected = 2 if role.id == "orchestrator" else 1
        for filename, body in built.bodies(role.id):
            lines = GRAMMAR_LINE_RE.findall(body)
            assert len(lines) == expected, f"{filename}: {len(lines)} grammar lines"
            for status in STATUSES:
                assert status in lines[0], f"{filename}: {status} missing from grammar line"


def test_result_vocabulary_is_closed(built: Build):
    """A role inventing a token the orchestrator does not route is the failure
    the grammar exists to prevent; the orchestrator, conversely, may route only
    tokens some role emits.
    """
    emitted: dict[str, set[str]] = {}
    for role in built.roles:
        if role.id in DRIVERS and role.id not in RESULTS:
            continue
        assert role.id in RESULTS, f"{role.id} has no result vocabulary listed"
        emitted[role.id] = set()
        for filename, body in built.bodies(role.id):
            found = set(BACKTICKED_TOKEN_RE.findall(reply_section(body))) - GRAMMAR_WORDS
            key = role.id
            if role.id == "implementer" and "planner" in built.by_id:
                key = "implementer/planned"
            assert found == RESULTS[key], filename
            emitted[role.id] |= found
    drivers = [role.id for role in built.roles if role.id in DRIVERS]
    assert drivers, f"{built.stem} has no driver"
    for driver in drivers:
        routed = set().union(*(tokens for rid, tokens in emitted.items() if rid != driver))
        for filename, body in built.bodies(driver):
            table = body[body.index("## Replies") : body.index("## Flow")]
            assert set(BACKTICKED_TOKEN_RE.findall(table)) - GRAMMAR_WORDS <= routed, filename


def test_triage_variant_carries_the_clerk(builds):
    """The clerk summarises and investigates: it links documents instead of
    dumping them, spawns explorers for questions the code needs, and names
    their reports so a later pass's `log --scan` ignores them.
    """
    build = builds[TRIAGE_STEM]
    assert set(build.by_id) == {
        "triage-orchestrator",
        "sketcher",
        "contrarian",
        "triage-forester",
        "explorer",
        "triage-clerk",
        "site-remover",
    }
    for filename, body in build.bodies("triage-clerk"):
        assert SPAWNING_PARTIAL_SENTINEL in body, filename
        assert spawn_references(body) == {"explorer"}, filename
        assert "todo-triage approve" in body and "todo-triage retriage" in body, filename
        assert "review-exploration-" in body, filename
        assert "verbatim in full" not in body, filename
        assert "summarise nothing" not in body, filename
        assert "never paste a document into the conversation" in body, filename
        assert "**Link**" in body and "**Summary**" in body, filename
        assert "has read none of the documents" in body, filename
        for deleted in ("**Header**", "**Entry**", "**Links**"):
            assert deleted not in body, (filename, deleted)
        assert "ccc resume" in body, filename
        assert "more investigation than" not in body, filename
        assert "`explorations`" in body and "`previous`" in body, filename
        assert "`triage` → `passes`" not in body, filename
        assert "rejected" not in body, filename
        assert "In this order" not in body, filename


def test_triage_clerk_explains_the_forester_priority_scale(builds):
    """The clerk tells the user what the forester's priority number means;
    a retuned rank in the forester must show up in the clerk's explanation.
    The forester's P1 qualifier is criteria for ruling, not part of the rank
    word the clerk gives the user.
    """
    build = builds[TRIAGE_STEM]
    for forester_name, forester in build.bodies("triage-forester"):
        scale = re.search(r"`priority`: (.*?)\. Priority is not deferral", " ".join(forester.split()))
        assert scale, f"{forester_name}: forester priority scale not found"
        ranks = re.findall(r"(\d) ([a-z ]+?)(?: \([^)]*\))?(?:, |$)", scale.group(1))
        assert [n for n, _ in ranks] == ["1", "2", "3", "4"], (forester_name, ranks)
        for filename, body in build.bodies("triage-clerk"):
            flat = " ".join(body.split())
            for n, meaning in ranks:
                assert f"P{n} {meaning}" in flat, (forester_name, filename, n, meaning)


def test_blocked_priority_is_explained_to_forester_and_clerk(builds):
    """The forester may give `blocked` a priority and the clerk states it as
    `P<n>`; one without the other leaves the user approving a number nobody
    showed them, or never offered.
    """
    build = builds[TRIAGE_STEM]
    for filename, body in build.bodies("triage-forester"):
        flat = " ".join(body.split())
        assert "`priority` (same 1–4 scale) is optional" in flat, filename
        assert "# iff do; optional for blocked" in body, filename
    for filename, body in build.bodies("triage-clerk"):
        flat = " ".join(body.split())
        assert "the entry goes to burndown at `P<n>` with no further triage" in flat, filename
        assert "blocked on … [P<n>]" in flat, filename


def test_forester_select_mode_offers_chains_and_combined_units(builds):
    """The forester reads `unlocks`, knows the closure rule `attempt` enforces,
    and is told to combine entries that share ground into one cycle.
    """
    for stem in (BURNDOWN_STEM, BURNDOWN_AUTO_STEM):
        for filename, body in builds[stem].bodies("triage-forester"):
            flat = " ".join(body.split())
            assert "`attempts` and `unlocks`" in flat, filename
            assert ("every `blocked_on` item of every follower must be resolved or in the"
                    " selection") in flat, filename
            assert "overlapping `site_dirs`" in flat, filename


def test_burndown_orchestrator_defers_to_ready(builds):
    """Which entries burndown may take is `todo-triage ready`'s rule; the
    driver names the tool, never a disposition.
    """
    for stem in (BURNDOWN_STEM, BURNDOWN_AUTO_STEM):
        build = builds[stem]
        assert "`do`" not in build.by_id["burndown-orchestrator"].frontmatter["description"]
        for filename, body in build.bodies("burndown-orchestrator"):
            flat = " ".join(body.split())
            assert "`todo-triage ready`" in flat, filename
            assert "ruled `do`" not in flat, filename


def test_triage_clerk_frontmatter_pins(builds):
    """Opus at low effort is the user's choice for the clerk; a later variant
    edit would otherwise undo it silently.
    """
    clerk = builds[TRIAGE_STEM].by_id["triage-clerk"].frontmatter
    assert clerk["model"] == "claude-opus-5-5[1M]"
    assert clerk["effort"] == "low"
    assert "Grep" in clerk["tools"]
    assert "Write" not in clerk["tools"]


def test_site_remover_frontmatter_pins(builds):
    """The remover reads code and judges what a comment means: pinned like the sketcher."""
    remover = builds[TRIAGE_STEM].by_id["site-remover"].frontmatter
    assert remover["model"] == "claude-fable-5-1[1M]"
    assert remover["codex_model"] == "gpt-6-sol"


def _in_order(text: str, *phrases: str) -> bool:
    positions = [text.find(p) for p in phrases]
    return -1 not in positions and positions == sorted(positions)


def test_triage_orchestrator_prunes_sites_before_todo_md(builds):
    """The comments go first, the tool's `--write` checks they are gone, and the
    one commit of both waits for the user."""
    for filename, body in builds[TRIAGE_STEM].bodies("triage-orchestrator"):
        flat = " ".join(body.split())
        prune = _section(body, "### Prune")
        assert _in_order(prune, "`git diff --cached --quiet`", "`ccc run site-remover`",
                         "`todo-triage prune --write`", "`git commit"), filename
        assert "the commit waits for the user's word" in prune, filename
        assert "code sites it lists still need removing" not in flat, filename


def test_triage_orchestrator_may_relay_git_output(builds):
    """The Prune section relays git output; the write exception has to allow it."""
    for filename, body in builds[TRIAGE_STEM].bodies("triage-orchestrator"):
        flat = " ".join(body.split())
        boundary = flat[flat.index("## Information boundary") : flat.index("## Flow")]
        assert "of `todo-triage` and of the git commands" in boundary, filename


def test_site_remover_never_commits(builds):
    build = builds[TRIAGE_STEM]
    role = build.by_id["site-remover"]
    assert {"Edit", "Bash"} <= set(role.frontmatter["tools"])
    for filename, body in build.bodies("site-remover"):
        flat = " ".join(body.split())
        for phrase in ("todo-triage sites", "git add --", "git diff --quiet --",
                       "never a commit", "ruling document: (ruled by the user)",
                       "ruled_by: user"):
            assert phrase in flat, (filename, phrase)
        assert "`git commit" not in flat, filename
        assert spawn_references(body) == set(), filename


def test_burndown_variants_embed_the_review_chain(builds):
    """The inner orchestrator resolves its roles from the burndown variant's
    `agents_dir`, so that variant must carry the whole chain, unchanged.
    """
    for stem, chain_stem in BURNDOWN_EMBEDS.items():
        build, chain = builds[stem], builds[chain_stem]
        assert set(build.by_id) - BURNDOWN_ROLES == set(chain.by_id), stem
        assert BURNDOWN_ROLES <= set(build.by_id), stem
        for role_id, role in chain.by_id.items():
            assert build.by_id[role_id].frontmatter == role.frontmatter, f"{stem}/{role_id}"
            assert build.bodies(role_id) == chain.bodies(role_id), f"{stem}/{role_id}"


def test_split_harness_pinning_holds(split: Build):
    """Codex-only coder, Claude-only supervisor able to spawn: each a decision a
    later variant edit would otherwise undo silently.
    """
    coder = split.by_id["coder"].frontmatter
    assert coder["harness"] == ["codex"]
    assert coder["codex_model"] == "gpt-6-luna"
    assert coder["codex_effort"] == "medium"
    assert not (set(HARNESS_KEYS["claude"]) & coder.keys())
    supervisor = split.by_id["coder-supervisor"].frontmatter
    assert supervisor["harness"] == ["claude"]
    assert supervisor["model"] == "claude-opus-5-5[1M]"
    assert supervisor["effort"] == "medium"
    assert "Bash" in supervisor["tools"]
    assert not (set(HARNESS_KEYS["codex"]) & supervisor.keys())
    for role in split.roles:
        if role.id not in {"coder", "coder-supervisor"}:
            assert "harness" not in role.frontmatter


def test_planner_variant_implementer_runs_on_both_harnesses(builds):
    """Opus preferred, Codex sol as the fallback when Claude usage runs out."""
    for stem in PLANNER_STEMS:
        implementer = builds[stem].by_id["implementer"].frontmatter
        assert "harness" not in implementer, stem
        assert implementer["model"] == "claude-opus-5-5[1M]", stem
        assert implementer["codex_model"] == "gpt-6-sol", stem
        assert "Bash" in implementer["tools"], stem


def _ccc_which(
    role_id: str, env: dict[str, str], harness: str | None = None
) -> subprocess.CompletedProcess:
    command = ["ccc", "which", role_id]
    if harness is not None:
        command.extend(["--harness", harness])
    return subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )


def _assert_current_ccc(result: subprocess.CompletedProcess, role_id: str) -> None:
    # A current ccc reports its agents config on stderr before it
    # loads any definition; an older ccc ignores CCC_AGENTS_CONFIG entirely.
    if not any(line.startswith("agents config: ") for line in result.stderr.splitlines()):
        pytest.fail(
            f"{role_id}: installed ccc does not print `agents config:` on stderr; "
            f"reinstall ccc from its repository\nccc which stderr:\n{result.stderr}"
        )


@pytest.mark.skipif(
    shutil.which("ccc") is None and not REQUIRE_CCC,
    reason="ccc is not on PATH (set AGENTGEN_REQUIRE_CCC=1 to require it)",
)
def test_ccc_resolves_every_emitted_type(built: Build, tmp_path_factory):
    """`which` spawns no agent, so an agents config with only `agents_dir` suffices.

    A type pinned to one harness must be refused by the other, and the selected
    prompt must follow the base/overlay policy.
    """
    assert shutil.which("ccc"), "AGENTGEN_REQUIRE_CCC=1 but ccc is not on PATH"
    config = tmp_path_factory.mktemp("ccc-config") / "agents.toml"
    quoted = str(built.out).replace("\\", "\\\\").replace('"', '\\"')
    config.write_text(f'agents_dir = "{quoted}"\n', encoding="utf-8")
    # `CCC_*` overrides outrank the agent definition at resolve time, so an
    # exported one in the developer's shell would mask or invent a failure here.
    env = {k: v for k, v in os.environ.items() if not k.startswith("CCC_")}
    env["CCC_AGENTS_CONFIG"] = str(config)

    for role in built.roles:
        done = _ccc_which(role.id, env, "codex")
        _assert_current_ccc(done, role.id)
        if "codex" not in role.harnesses:
            assert done.returncode == 2, role.id
            assert "rejects harness codex" in done.stderr, role.id
            assert "allowed harnesses: claude" in done.stderr, role.id
        else:
            assert done.returncode == 0, f"{role.id}: {done.stderr.strip()}"
            fields = dict(pair.split("=", 1) for pair in done.stdout.splitlines()[0].split())
            assert fields["agent"] == role.id
            assert fields["harness"] == "codex"
            assert fields["model"] == role.frontmatter["codex_model"], role.id
            assert fields["effort"] == role.frontmatter["codex_effort"], role.id
            assert fields["sandbox"] == role.frontmatter["codex_sandbox"], role.id
            prompt = f"{role.id}.codex.md" if "codex" in role.overlays else f"{role.id}.md"
            assert fields["prompt"] == prompt, role.id

        if role.harnesses == ["codex"]:
            done = _ccc_which(role.id, env)
            _assert_current_ccc(done, role.id)
            assert done.returncode == 0, f"{role.id}: {done.stderr.strip()}"
            fields = dict(pair.split("=", 1) for pair in done.stdout.splitlines()[0].split())
            assert fields["harness"] == "codex", role.id

        if "claude" not in role.harnesses:
            done = _ccc_which(role.id, env, "claude")
            _assert_current_ccc(done, role.id)
            assert done.returncode == 2, role.id
            assert "rejects harness claude" in done.stderr, role.id
            assert "allowed harnesses: codex" in done.stderr, role.id
