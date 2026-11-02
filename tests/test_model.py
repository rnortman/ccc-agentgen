from __future__ import annotations

from pathlib import Path

import pytest

from agentgen import AgentGenError
from agentgen.model import load_variant, merge_frontmatter, normalize_harness, resolve_variant_path
from agentgen.render import load_role_spec, render_variant
from agentgen.toolsets import load_catalogue

VARIANT = """
name = "demo"

[defaults]
model = "claude-opus-5-5[1M]"
effort = "medium"
codex_model = "gpt-6-astra"

[vars]
round_cap_lines = 4000

[roles.explorer]
effort = "low"
"""


def test_merge_precedence_later_wins():
    merged = merge_frontmatter(
        "explorer",
        {"description": "explores", "effort": "high", "tools": ["Read"]},
        {"model": "m", "effort": "medium"},
        {"effort": "low"},
    )
    assert merged["effort"] == "low"
    assert merged["model"] == "m"
    assert merged["tools"] == ["Read"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        (" CLAUDE, codex ", ["claude", "codex"]),
        ([" Codex ", "CLAUDE"], ["codex", "claude"]),
        (" , ", None),
        (["", "  "], None),
    ],
)
def test_normalize_harness(raw, expected):
    assert normalize_harness(raw, "variant.toml: [defaults]") == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        (3, "must be a string or list"),
        (["claude", 3], "entries must be strings"),
        ("claude,unknown", "unknown harness 'unknown'"),
        (["Claude", " claude "], "duplicate harness 'claude'"),
    ],
)
def test_normalize_harness_rejects_invalid_values(raw, expected):
    with pytest.raises(AgentGenError, match=expected):
        normalize_harness(raw, "variant.toml: [defaults]")


def test_variant_normalizes_policy_and_diagnoses_each_tier(root):
    path = root.variant(
        "demo",
        'name = "demo"\n[defaults]\nharness = ["CLAUDE", "claude"]\n'
        '[roles.explorer]\nharness = "codex"\n',
    )
    with pytest.raises(AgentGenError, match=r"\[defaults\].*duplicate harness"):
        load_variant(path)


def test_variant_reports_invalid_role_tier_policy(root):
    path = root.variant(
        "demo",
        'name = "demo"\n[defaults]\nharness = "claude"\n'
        '[roles.explorer]\nharness = ["codex", "CODEX"]\n',
    )
    with pytest.raises(AgentGenError, match=r"\[roles\.explorer\].*duplicate harness"):
        load_variant(path)


def test_blank_role_policy_does_not_clear_default_policy(root):
    root.role("explorer", "description: explores", "body\n")
    path = root.variant(
        "demo",
        'name = "demo"\n[defaults]\nharness = "codex"\n'
        '[roles.explorer]\nharness = " , "\n',
    )
    spec = load_role_spec(root.templates, "explorer", load_variant(path))
    assert spec.frontmatter["harness"] == ["codex"]


def test_harness_override_replaces_instead_of_unions_and_drops_knobs(root):
    root.role("explorer", "description: explores", "body\n")
    path = root.variant(
        "demo",
        'name = "demo"\n[defaults]\nmodel = "m"\neffort = "low"\n'
        'tools = ["Read"]\ncodex_model = "c"\ncodex_effort = "low"\n'
        'codex_sandbox = "workspace-write"\n[roles.explorer]\nharness = "codex"\n',
    )
    front = load_role_spec(root.templates, "explorer", load_variant(path)).frontmatter
    assert front["harness"] == ["codex"]
    assert "model" not in front and "effort" not in front and "tools" not in front
    assert front["codex_model"] == "c"
    assert list(front) == ["name", "description", "harness", "codex_model", "codex_effort", "codex_sandbox"]


def test_winning_harness_policy_prunes_knobs_from_every_tier(root):
    root.role("designer", "description: designs", "body\n")
    path = root.variant(
        "demo",
        'name = "demo"\n[defaults]\nharness = "codex"\n'
        '[roles.designer]\nmodel = "m"\neffort = "high"\n'
        'codex_model = "c"\ncodex_effort = "medium"\n',
    )
    front = load_role_spec(root.templates, "designer", load_variant(path)).frontmatter
    assert "model" not in front and "effort" not in front
    assert front["codex_model"] == "c"
    assert front["codex_effort"] == "medium"


@pytest.mark.parametrize("policy", ["claude,codex", [], ["codex", "codex"], ["robot"]])
def test_merge_rejects_unnormalized_harness_policy(policy):
    with pytest.raises(AgentGenError, match=r"role explorer: invalid normalized harness policy"):
        merge_frontmatter("explorer", {"description": "d"}, {"harness": policy}, {})


def test_merge_accepts_normalized_harness_policy_and_prunes():
    merged = merge_frontmatter(
        "explorer",
        {"description": "d"},
        {"harness": ["codex"], "model": "m", "codex_model": "c"},
        {},
    )
    assert merged["codex_model"] == "c"
    assert "model" not in merged


def test_name_is_always_the_role_id():
    merged = merge_frontmatter("explorer", {"description": "d"}, {}, {})
    assert merged["name"] == "explorer"


def test_merged_keys_are_ordered_and_ccc_only():
    merged = merge_frontmatter(
        "explorer", {"description": "d", "codex_sandbox": "workspace-write"}, {"model": "m"}, {}
    )
    assert list(merged) == ["name", "description", "model", "codex_sandbox"]


def test_role_template_frontmatter_and_variant_tiers_merge(root):
    root.role("explorer", "description: explores\ntools: [Read]\neffort: high", "body\n")
    path = root.variant("demo", VARIANT)
    variant = load_variant(path)
    spec = load_role_spec(root.templates, "explorer", variant)
    assert spec.frontmatter == {
        "name": "explorer",
        "description": "explores",
        "model": "claude-opus-5-5[1M]",
        "effort": "low",
        "tools": ["Read"],
        "codex_model": "gpt-6-astra",
    }


def test_role_template_with_non_ccc_key_is_rejected(root):
    root.role("explorer", "description: d\nstage: review", "body\n")
    variant = load_variant(root.variant("demo", VARIANT))
    with pytest.raises(AgentGenError, match="not accepted by agentgen"):
        load_role_spec(root.templates, "explorer", variant)


def test_role_template_without_description_is_rejected(root):
    root.role("explorer", "tools: [Read]", "body\n")
    variant = load_variant(root.variant("demo", VARIANT))
    with pytest.raises(AgentGenError, match="`description` is required"):
        load_role_spec(root.templates, "explorer", variant)


def test_variant_naming_a_role_with_no_template(root):
    variant = load_variant(root.variant("demo", VARIANT))
    with pytest.raises(AgentGenError, match="no template at"):
        load_role_spec(root.templates, "explorer", variant)


def test_variant_rejects_non_ccc_keys_in_defaults(root):
    path = root.variant("demo", '[defaults]\nstage = "x"\n[roles.explorer]\n')
    with pytest.raises(AgentGenError, match=r"\[defaults\]"):
        load_variant(path)


def test_variant_rejects_unknown_top_level_table(root):
    path = root.variant("demo", '[nonsense]\nx = 1\n[roles.explorer]\n')
    with pytest.raises(AgentGenError, match="unknown top-level key"):
        load_variant(path)


def test_variant_with_no_roles_is_rejected(root):
    path = root.variant("demo", 'name = "demo"\n')
    with pytest.raises(AgentGenError, match="no `\\[roles"):
        load_variant(path)


def test_variant_file_missing(root):
    with pytest.raises(AgentGenError, match="variant not found"):
        load_variant(root.path / "variants" / "nope.toml")


def test_variant_with_malformed_toml(root):
    path = root.variant("demo", "name = \n")
    with pytest.raises(AgentGenError, match="invalid TOML"):
        load_variant(path)


def test_variant_name_must_be_a_non_empty_string(root):
    path = root.variant("demo", "name = 1\n[roles.explorer]\n")
    with pytest.raises(AgentGenError, match="`name` must be a non-empty string"):
        load_variant(path)


@pytest.mark.parametrize("bad", ["../escaped", "review/chain", ".hidden"])
def test_variant_name_must_be_a_usable_directory_name(root, bad):
    path = root.variant("demo", f'name = "{bad}"\n[roles.explorer]\n')
    with pytest.raises(AgentGenError, match="not a usable directory name"):
        load_variant(path)


@pytest.mark.parametrize("table", ["defaults", "vars", "roles"])
def test_variant_table_must_be_a_table(root, table):
    toml = f"{table} = 3\n" + ("" if table == "roles" else "[roles.explorer]\n")
    path = root.variant("demo", toml)
    with pytest.raises(AgentGenError, match=f"`\\[{table}\\]` must be a table"):
        load_variant(path)


def test_variant_role_override_must_be_a_table(root):
    path = root.variant("demo", "[roles]\nexplorer = 3\n")
    with pytest.raises(AgentGenError, match=r"`\[roles.explorer\]` must be a table"):
        load_variant(path)


@pytest.mark.parametrize("bad", ["Test_Reviewer", "sub/foo", "..", "2fast"])
def test_variant_rejects_a_role_id_ccc_could_not_load(root, bad):
    path = root.variant("demo", f'[roles."{bad}"]\n')
    with pytest.raises(AgentGenError, match="is not a ccc agent type"):
        load_variant(path)


def test_variant_vars_and_role_ids(root):
    variant = load_variant(root.variant("demo", VARIANT))
    assert variant.vars == {"round_cap_lines": 4000}
    assert variant.role_ids == ["explorer"]


def test_resolve_variant_path():
    root_dir = Path("/tmp/root")
    assert resolve_variant_path(root_dir, "demo") == root_dir / "variants" / "demo.toml"
    assert resolve_variant_path(root_dir, "a/b.toml") == Path("a/b.toml")


def test_resolve_variant_path_keeps_a_dotted_bare_name():
    root_dir = Path("/tmp/root")
    assert resolve_variant_path(root_dir, "demo.v2") == root_dir / "variants" / "demo.v2.toml"


MERGE_CATALOGUE = """
[sets.harmless]
description = "always offered"
tools = ["Read"]

[sets.write]
description = "write files"
tools = ["Write"]

[sets.run]
description = "run commands"
tools = ["Bash", "TaskStop"]

[withheld]
Agent = "spawn through ccc"
"""


def _catalogue(root):
    (root.templates / "toolsets.toml").write_text(MERGE_CATALOGUE, encoding="utf-8")
    catalogue = load_catalogue(root.templates)
    assert catalogue is not None
    return catalogue


@pytest.mark.parametrize(
    "template, defaults, overrides, expected",
    [
        ({"toolsets": ["write"]}, {}, {}, ["Read", "Write"]),
        ({"toolsets": ["write"]}, {"toolsets": ["run"]}, {}, ["Read", "Bash", "TaskStop"]),
        ({"toolsets": ["write"]}, {"toolsets": ["run"]}, {"toolsets": []}, ["Read"]),
        ({"toolsets": ["write"], "tools": ["Agent"]}, {}, {}, ["Read", "Write", "Agent"]),
        ({"toolsets": ["write"], "tools": ["Agent"]}, {"tools": []}, {}, ["Read", "Write"]),
        ({"toolsets": ["write"]}, {}, {"tools": ["mcp__s__t"]}, ["Read", "Write", "mcp__s__t"]),
        ({"tools": ["Agent"]}, {}, {}, ["Read", "Agent"]),
    ],
)
def test_merge_resolves_toolsets_per_key_across_tiers(root, template, defaults, overrides, expected):
    merged = merge_frontmatter(
        "explorer", {"description": "d", **template}, defaults, overrides, _catalogue(root)
    )
    assert merged["tools"] == expected
    assert "toolsets" not in merged


def test_merge_reports_resolution_errors_with_the_role(root):
    with pytest.raises(AgentGenError, match="role explorer: `Bash` is granted by tool set `run`"):
        merge_frontmatter(
            "explorer", {"description": "d", "tools": ["Bash"]}, {}, {}, _catalogue(root)
        )


def test_merge_with_catalogue_rejects_a_claude_role_without_a_tool_list(root):
    with pytest.raises(AgentGenError, match="role explorer: a role with no tool list"):
        merge_frontmatter("explorer", {"description": "d"}, {}, {}, _catalogue(root))


def test_merge_with_catalogue_drops_both_keys_for_a_codex_only_role(root):
    merged = merge_frontmatter(
        "coder",
        {"description": "d", "toolsets": ["nonexistent"], "tools": ["Bash"]},
        {"harness": ["codex"]},
        {},
        _catalogue(root),
    )
    assert "tools" not in merged and "toolsets" not in merged


def test_merge_with_catalogue_codex_only_role_needs_no_tool_list(root):
    merged = merge_frontmatter(
        "coder", {"description": "d"}, {}, {"harness": ["codex"]}, _catalogue(root)
    )
    assert "tools" not in merged


def test_merge_with_catalogue_resolves_for_a_role_allowing_both(root):
    merged = merge_frontmatter(
        "explorer",
        {"description": "d", "toolsets": ["run"]},
        {"harness": ["codex", "claude"]},
        {},
        _catalogue(root),
    )
    assert merged["tools"] == ["Read", "Bash", "TaskStop"]


def test_merge_without_catalogue_passes_tools_through():
    merged = merge_frontmatter("explorer", {"description": "d", "tools": ["Bash", "Grep"]}, {}, {})
    assert merged["tools"] == ["Bash", "Grep"]


def test_merge_without_catalogue_needs_no_tool_list():
    merged = merge_frontmatter("explorer", {"description": "d"}, {}, {})
    assert "tools" not in merged


@pytest.mark.parametrize("tier", ["template", "defaults", "overrides"])
def test_merge_without_catalogue_rejects_toolsets(tier):
    tiers = {"template": {"description": "d"}, "defaults": {}, "overrides": {}}
    tiers[tier]["toolsets"] = ["run"]
    with pytest.raises(AgentGenError, match="role explorer: `toolsets` needs a tool-set catalogue"):
        merge_frontmatter("explorer", tiers["template"], tiers["defaults"], tiers["overrides"])


def test_variant_and_template_accept_toolsets(root):
    root.role("explorer", "description: explores\ntoolsets: [write]", "body\n")
    _catalogue(root)
    path = root.variant("demo", '[defaults]\ntoolsets = ["run"]\n[roles.explorer]\ntoolsets = ["write", "run"]\n')
    variant = load_variant(path)
    spec = load_role_spec(root.templates, "explorer", variant, load_catalogue(root.templates))
    assert spec.frontmatter["tools"] == ["Read", "Write", "Bash", "TaskStop"]
    assert "toolsets" not in spec.frontmatter


def test_render_variant_loads_the_catalogue(root):
    root.role("explorer", "description: explores\ntoolsets: [run]", "{{ role.tools | join(',') }}\n")
    _catalogue(root)
    variant = load_variant(root.variant("demo", "[roles.explorer]\n"))
    (role,) = render_variant(root.templates, variant)
    assert role.frontmatter["tools"] == ["Read", "Bash", "TaskStop"]
    assert role.body == "Read,Bash,TaskStop\n"


def test_render_variant_without_catalogue_rejects_toolsets(root):
    root.role("explorer", "description: explores\ntoolsets: [run]", "body\n")
    variant = load_variant(root.variant("demo", "[roles.explorer]\n"))
    with pytest.raises(AgentGenError, match="no templates/toolsets.toml"):
        render_variant(root.templates, variant)
