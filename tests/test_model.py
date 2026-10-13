from __future__ import annotations

from pathlib import Path

import pytest

from agentgen import AgentGenError
from agentgen.model import load_variant, merge_frontmatter, normalize_harness, resolve_variant_path
from agentgen.render import load_role_spec

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
    with pytest.raises(AgentGenError, match="not accepted by ccc"):
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
