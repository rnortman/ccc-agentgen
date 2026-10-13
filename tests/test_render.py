from __future__ import annotations

import pytest
import yaml

from agentgen import AgentGenError
from agentgen.emit import read_definition, serialize
from agentgen.model import Variant, load_variant
from agentgen.render import (
    RenderedRole,
    load_role_spec,
    parse_frontmatter,
    render_variant,
    split_frontmatter,
)

TWO_ROLES = """
name = "demo"

[defaults]
model = "claude-opus-5-5[1M]"
codex_model = "gpt-6-astra"

[vars]
round_cap_lines = 4000

[roles.orchestrator]
[roles.explorer]
"""

ONE_ROLE = """
name = "lite"

[defaults]
model = "claude-opus-5-5[1M]"

[vars]
round_cap_lines = 4000

[roles.orchestrator]
"""


def rendered_by_id(roles):
    return {r.id: r for r in roles}


def test_split_frontmatter_separates_front_and_body():
    front, body = split_frontmatter("---\na: 1\n---\nhello\n", "x")
    assert front == "a: 1\n"
    assert body == "hello\n"


@pytest.mark.parametrize("body", ["hello\n", "hello"])
def test_serialize_round_trips_through_split_frontmatter(body):
    role = RenderedRole(
        id="explorer",
        frontmatter={"name": "explorer", "description": "explores", "model": "m"},
        body=body,
    )
    front, out_body = split_frontmatter(serialize(role), "explorer.md")
    assert yaml.safe_load(front) == role.frontmatter
    assert out_body == "hello\n"


def test_read_definition_is_the_inverse_of_serialize(tmp_path):
    role = RenderedRole(
        id="explorer",
        frontmatter={"name": "explorer", "description": "explores", "tools": ["Read"]},
        body="You explore.\n",
    )
    path = tmp_path / "explorer.md"
    path.write_text(serialize(role), encoding="utf-8")
    assert read_definition(path) == role


def test_read_definition_reports_the_file_it_could_not_parse(tmp_path):
    path = tmp_path / "explorer.md"
    path.write_text("no fence here\n", encoding="utf-8")
    with pytest.raises(AgentGenError, match="explorer.md"):
        read_definition(path)


def test_split_frontmatter_requires_opening_fence():
    with pytest.raises(AgentGenError, match="must start with"):
        split_frontmatter("hello\n", "x")


def test_split_frontmatter_requires_closing_fence():
    with pytest.raises(AgentGenError, match="never closed"):
        split_frontmatter("---\na: 1\nhello\n", "x")


def test_parse_frontmatter_rejects_non_mapping():
    with pytest.raises(AgentGenError, match="must be a YAML mapping"):
        parse_frontmatter("- a\n- b\n", "x")


def test_invalid_yaml_frontmatter_names_the_file(root):
    root.role("orchestrator", "description: [unclosed", "body\n")
    variant = load_variant(root.variant("lite", ONE_ROLE))
    with pytest.raises(AgentGenError, match="invalid YAML frontmatter") as exc:
        load_role_spec(root.templates, "orchestrator", variant)
    assert "orchestrator.md.j2" in str(exc.value)


def test_template_error_line_numbers_match_the_file(root):
    root.role(
        "orchestrator",
        "description: orchestrates\ntools: [Read]",
        "one\ntwo\n{% if true %}\nthree\n",
    )
    variant = load_variant(root.variant("lite", ONE_ROLE))
    # The file is 8 lines; a loader that dropped the 4 frontmatter lines instead
    # of blanking them would report line 4.
    with pytest.raises(AgentGenError, match="line 8") as exc:
        render_variant(root.templates, variant)
    assert "Unexpected end of template" in str(exc.value)


def test_template_error_in_a_partial_names_the_partial(root):
    root.partial("broken.md.j2", "alpha\nbeta\n{% if true %}\ngamma\n")
    root.role(
        "orchestrator",
        "description: orchestrates",
        'one\n{% include "partials/broken.md.j2" %}\n',
    )
    variant = load_variant(root.variant("lite", ONE_ROLE))
    with pytest.raises(AgentGenError, match=r"partials/broken\.md\.j2, line 4"):
        render_variant(root.templates, variant)


def test_missing_var_fails_the_build(root):
    root.role("orchestrator", "description: orchestrates", "Cap is {{ vars.nope }}.\n")
    variant = load_variant(root.variant("lite", ONE_ROLE))
    with pytest.raises(AgentGenError, match="nope"):
        render_variant(root.templates, variant)


def test_roles_conditional_includes_and_excludes_a_stage(root):
    body = (
        "Start.\n"
        "{% if 'explorer' in roles %}\n"
        "Explore first.\n"
        "{% endif %}\n"
        "Cap {{ vars.round_cap_lines }}.\n"
    )
    root.role("orchestrator", "description: orchestrates", body)
    root.role("explorer", "description: explores", "You explore.\n")

    full = rendered_by_id(render_variant(root.templates, load_variant(root.variant("demo", TWO_ROLES))))
    assert "Explore first." in full["orchestrator"].body
    assert "Cap 4000." in full["orchestrator"].body

    lite = rendered_by_id(render_variant(root.templates, load_variant(root.variant("lite", ONE_ROLE))))
    assert "Explore first." not in lite["orchestrator"].body


def test_macro_partial_renders_its_argument(root):
    root.partial(
        "reply.md.j2",
        "{% macro reply(tokens) %}\n"
        "Reply with the path, plus one of: {{ tokens | join(', ') }}.\n"
        "{% endmacro %}\n",
    )
    body = "{% from 'partials/reply.md.j2' import reply %}\n{{ reply(['done', 'in progress']) }}"
    root.role("orchestrator", "description: orchestrates", body)
    roles = render_variant(root.templates, load_variant(root.variant("lite", ONE_ROLE)))
    assert "plus one of: done, in progress." in roles[0].body


def test_role_context_exposes_id_and_frontmatter(root):
    root.role("orchestrator", "description: orchestrates", "You are {{ role.id }} in {{ variant }}.\n")
    roles = render_variant(root.templates, load_variant(root.variant("lite", ONE_ROLE)))
    assert roles[0].body.strip() == "You are orchestrator in lite."


def test_plain_include_of_a_partial(root):
    root.partial("one-shot.md.j2", "One-shot. Single pass.\n")
    root.role("orchestrator", "description: orchestrates", "{% include 'partials/one-shot.md.j2' %}")
    roles = render_variant(root.templates, load_variant(root.variant("lite", ONE_ROLE)))
    assert "One-shot. Single pass." in roles[0].body


def test_template_policy_is_normalized_with_a_template_path_diagnostic(root):
    path = root.role("orchestrator", "description: orchestrates\nharness: robot", "body\n")
    variant = load_variant(root.variant("lite", ONE_ROLE))
    with pytest.raises(AgentGenError, match="unknown harness") as exc:
        load_role_spec(root.templates, "orchestrator", variant)
    assert str(path) in str(exc.value)


def harness_variant(root, policy: str = ""):
    front = "description: explores\n" + (f"harness: {policy}\n" if policy else "")
    root.role(
        "explorer",
        front,
        "{{ harness }}|{{ harnesses | join(',') }}|{{ role.harness | default('absent') }}\n",
    )
    return load_variant(
        root.variant(
            "demo",
            'name = "demo"\n[defaults]\nmodel = "m"\ncodex_model = "c"\n'
            '[roles.explorer]\n',
        )
    )


def test_unset_policy_renders_claude_base_and_codex_overlay(root):
    role = render_variant(root.templates, harness_variant(root))[0]
    assert role.harnesses == ["claude", "codex"]
    assert role.body == "claude|claude,codex|absent\n"
    assert role.body_for("claude") == role.body
    assert role.body_for("codex") == "codex|claude,codex|absent\n"
    assert role.overlays == {"codex": role.body_for("codex")}


def test_codex_only_policy_renders_only_codex_base(root):
    role = render_variant(root.templates, harness_variant(root, "codex"))[0]
    assert role.harnesses == ["codex"]
    assert role.body == "codex|codex|['codex']\n"
    assert role.overlays == {}


def test_explicit_harness_order_selects_base_and_overlay(root):
    role = render_variant(root.templates, harness_variant(root, "[codex, claude]"))[0]
    assert role.harnesses == ["codex", "claude"]
    assert role.body == "codex|codex,claude|['codex', 'claude']\n"
    assert role.overlays == {"claude": "claude|codex,claude|['codex', 'claude']\n"}


def test_harness_neutral_template_emits_no_overlay(root):
    root.role("explorer", "description: explores", "same body\n")
    variant = load_variant(root.variant("demo", '[defaults]\nmodel = "m"\ncodex_model = "c"\n[roles.explorer]\n'))
    role = render_variant(root.templates, variant)[0]
    assert role.overlays == {}


@pytest.mark.parametrize("policy", ["codex", []])
def test_render_rejects_unnormalized_variant_policy_before_rendering(root, policy):
    root.role("explorer", "description: explores", "body\n")
    variant = Variant(
        name="demo",
        defaults={"harness": policy, "model": "m", "codex_model": "c"},
        roles={"explorer": {}},
    )
    with pytest.raises(AgentGenError, match=r"role explorer: invalid normalized harness policy"):
        render_variant(root.templates, variant)
