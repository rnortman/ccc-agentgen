from __future__ import annotations

import pytest

from agentgen.check import MAX_BODY_BYTES, WARN_BODY_BYTES, check_all, check_role, spawn_references
from agentgen.model import HARNESSES
from agentgen.render import RenderedRole

GOOD = {
    "name": "explorer",
    "description": "Answers questions about a codebase.",
    "model": "claude-opus-5-5[1M]",
    "effort": "low",
    "codex_model": "gpt-5.6-sol",
    "codex_effort": "low",
    "codex_sandbox": "workspace-write",
}


def role(body: str = "You explore.\n", **overrides) -> RenderedRole:
    overlays = overrides.pop("overlays", {})
    front = dict(GOOD)
    for key, value in overrides.items():
        if value is None:
            front.pop(key, None)
        else:
            front[key] = value
    return RenderedRole(id="explorer", frontmatter=front, body=body, overlays=overlays)


def test_passing_fixture_is_clean():
    body = "Spawn with `ccc run <type>`; never `ccc run $AGENT`.\n"
    result = check_role(role(body=body), ["explorer"])
    assert result.ok and not result.warnings


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"stage": "review"}, "not accepted by ccc"),
        ({"name": "scout"}, "must equal the file stem"),
        ({"description": "  "}, "`description` is required"),
        ({"model": None, "codex_model": None}, "names no model"),
        ({"effort": "hi"}, "not a Claude effort"),
        ({"codex_effort": "max"}, "not a Codex effort"),
        ({"codex_sandbox": "danger"}, "not one of"),
        ({"model": "  ", "codex_model": None}, "names no model"),
        ({"model": 5}, "must be a string"),
        ({"effort": 3}, "must be a string"),
    ],
)
def test_frontmatter_rules(overrides, expected):
    result = check_role(role(**overrides), ["explorer"])
    assert not result.ok
    assert any(expected in e for e in result.errors), result.errors


def test_empty_body_fails():
    result = check_role(role(body="   \n"), ["explorer"])
    assert not result.ok
    assert any("body is empty" in e for e in result.errors)


def test_oversize_body_fails():
    result = check_role(role(body="x" * (MAX_BODY_BYTES + 1)), ["explorer"])
    assert not result.ok
    assert any("ceiling" in e for e in result.errors)


def test_large_body_warns_but_passes():
    result = check_role(role(body="x" * (WARN_BODY_BYTES + 10)), ["explorer"])
    assert result.ok
    assert any("argv ceiling" in w for w in result.warnings)


def test_dangling_spawn_reference_fails():
    result = check_role(role(body="Then `ccc run socratic-reviewer` to check.\n"), ["explorer"])
    assert not result.ok
    assert any("socratic-reviewer" in e for e in result.errors)


def test_known_spawn_reference_passes():
    body = "Explore with `ccc run explorer` before designing.\n"
    assert check_role(role(body=body), ["explorer", "designer"]).ok


def test_spawn_reference_token_shape():
    text = (
        "ccc run <type> --model x\n"
        "ccc run $AGENT\n"
        "ccc run {{ role.id }}\n"
        "ccc launch tracer-reviewer\n"
        "ccc run design-reviewer\n"
        "ccc resume abc123\n"
    )
    assert spawn_references(text) == {"tracer-reviewer", "design-reviewer"}


def test_blank_effort_is_absent_not_invalid():
    assert check_role(role(effort="  "), ["explorer"]).ok


@pytest.mark.parametrize(
    "policy, removed, expected",
    [
        (["claude"], "codex_model", None),
        (["codex"], "model", None),
        (["claude"], "model", "explorer.md: allows claude but names no model"),
        (["codex"], "codex_model", "explorer.md: allows codex but names no codex_model"),
        (["claude", "codex"], None, None),
    ],
)
def test_policy_controls_required_models(policy, removed, expected):
    overrides = {"harness": policy}
    if removed:
        overrides[removed] = None
    result = check_role(role(**overrides), ["explorer"])
    if expected is None:
        assert result.ok, result.errors
    else:
        assert result.errors == [expected]


def test_default_policy_requires_both_named_models():
    for removed, expected in [
        ("codex_model", "explorer.md: names no codex_model; either add one or set harness: claude"),
        ("model", "explorer.md: names no model; either add one or set harness: codex"),
    ]:
        front = dict(GOOD)
        front.pop(removed)
        result = check_role(RenderedRole("explorer", front, "body\n"), ["explorer"])
        assert result.errors == [expected]


def test_check_accepts_excluded_harness_knobs_on_readback():
    codex = check_role(
        role(harness=["codex"], model="claude", effort="low", tools=["Read"]), ["explorer"]
    )
    claude = check_role(
        role(
            harness=["claude"],
            codex_model="codex",
            codex_effort="low",
            codex_sandbox="workspace-write",
        ),
        ["explorer"],
    )
    assert codex.ok
    assert claude.ok


@pytest.mark.parametrize("policy", [[], ["claude", "claude"], ["robot"], "codex", [3]])
def test_invalid_harness_policy_skips_model_cascades(policy):
    result = check_role(role(harness=policy, model=None, codex_model=None), ["explorer"])
    assert not result.ok
    assert any("harness" in error for error in result.errors)
    assert not any("allows" in error or "names no model" in error for error in result.errors)


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.parametrize(
    "body, expected",
    [
        ("\n---\nbody\n", "must not start with a frontmatter fence"),
        ("\ufeff---\nbody\n", "must not start with a frontmatter fence"),
        ("ccc run ghost\n", "spawns `ghost`"),
        ("   \n", "body is empty"),
        ("x" * (MAX_BODY_BYTES + 1), "ceiling"),
    ],
)
def test_overlay_body_rules_are_independent(harness, body, expected):
    result = check_role(role(overlays={harness: body}), ["explorer"])
    assert not result.ok
    assert any(f"explorer.{harness}.md" in error and expected in error for error in result.errors)


@pytest.mark.parametrize("harness", HARNESSES)
def test_overlay_bom_plus_whitespace_is_empty(harness):
    result = check_role(role(overlays={harness: "\ufeff \n\t"}), ["explorer"])
    assert not result.ok
    assert any(f"explorer.{harness}.md" in error and "body is empty" in error for error in result.errors)


@pytest.mark.parametrize("harness", HARNESSES)
def test_overlay_body_warning_is_independent(harness):
    result = check_role(role(overlays={harness: "x" * (WARN_BODY_BYTES + 1)}), ["explorer"])
    assert result.ok
    assert any(f"explorer.{harness}.md" in warning and "argv ceiling" in warning for warning in result.warnings)


def test_check_all_aggregates():
    roles = [role(), role(body="ccc run ghost\n")]
    result = check_all(roles)
    assert not result.ok
    assert len(result.errors) == 1


def test_check_all_aggregates_warnings():
    roles = [role(), role(body="x" * (WARN_BODY_BYTES + 10))]
    result = check_all(roles)
    assert result.ok
    assert len(result.warnings) == 1
