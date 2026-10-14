"""Contract and content checks over the shipped templates/ tree.

Every role template is rendered against three synthetic variants — one naming
all of them with the forester deciding, the same with the forester advising,
and one lite shape that drops optional stages — so a template is covered here
from the moment it exists, in both the full and the pruned shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentgen.check import check_all, spawn_references
from agentgen.model import Variant
from agentgen.render import render_variant
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

# Knobs every shipped template may read. StrictUndefined turns a template that
# reads anything else into a failure of this test.
ALL_VARS = {
    "increment_target_min": 500,
    "increment_target_max": 700,
    "increment_stop_lines": 1200,
    "round_cap_lines": 4000,
    "max_increments": 5,
    "designer_spawns_explorers": True,
    "plan_target_min": 400,
    "plan_target_max": 800,
    "unit_retry_cap": 2,
    "forester_autonomy": "decide",
    "forester_gates": ["design", "triage", "scan", "ship"],
    "forester_gate_rounds": 2,
}

# Roles the lite shape drops, exercising the `{% if "<id>" in roles %}` pruning
# and the designer's self-exploring branch. The planner/coder trio goes too, so
# the lite shape renders the implementer loop and the full shape the split one.
LITE_DROPPED = (
    "eli5-explainer",
    "prepass-reviewer",
    "workflow-scanner",
    "explorer",
    "planner",
    "coder",
    "coder-supervisor",
)

# Claude-Code mechanics and tool names that must not reach a rendered body:
# one body serves both harnesses, so everything in it has to be harness-neutral.
FORBIDDEN = (
    "<invoke",
    "function_calls",
    "subagent_type",
    "run_in_background",
    "background",
    "foreground",
    "SendMessage",
    "Read tool",
    "Edit tool",
    "Write tool",
)

# `{% if "<role-id>" in roles %}` — the modularity primitive, in template source.
ROLE_GUARD_RE = re.compile(r'"([a-z][a-z0-9-]*)"\s+in\s+roles')

# `{% include "partials/x.md.j2" %}`, `{% from "partials/x.md.j2" import … %}`.
PARTIAL_REF_RE = re.compile(r'["\']partials/([a-z0-9.-]+\.md\.j2)["\']')


def role_ids() -> list[str]:
    return role_template_ids(TEMPLATES)


def render(ids: list[str], **var_overrides) -> list:
    variant = Variant(
        name="synthetic",
        defaults={
            "model": "claude-opus-5-5[1M]",
            "effort": "medium",
            "codex_model": "gpt-5.6-sol",
            "codex_effort": "medium",
        },
        vars={**ALL_VARS, **var_overrides},
        roles={rid: {} for rid in ids},
    )
    return render_variant(TEMPLATES, variant)


@pytest.fixture(scope="module")
def full():
    return render(role_ids())


@pytest.fixture(scope="module")
def lite():
    ids = [rid for rid in role_ids() if rid not in LITE_DROPPED]
    return render(ids, designer_spawns_explorers=False)


@pytest.fixture(scope="module")
def advise():
    """The forester briefs the user, so the design gate renders the ELI5 too."""
    return render(role_ids(), forester_autonomy="advise")


@pytest.fixture(scope="module", params=["full", "lite", "advise"])
def rendered(request):
    return request.getfixturevalue(request.param)


def test_every_role_template_renders(full):
    assert [role.id for role in full] == role_ids()
    for role in full:
        assert role.body.strip(), role.id


def test_rendered_roles_pass_the_ccc_contract(rendered):
    result = check_all(rendered)
    assert result.errors == []
    assert result.warnings == []


def test_every_role_template_declares_workspace_write(rendered):
    """The synthetic variant sets no `codex_sandbox`, so only the template can.

    The golden build asserts the resolved value, which the shipped `[defaults]`
    supply; this one is the template tier, where `read-only` would deny a
    third-party variant's roles the artifact write they all make.
    """
    for role in rendered:
        assert role.frontmatter.get("codex_sandbox") == "workspace-write", role.id


def test_bodies_are_harness_neutral(rendered):
    """Guard only bodies Codex can receive; Claude-only roles and Claude overlays are exempt."""
    for role in rendered:
        if "codex" not in role.harnesses:
            continue
        body = role.body_for("codex")
        for token in FORBIDDEN:
            assert token not in body, f"{role.id}: {token}"


def test_role_guards_name_roles_that_exist():
    """`{% if "<id>" in roles %}` is silently False on a typo; nothing else checks it."""
    ids = set(role_ids())
    guarded = set()
    for path in (TEMPLATES / "roles").glob("*.md.j2"):
        guarded |= set(ROLE_GUARD_RE.findall(path.read_text(encoding="utf-8")))
    assert guarded, "no role guards found; the pattern moved"
    assert guarded <= ids, f"guards naming no role template: {sorted(guarded - ids)}"


def test_every_partial_is_included_somewhere():
    """A partial nothing includes is never compiled, so it can rot unnoticed."""
    referenced = set()
    for path in list((TEMPLATES / "roles").glob("*.md.j2")) + list(
        (TEMPLATES / "partials").glob("*.md.j2")
    ):
        referenced |= set(PARTIAL_REF_RE.findall(path.read_text(encoding="utf-8")))
    on_disk = {p.name for p in (TEMPLATES / "partials").glob("*.md.j2")}
    assert referenced - on_disk == set(), "includes naming no partial"
    assert on_disk - referenced == set(), "partials nothing includes"


def test_user_directives_partial_is_included_only_by_the_drivers():
    """Only an interactive driver has a user to take directives from."""
    including = {
        path.name.removesuffix(".md.j2")
        for path in list((TEMPLATES / "roles").glob("*.md.j2"))
        + list((TEMPLATES / "partials").glob("*.md.j2"))
        if "user-directives.md.j2" in PARTIAL_REF_RE.findall(path.read_text(encoding="utf-8"))
    }
    assert including == DRIVERS


def test_designer_spawning_a_role_the_variant_dropped_is_caught():
    """The var and the role set can disagree; rule 4 has to make that loud."""
    ids = [rid for rid in role_ids() if rid != "explorer"]
    result = check_all(render(ids, designer_spawns_explorers=True))
    assert any("designer.md" in e and "explorer" in e for e in result.errors), result.errors


def test_clerk_without_an_explorer_spawns_nobody():
    """Dropping the explorer has to take the clerk's spawning text with it."""
    ids = [rid for rid in role_ids() if rid != "explorer"]
    rendered = render(ids, designer_spawns_explorers=False)
    clerk = next(role for role in rendered if role.id == "triage-clerk")
    assert spawn_references(clerk.body) == set()
    assert SPAWNING_PARTIAL_SENTINEL not in clerk.body
    assert "review-exploration-" not in clerk.body
    assert "more investigation than" in clerk.body
    assert "as a `retriage` note" in clerk.body
    result = check_all(rendered)
    assert result.errors == []


def _body(rendered, role_id: str) -> str:
    return " ".join(next(role for role in rendered if role.id == role_id).body.split())


def test_forester_defaults_rule_only_when_deciding(full, advise):
    assert DEFAULTS_RULE in _body(full, "forester")
    advising = _body(advise, "forester")
    assert DEFAULTS_RULE not in advising
    assert "userproxy" not in advising


def test_designer_default_and_owner_notes_render(full, advise):
    for rendered in (full, advise):
        designer = _body(rendered, "designer")
        assert "**Default**" in designer
        assert "**No default**" in designer
        assert OWNER_NOTES_LABEL in designer
        assert OWNER_NOTES_LABEL in _body(rendered, "orchestrator")
        assert DEFAULT_CHECK in _body(rendered, "design-reviewer")
