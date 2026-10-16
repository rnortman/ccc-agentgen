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
from conftest import (
    DEFAULT_CHECK,
    DEFAULTS_RULE,
    DRIVERS,
    OWNER_NOTES_LABEL,
    SPAWNING_PARTIAL_SENTINEL,
    role_template_ids,
)

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
STACK_STEM = "review-chain-stack"
STACK_AUTO_STEM = "review-chain-stack-auto"
FOLLOWUP_STEM = "review-chain-stack-followup"
FOLLOWUP_AUTO_STEM = "review-chain-stack-followup-auto"
FOLLOWUP_STEMS = (FOLLOWUP_STEM, FOLLOWUP_AUTO_STEM)
STACK_STEMS = (STACK_STEM, STACK_AUTO_STEM) + FOLLOWUP_STEMS
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
    "pr-assembler": ("pr-assembler", "assembl", "stack", "pr-done", "pr-r<", "fixup", "restack", "review base"),
    "pr-liaison": ("liaison", "pr-comments", "pr-posted", "pr-publish", "followup"),
}

# The stack's open-PR rule, one wording shared by the designer and the
# design-reviewer's delta check.
OPEN_PR_RULE = "remove it only when the delta's triggering document is an **Empty round** triage"

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
    # The implementer of a stack variant also closes a PR short of the design.
    "implementer/stack": {"done", "pr-done", "in-progress", "responded", "delta-complete", "delta-remaining"},
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
    "pr-assembler": {"assembled", "restacked"},
    "pr-liaison": {"new-comments", "no-new-comments", "posted", "published", "unpushed"},
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
GRAMMAR_WORDS = {
    "status", "result", "out", "commit", "tree", "round", "clean", "dirty", "close", "open", "branch", "changelog", "size",
    "stack",
}
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
            STACK_STEM,
            STACK_AUTO_STEM,
            FOLLOWUP_STEM,
            FOLLOWUP_AUTO_STEM,
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


def _assert_no_vocabulary(build: Build, forbidden) -> None:
    """No role of `build` carries a `forbidden` word, lower-cased, in its
    description or any rendered body.
    """
    for role in build.roles:
        description = role.frontmatter.get("description", "").lower()
        for word in forbidden:
            assert word not in description, f"{build.stem}/{role.id}: description: {word}"
        for filename, body in build.bodies(role.id):
            lowered = body.lower()
            for word in forbidden:
                assert word not in lowered, f"{build.stem}/{filename}: {word}"


def test_lite_bodies_carry_no_trace_of_a_dropped_stage(lite: Build, dropped_roles):
    """Pruning has to take the stage's vocabulary with it, not just its id."""
    missing = [d for d in dropped_roles if d not in STAGE_VOCABULARY]
    assert not missing, f"dropped roles with no vocabulary mapped: {missing}"
    forbidden = sorted(
        {d for d in dropped_roles} | {w for d in dropped_roles for w in STAGE_VOCABULARY[d]}
    )
    _assert_no_vocabulary(lite, forbidden)


def test_planner_bodies_carry_no_trace_of_the_split_workers(builds):
    """The planner chains drop split's coder and supervisor; their prose goes
    too, or the planner writes for a coder that cannot see the design.
    """
    for stem in PLANNER_STEMS:
        build = builds[stem]
        dropped = sorted(set(builds[SPLIT_STEM].variant.role_ids) - set(build.variant.role_ids))
        assert dropped == ["coder", "coder-supervisor"], stem
        _assert_no_vocabulary(build, sorted({w for d in dropped for w in STAGE_VOCABULARY[d]}))


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
        assert "a polling loop of your own" in " ".join(body.split()), name


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


def test_forester_approval_binds_open_questions_to_defaults(builds):
    """A deciding forester's ruling reaches nobody, so its approval may only
    accept the design's defaults; an advising forester's brief gains neither
    that rule nor the userproxy channel.
    """
    variants = [build.variant for build in builds.values()]
    assert any(_forester_holds_design(v, "decide") for v in variants)
    assert any(_forester_holds_design(v, "advise") for v in variants)
    for build in builds.values():
        if "forester" not in build.by_id:
            continue
        autonomy = build.variant.vars.get("forester_autonomy")
        for name, body in build.bodies("forester"):
            where = f"{build.stem}/{name}"
            flat = " ".join(body.split())
            assert (DEFAULTS_RULE in flat) == (autonomy == "decide"), where
            if autonomy == "advise":
                assert "userproxy" not in flat, where


def test_owner_overrules_frozen_search_and_false_rationale_rules(builds):
    """An owner's overrule binds like an answer; a delta's overrides come from
    searching the frozen set; a false rationale under a standing decision is
    logged, not escalated.
    """
    phrases = {
        "designer": ("an owner's note that overrules a decision binds the same way", "found by searching the frozen set"),
        "implementer": ("A frozen document's rationale that proves false",),
    }
    for build in builds.values():
        for role, wanted in phrases.items():
            if role not in build.by_id:
                continue
            for name, body in build.bodies(role):
                flat = " ".join(body.split())
                for phrase in wanted:
                    assert phrase in flat, f"{build.stem}/{name}: {phrase}"


def test_designer_open_questions_state_a_default(builds):
    """Every designer states defaults and closes questions only on notes the
    launch prompt names as the owner's.
    """
    for build in builds.values():
        if "designer" in build.by_id:
            for name, body in build.bodies("designer"):
                where = f"{build.stem}/{name}"
                assert "**Default**" in body, where
                assert "**No default**" in body, where
                flat = " ".join(body.split())
                assert OWNER_NOTES_LABEL in flat, where
                assert "userproxy" not in flat, where
                assert "notes-design-" not in flat, where
        if "design-reviewer" in build.by_id:
            for name, body in build.bodies("design-reviewer"):
                assert DEFAULT_CHECK in " ".join(body.split()), f"{build.stem}/{name}"


def test_orchestrator_names_owner_notes_at_designer_respond(builds):
    """User notes are labelled the owner's in every orchestrator; the
    forester's userproxy notes are, exactly where it decides the design gate.
    """
    userproxy_to_designer = f"userproxy notes as its notes path, named {OWNER_NOTES_LABEL}"
    variants = [build.variant for build in builds.values()]
    assert any(_forester_holds_design(v, "decide") for v in variants)
    assert any(not _forester_holds_design(v, "decide") for v in variants)
    for build in builds.values():
        if "orchestrator" not in build.by_id:
            continue
        decides = _forester_holds_design(build.variant, "decide")
        for name, body in build.bodies("orchestrator"):
            where = f"{build.stem}/{name}"
            flat = " ".join(body.split())
            assert f"the user-notes path named {OWNER_NOTES_LABEL}" in flat, where
            assert (userproxy_to_designer in flat) == decides, where
            proxy = " or userproxy notes path" if decides else ""
            review_chain = f"user notes path{proxy} among them is handed {OWNER_NOTES_LABEL}"
            assert review_chain in flat, where


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
            if stem in STACK_STEMS:
                assert "`work branch <branch> at <HEAD>; stack: <b1>@<c1> … <bn>@<cn>`" in flat, where
                assert "pushed to origin" not in flat, where
                assert "`<branch> at <HEAD>, unpushed`" not in flat, where
            else:
                for fact in (
                    "`<branch> at <HEAD>, pushed to origin`",
                    "`<branch> at <HEAD>, unpushed`",
                ):
                    assert fact in flat, f"{where}: {fact}"
            post_cycle = _section(body, "### Post-cycle turns")
            assert "the same `shipped` status line" in post_cycle, where
            assert "No turn after the shipped end reopens the cycle" in post_cycle, where


def test_non_stack_orchestrator_keeps_the_checked_out_branch(builds):
    """Without the assembler the checked-out branch is the deliverable."""
    for stem in REVIEW_CHAIN_STEMS:
        if stem in STACK_STEMS:
            continue
        for name, body in builds[stem].bodies("orchestrator"):
            where = f"{stem}/{name}"
            flat = " ".join(body.split())
            assert "which branch to work on" in flat, where
            assert "`git symbolic-ref --short HEAD` at setup" in flat, where
            assert "that branch is now **branch**" in flat, where
            assert "a detached HEAD at setup" in flat, where
            assert "a branch HEAD was found on stays where it is" not in flat, where


def _autopush(variant: Variant) -> bool:
    return (
        "forester" in variant.roles
        and variant.vars.get("forester_autonomy") == "decide"
        and "ship" in variant.vars.get("forester_gates", ())
        and "pr-assembler" not in variant.roles
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
            elif stem in STACK_STEMS:
                assert {"pr-assembler", "implementer"} <= spawns, filename
                assert not ({"planner", "coder-supervisor", "coder"} & spawns), filename
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
            elif role.id == "implementer" and "pr-assembler" in built.by_id:
                key = "implementer/stack"
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


def _stack_builds(builds) -> list[Build]:
    stacks = [build for build in builds.values() if "pr-assembler" in build.variant.roles]
    assert sorted(build.stem for build in stacks) == sorted(STACK_STEMS)
    return stacks


def test_stack_vocabulary_is_absent_elsewhere(builds):
    """A variant without the assembler keeps no stack; none of the stack's
    words may leak into it, in a body or a description.
    """
    forbidden = STAGE_VOCABULARY["pr-assembler"]
    for build in builds.values():
        if "pr-assembler" in build.variant.roles:
            continue
        _assert_no_vocabulary(build, forbidden)


def test_followup_vocabulary_is_absent_elsewhere(builds):
    """A variant without the pr-liaison never talks to the code host; none of
    the follow-up cycle's words may leak into it.
    """
    followups = [build.stem for build in builds.values() if "pr-liaison" in build.variant.roles]
    assert sorted(followups) == sorted(FOLLOWUP_STEMS)
    forbidden = STAGE_VOCABULARY["pr-liaison"]
    for build in builds.values():
        if "pr-liaison" in build.variant.roles:
            continue
        _assert_no_vocabulary(build, forbidden)


FORGEJO_TOOLS = {
    f"mcp__forgejo__forgejo_{name}"
    for name in ("list_prs", "get_pr", "get_review_comments", "get_pr_comments", "post_review", "open_pr")
}


def test_followup_liaison_frontmatter_pins(builds):
    """The liaison is the one role with code-host tools, and Claude-only: the
    tools are Claude Code MCP names, and the Codex config carries no server.
    """
    for stem in FOLLOWUP_STEMS:
        liaison = builds[stem].by_id["pr-liaison"]
        front = liaison.frontmatter
        assert front["harness"] == ["claude"], stem
        assert liaison.overlays == {} or set(liaison.overlays) <= {"claude"}, stem
        assert FORGEJO_TOOLS <= set(front["tools"]), stem
        assert "Edit" not in front["tools"], stem
        assert not (set(HARNESS_KEYS["codex"]) & front.keys()), stem
    for build in builds.values():
        if "pr-liaison" in build.by_id:
            continue
        for role in build.roles:
            tools = role.frontmatter.get("tools", [])
            assert not any(tool.startswith("mcp__") for tool in tools), f"{build.stem}/{role.id}"


def test_followup_liaison_reads_any_prior_cycle_and_pushes_atomically(builds):
    """A follow-up of a follow-up keeps the whole stack and its own replies:
    fetch takes the inherited branches from the prior dir's latest comments
    header and carries the `ours:` review ids forward. Fetch settles the
    remote and records it; publish moves every due branch to that recorded
    remote in one atomic push, and the by-hand block is that command.
    """
    for stem in FOLLOWUP_STEMS:
        for name, body in builds[stem].bodies("pr-liaison"):
            where = f"{stem}/{name}"
            flat = " ".join(body.split())
            host = flat[flat.index("## The code host") : flat.index("## Mode: fetch")]
            fetch = flat[flat.index("## Mode: fetch") : flat.index("## Mode: post")]
            publish = flat[flat.index("## Mode: publish") : flat.index("## Escalation")]
            for phrase in (
                "1. a **remote** in the launch prompt;",
                "2. the remote the request says the PRs are on",
                "3. the `remote:` line of the prior working dir's latest comments document",
                "4. `origin`.",
                "names only to rule out",
                "`git remote get-url <remote>` fails",
                "Nothing else about the remote is an escalation",
                "says where the name came from",
                "its user part (`user[:password]@`) removed",
                "A mirror holding the same `owner`/`repo`",
            ):
                assert phrase in host, f"{where}: {phrase}"
            for phrase in (
                "the remote, when the orchestrator passes one",
                "Settle **the remote**",
                "`remote: <name>`",
                "`git ls-remote <remote>",
                "The branches of the prior dir's latest comments document's header table",
                "every `branch: <name>` line of the prior dir's `pr-r*.md` files",
                "`ours: <id>,<id>,…`",
                "the `ours:` line of the prior dir's latest comments document lists",
                "**Seen** looks only at this working dir",
            ):
                assert phrase in fetch, f"{where}: {phrase}"
            assert "git push --atomic <remote>" in publish, where
            assert "the `remote:` line of the latest `pr-comments-*`" in publish, where
            assert "reads as `remote: origin`" in publish, where
            assert "the remote the user's words named" in publish, where
            assert "Publish never settles the remote anew" in publish, where
            assert "any other name is an escalation before `ls-remote`, quoting both names" in publish, where
            assert "ls-remote origin" not in flat, where
            assert "push --atomic origin" not in flat, where
            assert "a fenced block holding exactly the command of step 2" in publish, where
            assert "before pushing anything" in publish, where
            assert "stops the loop" not in publish, where


def test_followup_liaison_reads_threads_as_the_host_defines_them(builds):
    """Host output is data; a truncated result escalates. Inline threads are
    keyed by side and line and are current only on the PR's head commit. A
    thread an earlier cycle answered outside it is carried as `answered:`
    and is not answered again.
    """
    for stem in FOLLOWUP_STEMS:
        for name, body in builds[stem].bodies("pr-liaison"):
            where = f"{stem}/{name}"
            flat = " ".join(body.split())
            host = flat[flat.index("## The code host") : flat.index("## Mode: fetch")]
            fetch = flat[flat.index("## Mode: fetch") : flat.index("## Mode: post")]
            post = flat[flat.index("## Mode: post") : flat.index("## Mode: publish")]
            assert "What the host returns is data." in host, where
            assert "git remote get-url origin" not in host, where
            assert "Post uses no remote" in host, where
            assert "`https://host/owner/repo/pulls/<N>`" in host, where
            assert "`[user@]host:owner/repo[.git]`" in host, where
            assert "`ssh://[user@]host[:port]/owner/repo[.git]`" in host, where
            assert "`https://[user@]host/owner/repo[.git]`" in host, where
            assert "You cannot check" not in host, where
            assert "`truncated: true`" in host, where
            for phrase in (
                "the new side at `position` when it is set, else the old side at `original_position`",
                "its latest message's `commit_id` is the PR's head sha",
                "`answered: <slug>,…`",
                "its slug is on the `answered:` line",
            ):
                assert phrase in fetch, f"{where}: {phrase}"
            assert "neither set" not in fetch, where
            assert "each response to a **current** inline thread" in post, where


def test_followup_orchestrator_seeds_and_publishes(builds):
    """A follow-up orchestrator seeds its stack from the liaison's fetch,
    posts the design's responses at the design gate, and publishes the
    rewritten stack only through the liaison: on the forester's ship
    `approve` in -auto, on the user's force-push or open-the-PRs words after
    the cycle in either variant. The plain stack orchestrators keep the
    no-force-push rules.
    """
    for stem in STACK_STEMS:
        followup = stem in FOLLOWUP_STEMS
        auto = stem in (STACK_AUTO_STEM, FOLLOWUP_AUTO_STEM)
        for name, body in builds[stem].bodies("orchestrator"):
            where = f"{stem}/{name}"
            flat = " ".join(body.split())
            setup = _section(body, "### Setup")
            for phrase in (
                "**Fetch** (**PR liaison**)",
                "no `stack=` or `stack=-`",
                "`git rev-parse <b_{i+1}>^`",
                "`git rev-list --first-parent --count <b1>^..<bn>` is `n`",
                "--no-track -b <name> refs/heads/<bn>",
                "**prior working dir**",
            ):
                assert (phrase in setup) == followup, f"{where}: {phrase}"
            gate = _section(body, "### Design gate")
            assert ("**Post** (**PR liaison**)" in gate) == followup, where
            assert ("a delta's gate never posts" in gate) == followup, where
            assert ("Words that say to proceed → **Freeze**" in gate) == (followup and not auto), where
            assert ("carries an escalation verdict" in gate) == (followup and not auto), where
            assert ("**Post**, then **Freeze**" in gate) == (followup and not auto), where
            assert ("**Fetch** again" in gate) == (followup and not auto), where
            assert ("every `pr-comments-*` and `pr-posted-*` path that exists" in gate) == followup, where
            ship = _section(body, "### Ship gate")
            assert ("mode publish" in ship) == (followup and auto), where
            assert ("**push** `yes`" in ship) == (followup and auto), where
            assert ("rewritten, unpushed" in ship) == (followup and not auto), where
            assert ("Never force-push" in ship) == (not followup), where
            post_cycle = _section(body, "### Post-cycle turns")
            assert ("`force-push the stack`" in post_cycle) == followup, where
            assert ("optionally `to <remote>`" in post_cycle) == followup, where
            if followup:
                assert "to origin" not in post_cycle, where
            assert ("**push** `no`" in post_cycle) == followup, where
            assert ("A push instruction naming the repository and branch" in post_cycle) == (not followup), where
            never = _section(body, "## Never")
            assert ("Only the pr-liaison's **Publish** pushes" in never) == followup, where
            assert ("Force-push ever." in never) == (not followup), where
            assert ("or the forester's ship `approve`" in never) == (followup and auto), where
            liaison = _section(body, "### PR liaison") if followup else ""
            for phrase in ("mode fetch", "mode post", "mode publish", "`exception-liaison-<K>.md`"):
                assert (phrase in liaison) == followup, f"{where}: {phrase}"
            if followup:
                fetch_line = liaison[liaison.index("**Fetch** — mode fetch") : liaison.index("**Post** — mode post")]
                publish_line = liaison[liaison.index("**Publish** — mode publish") :]
                post_line = liaison[liaison.index("**Post** — mode post") : liaison.index("**Publish** — mode publish")]
                assert "+ the remote, when recorded" in fetch_line, where
                assert "Post carries" not in fetch_line, where
                assert "takes each PR's repository from the comments documents" in post_line, where
                assert "the remote the user's words named" in publish_line, where
                assert "rewritten, unpushed; commands in <publish path>" in liaison, where
                assert "publish escalated, what reached the remote in <exception path>" in liaison, where
                assert "follows each launch before the shipped end" in liaison, where
                assert "tracked files have uncommitted changes" in setup, where
                assert "recorded when they do, never asked for" in setup, where
                assert "A request written from chat records none" in setup, where
                assert ("words to retry run **Fetch** again" in gate) == (not auto), where
                assert "**Relaunch naming** does not apply" in liaison, where
            replies = flat.split("## Replies")[1].split("## Flow")[0]
            assert ("| pr-liaison | fetch: `new-comments` · `no-new-comments`" in replies) == followup, where
            state = _section(body, "## State you track")
            assert ("| **prior working dir** |" in state) == followup, where
            assert ("| **remote** |" in state) == followup, where
            assert ("liaison launch ordinal" in state) == followup, where
            chain = flat.split("## Information boundary")[0]
            assert ("request → fetch → design" in chain) == followup, where
            assert ("ship gate → publish" in chain) == (followup and auto), where


def test_stack_orchestrator_keeps_the_stack(builds):
    """The orchestrator verifies the assembler's rewrite and never squashes,
    rebases or pushes itself.
    """
    for build in _stack_builds(builds):
        for name, body in build.bodies("orchestrator"):
            where = f"{build.stem}/{name}"
            flat = " ".join(body.split())
            assert "### Assembly — no gate" in body, where
            assert "### Assembly check" in body, where
            assert "### Empty round" in body, where
            for phrase in (
                "`git diff --name-only <pre-assembly HEAD> HEAD`",
                "--first-parent",
                "--invert-grep",
                "branch=",
                "restacked",
                "stall mark",
                "; stack: <b1>@<c1>",
                "--git-path rebase-merge",
                "git checkout -f -B <branch> <pre-assembly HEAD>",
                "git for-each-ref --format='%(refname:short)' refs/heads",
                "absent from the recorded names",
                "`size=`",
                "`<request slug>-stack-work`",
            ):
                assert phrase in flat, f"{where}: {phrase}"
            if "Nothing is surfaced; the forester's `approve`" not in flat:
                assert "<N> added lines (<pr-r path>)" in flat, where
            assert "paths under the working dir" not in flat, where
            setup = _section(body, "### Setup")
            for phrase in (
                "outside the repo — a standard that places it inside → ask the user",
                "The working dir is inside the repo — its `realpath` lies under `git rev-parse --show-toplevel`",
                "→ STOP, say so and why, end turn: a stack workflow keeps its artifacts in a sidecar repository",
            ):
                assert phrase in setup, f"{where}: {phrase}"
            cut_off = _section(body, "### Launch failure")
            for phrase in ("git worktree list", "git worktree remove --force"):
                assert phrase in cut_off, f"{where}: {phrase}"
            for phrase in ("`PR <branch> assembled` entry", "by hand"):
                assert phrase not in cut_off, f"{where}: {phrase}"
            check = _section(body, "### Assembly check")
            assert "run this check again in place of **HEAD check** step 2" in check, where
            assert "no longer describes the repository" not in check, where
            payload = flat.split("## Standard payload")[1].split("## Replies")[0]
            assert "the stack — branch and commit per assembled PR" in payload, where
            assert "or a relaunch the user directs after the ruling" in _section(body, "### Assembly — no gate"), where
            naming = _section(body, "## Artifacts and their names")
            assert "relaunch of the pr-assembler the user directs" in naming, where
            ship = _section(body, "### Ship gate")
            assert "**Artifact commit** — the **Stop commit** of the shipped end:" in ship, where
            assert "artifact repo is not the repo" not in ship, where
            assert "### Intermediate squash" not in body, where
            assert "squash to **original base**" not in flat, where
            assert "folding every round into one" not in flat, where
            assert "changelog-author" not in flat, where
            never = _section(body, "## Never")
            for word in ("Squash", "rebase", "push"):
                assert word in never, f"{where}: {word}"
            assert "A failed squash" not in _section(body, "### Commit failure"), where
            assert "REBASE_HEAD" not in flat, where
            assert "`pr-r<R>.md` names the branch" not in flat, where
            naming = _section(body, "## Artifacts and their names")
            for entry in ("`pr-r<R>.md`", "`exception-assembly-r<R>.md`"):
                assert entry in naming, f"{where}: {entry}"
            assert "A reopened round's review and assembly artifacts take the same suffix" in naming, where
            implementation = _section(body, "### Implementation")
            assert "`done` or `pr-done` with **reopen** set → **Reopened round**" in implementation, where
            landing = _section(body, "#### Where a delta lands")
            for phrase in (
                "**After the review passed, before assembly**",
                "**Reopened round**, either way: set **reopen**",
                "stall mark = `I`",
                "`exception-assembly-r<R>-k<K>.md`",
                "HEAD = the **reopen** HEAD → nothing is reviewed again",
            ):
                assert phrase in landing, f"{where}: {phrase}"
            assert "`K` += 1, that `K` is the reopen's" in landing, where
            for removed in ("through a triage", "`K` that triage's"):
                assert removed not in landing, f"{where}: {removed}"
            for phrase in ("the scanner had not replied →", "nothing is reviewed again; round state → *finished*"):
                assert phrase in landing, f"{where}: {phrase}"
            assert "reopened by `delta-remaining`" not in landing, where
            for phrase in ("`K` the fresh one the reopen took", "round reopens"):
                assert phrase in naming, f"{where}: {phrase}"
            state = _section(body, "## State you track")
            for phrase in (
                "`K` += 1 at the reopen",
                "/ reopen",
                "no value is taken twice",
                "the round-close scanner had replied",
                "at the deep review's pass",
                "a reopened round's close",
            ):
                assert phrase in state, f"{where}: {phrase}"
            round_review = _section(body, "### Round review")
            for phrase in ("Every launch here carries the **review base**", "On the deep review's pass"):
                assert phrase in round_review, f"{where}: {phrase}"
            assembly = _section(body, "### Assembly — no gate")
            assert "round base = review base = HEAD; **reopen** cleared" in assembly, where
            assert "round base = review base = current HEAD" in _section(body, "### Ship gate"), where
            for phrase in (
                "review base",
                "reopened",
                "review a round twice from the same base",
                "review base, HEAD, reviewed HEAD",
            ):
                assert phrase in flat, f"{where}: {phrase}"
            for removed in (
                "fresh round at current HEAD without assembling",
                "Nothing reopens a round",
                "At the round-close scan",
                "One case is not empty",
                "still assembled as its PR",
            ):
                assert removed not in flat, f"{where}: {removed}"


def test_non_stack_orchestrator_keeps_round_landing_rules(builds):
    """Outside the stack a round is a checkpoint: it never reopens and its
    review never runs twice.
    """
    for build in builds.values():
        if "pr-assembler" in build.variant.roles or "orchestrator" not in build.by_id:
            continue
        for name, body in build.bodies("orchestrator"):
            where = f"{build.stem}/{name}"
            flat = " ".join(body.split())
            for phrase in (
                "Nothing reopens a round",
                "A round's review never runs twice",
                "round base, HEAD, reviewed HEAD",
            ):
                assert phrase in flat, f"{where}: {phrase}"
            for absent in ("/ reopen", "round reopens", "deep review's pass", "scanner had replied"):
                assert absent not in flat, f"{where}: {absent}"


def test_every_orchestrator_takes_no_ordinal_twice(builds):
    """`N` and `K` stay unique in every orchestrator, stack or not."""
    for build in builds.values():
        if "orchestrator" not in build.by_id:
            continue
        for name, body in build.bodies("orchestrator"):
            state = _section(body, "## State you track")
            assert "no value is taken twice" in state, f"{build.stem}/{name}"


def test_stack_assembler_keeps_history_safe(builds):
    """The assembler checks before it writes, never moves a ref it does not
    own, restores everything on an escalation, and reports what it measured.
    """
    for build in _stack_builds(builds):
        for name, body in build.bodies("pr-assembler"):
            where = f"{build.stem}/{name}"
            flat = " ".join(body.split())
            identify = flat.split("2. **Restack**")[0]
            for phrase in (
                "git status --porcelain --untracked-files=no",
                "git rev-parse -q --verify refs/heads/<name>",
                "before you write anything",
                "not in the stack the orchestrator passed",
            ):
                assert phrase in identify, f"{where}: {phrase}"
            assert "no `PR <branch> assembled` entry" not in identify, where
            for phrase in (
                "GIT_SEQUENCE_EDITOR=true git -c core.commentChar=';' rebase -i --autosquash",
                "git diff --quiet <pre-assembly HEAD> HEAD",
                "`git branch <name> HEAD` — never `-f`",
                "git rebase --abort",
                "git worktree remove --force <path>",
                "git checkout -f -B <working branch> <pre-assembly HEAD>",
                "git branch -f <b_i> <recorded c_i>",
                "git branch -D <name>",
                "Never push",
                f"past {build.variant.vars['pr_max_lines']} stays whole",
                "size=<sizes|->",
            ):
                assert phrase.lower() in flat.lower(), f"{where}: {phrase}"
            log_entry = flat.split("5. **Compose the log entry**")[1].split("6. **Squash**")[0]
            assert "the last write before the reply" in log_entry, where
            assert "No escalation writes it" in log_entry, where
            for removed in ("log-only", "--amend", "inside the repo", "same-repo", "**fold**", "or the fold", "withdraw"):
                assert removed not in flat, f"{where}: {removed}"
            assert re.search(r"\bsed\b", flat) is None, where
            restack = flat.split("2. **Restack**")[1].split("3. **Measure**")[0]
            assert "`amend! <that PR's commit subject>`" in restack, where
            assert "--allow-empty" in restack, where
            for phrase in (
                "at most one `amend!` per PR",
                "No other `amend!` carries content",
                "at the top of the stack and nowhere else",
                "git log -1 --format=%B <its branch>",
                "git -c core.commentChar=';' rebase --continue",
            ):
                assert phrase in restack, f"{where}: {phrase}"
            for removed in ("correct both", "stage only that path", "folds it into", "one per corrected pr"):
                assert removed not in restack.lower(), f"{where}: {removed}"
            assert flat.count("core.commentChar=';'") == 2, where
            squash = flat.split("6. **Squash**")[1].split("7. **Branches**")[0]
            assert "correcting any restacked PR's line" in squash, where
            message = flat.split("## Commit message standard")[1].split("## Changelog standard")[0]
            assert "No line begins with `;`" in message, where
            for phrase in ("one or two paragraphs", "no review guide", "Stacked on:"):
                assert phrase in message, f"{where}: {phrase}"
            for removed in ("what to look at first", "every `TODO(slug)` placed", "deviations and deferrals"):
                assert removed not in message, f"{where}: {removed}"
            assert "how the next implementer finds" not in flat, where
            assert "`reword`" not in flat and "`edit` in the same rebase" not in flat, where
            assert "names nothing beyond the path you reply as `changelog=`" in flat, where
            assert "relative to the repository root" in flat, where
            assert flat.count("GIT_SEQUENCE_EDITOR") == flat.count("GIT_SEQUENCE_EDITOR=true") == 1, where
            for line in flat.lower().split(". "):
                if "push" in line:
                    assert "never push" in line or "pushes" not in line, f"{where}: {line}"


def test_stack_assembly_ruling_bounds_agree(builds):
    """What an assembly `resume` ruling may and may not direct reads the same
    in the designer that writes the ruling and the assembler that obeys it.
    """
    bounds = (
        "which side of a conflict is right, which PR a contested hunk belongs to, "
        "what a corrected message or changelog line says",
        "drop, edit or re-target a fixup, bypass a hook, or patch code",
        "cannot carry out within",
        "is the same escalation again",
    )
    for build in _stack_builds(builds):
        for name, body in build.bodies("designer"):
            triage = " ".join(_section(body, "### Triage of a stack stop").split())
            for phrase in bounds:
                assert phrase in triage, f"{build.stem}/{name}: {phrase}"
        for name, body in build.bodies("pr-assembler"):
            identify = " ".join(body.split()).split("2. **Restack**")[0]
            for phrase in bounds:
                assert phrase in identify, f"{build.stem}/{name}: {phrase}"


def test_stack_forester_approves_the_stack(builds):
    """In a stack build the forester's ship ruling approves a stack it can
    name, and never a squash.
    """
    for build in builds.values():
        if "forester" not in build.by_id:
            continue
        stack = "pr-assembler" in build.variant.roles and "planner" not in build.variant.roles
        for name, body in build.bodies("forester"):
            ship = " ".join(body.split("## Mode: ship")[1].split("## Mode: repo")[0].split())
            where = f"{build.stem}/{name}"
            assert ("would approve the stack" in ship) == stack, where
            assert (STACK_INPUT in ship) == stack, where
            if "Decide: `approve`" in ship:
                followup = "pr-liaison" in build.by_id
                assert ("the stack stands and is published" in ship) == followup, where
                assert ("the stack stands; nothing is pushed" in ship) == (stack and not followup), where
                assert ("the squash proceeds" in ship) == (not stack), where


STACK_INPUT = "stack — branch and commit per assembled PR, bottom first; empty before the first assembly"


STACK_INPUT_MODES = {
    "orchestrator": (),
    "implementer": ("## Mode: incremental", "## Mode: respond"),
    "designer": ("## Mode: delta", "## Mode: triage"),
    "design-reviewer": (),
    "prepass-reviewer": (),
    "pr-assembler": (),
    "forester": ("## Mode: ship",),
    "pr-liaison": ("## Mode: publish",),
}


def test_stack_input_is_described_once(builds):
    """Every role handed the stack describes it in the one shared wording, once
    per mode that takes it, and the payload carries it, so no launch line adds
    it again.
    """
    for build in _stack_builds(builds):
        for role, modes in STACK_INPUT_MODES.items():
            if role not in build.by_id:
                continue
            for name, body in build.bodies(role):
                where = f"{build.stem}/{name}"
                assert " ".join(body.split()).count(STACK_INPUT) == max(len(modes), 1), where
                for mode in modes:
                    section = " ".join(_raw_section(body, mode).split())
                    assert STACK_INPUT in section, f"{where}: {mode}"
        for name, body in build.bodies("orchestrator"):
            assert "+ the stack" not in " ".join(body.split()), f"{build.stem}/{name}"


def test_stack_implementer_knows_its_pr(builds):
    for stem in REVIEW_CHAIN_STEMS:
        build = builds[stem]
        if "implementer" not in build.by_id:
            continue
        stack = "pr-assembler" in build.variant.roles
        for name, body in build.bodies("implementer"):
            flat = " ".join(body.split())
            phrases = ("pr-done", "within the current PR", "--fixup=")
            for phrase in phrases:
                assert (phrase in flat) == stack, f"{stem}/{name}: {phrase}"
            if stack:
                assert f"**{build.variant.vars['pr_max_lines']}**" in flat, f"{stem}/{name}"
                assert "in the stack you were given" in flat, f"{stem}/{name}"
                for removed in (
                    "log-only",
                    "When the log lives in the repository",
                    "withdraw",
                    "revert --no-commit",
                    "`PR <branch> assembled`",
                    "TODO(quality-open-pr-decided-by-two-sources)",
                ):
                    assert removed not in flat, f"{stem}/{name}: {removed}"


def test_stack_variants_have_no_changelog_author(builds):
    """The assembler writes each PR's changelog line, to the standard the
    changelog-author keeps for the squashing chains.
    """
    standard = "A changelog is not a commit log."
    for build in _stack_builds(builds):
        assert "changelog-author" not in build.variant.roles, build.stem
        for name, body in build.bodies("pr-assembler"):
            assert standard in body, f"{build.stem}/{name}"
    for name, body in builds[FULL_STEM].bodies("changelog-author"):
        assert standard in body, name


def test_stack_variants_use_the_self_scoping_loop(builds):
    """A stack round is scoped by the implementer against its PR; no planner
    splits it, and the implementer renders the self-scoping loop.
    """
    for build in _stack_builds(builds):
        assert not ({"planner", "coder", "coder-supervisor"} & set(build.variant.roles)), build.stem
        for name, body in build.bodies("implementer"):
            flat = " ".join(body.split())
            assert "## Mode: incremental" in body, f"{build.stem}/{name}"
            assert "`in-progress`" in flat, f"{build.stem}/{name}"
            for word in ("`replan`", "`respawn`", "planner"):
                assert word not in flat, f"{build.stem}/{name}: {word}"


def test_stack_designer_and_reviewer_carry_the_stack_section(builds):
    """PR scope is decided in the design's Stack section and checked at design
    and delta review; the pre-pass holds each round to its own PR.
    """
    for stem in REVIEW_CHAIN_STEMS:
        build = builds[stem]
        stack = "pr-assembler" in build.variant.roles
        target = build.variant.vars.get("pr_target_lines")
        limit = build.variant.vars.get("pr_max_lines")
        for name, body in build.bodies("designer"):
            flat = " ".join(body.split())
            where = f"{stem}/{name}"
            assert ("- **Stack** —" in flat) == stack, where
            assert ("keep their place, not their content" in flat) == stack, where
            assert ("### Triage of a stack stop" in body) == stack, where
            assert ("keeps its position, not its content" in flat) == stack, where
            assert (OPEN_PR_RULE in flat) == stack, where
            assert "only by **re-split**" not in flat, where
            assert ("The implementer is never the first to run a done line" in flat) == stack, where
            assert ("running its command once on the base tree" in flat) == stack, where
            if stack:
                assert f"at most {target};" in flat, where
                assert f"never above {limit}" in flat, where
                assert "whose branch is not in the stack you were given" in flat, where
            assert "assembly entry in the log" not in flat, where
        if "design-reviewer" not in build.by_id:
            continue
        for name, body in build.bodies("design-reviewer"):
            enforce = " ".join(body.split("## Enforce")[1].split("## Reviewing a delta")[0].split())
            where = f"{stem}/{name}"
            assert ("**Stack** —" in enforce) == stack, where
            if stack:
                assert f"exceeds {target} added lines" in enforce, where
                assert f"none exceeds {limit}" in enforce, where
                assert "**Does the composite stack hold?**" in body, where
                delta_check = " ".join(body.split("**Does the composite stack hold?**")[1].split())
                assert "whose branch is not in the stack you were given" in delta_check, where
                assert "The open PR keeps its position, not its content" in delta_check, where
                assert OPEN_PR_RULE in delta_check, where
                assert "removed or re-scoped only when" not in delta_check, where
            assert "assembly entry in the log" not in " ".join(body.split()), where
        if "prepass-reviewer" not in build.by_id:
            continue
        for name, body in build.bodies("prepass-reviewer"):
            flat = " ".join(body.split())
            assert ("assigns to a **later** PR" in flat) == stack, f"{stem}/{name}"
            assert ("whose branch is not in the stack you were given" in flat) == stack, f"{stem}/{name}"
            assert "assembly entry in the log" not in flat, f"{stem}/{name}"
            assert ("review base" in flat) == stack, f"{stem}/{name}"
            assert ("this round shipped after the base" in flat) == stack, f"{stem}/{name}"
            assert "log-against-design" in flat, f"{stem}/{name}"


def test_followup_designer_and_reviewer_carry_responses(builds):
    """A follow-up design answers every PR thread in a Responses section, the
    reviewer holds it to one entry per thread, and the forester signs the
    responses as the owner.
    """
    for stem in REVIEW_CHAIN_STEMS:
        build = builds[stem]
        followup = stem in FOLLOWUP_STEMS
        for name, body in build.bodies("designer"):
            where = f"{stem}/{name}"
            draft = " ".join(_raw_section(body, "## Mode: draft").split())
            respond = " ".join(_raw_section(body, "## Mode: respond").split())
            revise = " ".join(_raw_section(body, "## Mode: revise").split())
            assert ("- **Responses** —" in draft) == followup, where
            assert ("one entry per **thread**" in draft) == followup, where
            assert ("the inherited entries first" in draft) == followup, where
            assert ("A `pr-comments-<K>.md` handed as the owner's notes" in respond) == followup, where
            assert ("A `Won't-Do` on a human's comment is still answered" in respond) == followup, where
            assert ("A `pr-comments-<K>.md` among the change inputs" in revise) == followup, where
        if "design-reviewer" in build.by_id:
            for name, body in build.bodies("design-reviewer"):
                where = f"{stem}/{name}"
                enforce = " ".join(body.split("## Enforce")[1].split("## Reviewing a delta")[0].split())
                assert ("**Responses** —" in enforce) == followup, where
                assert ("every thread item not marked **ours** has exactly one entry" in enforce) == followup, where
        if "forester" in build.by_id:
            for name, body in build.bodies("forester"):
                where = f"{stem}/{name}"
                design = " ".join(_raw_section(body, "## Mode: design").split())
                assert ("Approve only responses the owner would sign" in design) == followup, where
                assert ("every `pr-posted-<K>.md`" in design) == followup, where
