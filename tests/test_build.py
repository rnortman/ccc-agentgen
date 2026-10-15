from __future__ import annotations

import pytest

from agentgen import AgentGenError
from agentgen.check import WARN_BODY_BYTES
from agentgen.cli import build, main
from agentgen.emit import MARKER_NAME, check_out_dir, prepare_out_dir, read_build, read_definition, write_all
from agentgen.render import RenderedRole

VARIANT = """
name = "demo"

[defaults]
model = "claude-opus-5-5[1M]"
effort = "medium"
codex_model = "gpt-6-astra"
codex_effort = "medium"
codex_sandbox = "workspace-write"

[vars]
round_cap_lines = 4000

[roles.orchestrator]
effort = "high"
[roles.explorer]
effort = "low"
"""


@pytest.fixture
def demo(root):
    root.role(
        "orchestrator",
        "description: Runs the workflow.\ntools: [Read, Bash]",
        "You drive the chain. Spawn with `ccc run explorer`.\n",
    )
    root.role("explorer", "description: Explores a codebase.", "You explore. Read-only.\n")
    root.variant("demo", VARIANT)
    return root


def test_build_writes_one_definition_per_role(demo):
    out = demo.path / "out"
    written = build("demo", demo.path, out)
    assert sorted(p.name for p in written) == ["explorer.md", "orchestrator.md"]

    role = read_definition(out / "orchestrator.md")
    assert role.frontmatter["name"] == "orchestrator"
    assert role.frontmatter["effort"] == "high"
    assert role.frontmatter["codex_sandbox"] == "workspace-write"
    assert role.frontmatter["tools"] == ["Read", "Bash"]
    assert "ccc run explorer" in role.body


def test_build_default_out_dir(demo):
    written = build("demo", demo.path, None)
    assert written[0].parent == demo.path / "build" / "demo"


def test_build_refuses_a_dangling_spawn_reference(demo):
    demo.role("orchestrator", "description: Runs the workflow.", "Spawn `ccc run judge`.\n")
    out = demo.path / "out"
    with pytest.raises(AgentGenError, match="not a role in this variant"):
        build("demo", demo.path, out)
    assert not out.exists()


def test_a_failing_check_leaves_the_previous_build_untouched(demo):
    out = demo.path / "out"
    build("demo", demo.path, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}

    demo.role("orchestrator", "description: Runs the workflow.", "Spawn `ccc run judge`.\n")
    with pytest.raises(AgentGenError, match="nothing written"):
        build("demo", demo.path, out)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_build_into_an_existing_empty_dir(demo):
    out = demo.path / "out"
    out.mkdir()
    written = build("demo", demo.path, out)
    assert sorted(p.name for p in written) == ["explorer.md", "orchestrator.md"]
    assert (out / MARKER_NAME).is_file()


def test_out_path_that_is_a_file_is_refused(demo):
    out = demo.path / "out"
    out.write_text("x", encoding="utf-8")
    with pytest.raises(AgentGenError, match="not a directory"):
        build("demo", demo.path, out)


def test_symlinked_output_directory_is_refused_without_touching_target(demo, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.md").write_text("keep\n", encoding="utf-8")
    out = demo.path / "out"
    out.symlink_to(target, target_is_directory=True)
    with pytest.raises(AgentGenError, match="symlink"):
        build("demo", demo.path, out)
    assert (target / "keep.md").read_text(encoding="utf-8") == "keep\n"


def test_symlinked_build_marker_is_refused_without_touching_target(demo, tmp_path):
    out = demo.path / "out"
    out.mkdir()
    marker_target = tmp_path / "marker"
    marker_target.write_text("keep\n", encoding="utf-8")
    out.joinpath(MARKER_NAME).symlink_to(marker_target)
    with pytest.raises(AgentGenError, match="previous build"):
        build("demo", demo.path, out)
    assert marker_target.read_text(encoding="utf-8") == "keep\n"


def test_symlinked_generated_markdown_is_refused_without_touching_target(demo, tmp_path):
    out = demo.path / "out"
    build("demo", demo.path, out)
    target = tmp_path / "target.md"
    target.write_text("keep\n", encoding="utf-8")
    (out / "stale.md").symlink_to(target)
    with pytest.raises(AgentGenError, match="stale.md"):
        build("demo", demo.path, out)
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_root_must_hold_variants_for_a_bare_name(demo):
    (demo.path / "variants" / "demo.toml").unlink()
    (demo.path / "variants").rmdir()
    with pytest.raises(AgentGenError, match="does not contain variants/"):
        build("demo", demo.path, demo.path / "out")


def test_an_oversize_body_warns_on_stderr_and_still_builds(demo, capsys):
    demo.role("explorer", "description: Explores a codebase.", "x" * (WARN_BODY_BYTES + 10))
    code = main(["build", "demo", "--root", str(demo.path), "--out", str(demo.path / "out")])
    err = capsys.readouterr().err
    assert code == 0
    assert "warning:" in err and "argv ceiling" in err
    assert (demo.path / "out" / "explorer.md").is_file()


def test_build_marks_the_out_dir_as_its_own(demo):
    out = demo.path / "out"
    build("demo", demo.path, out)
    assert (out / MARKER_NAME).read_text(encoding="utf-8").strip() == "demo"


def test_build_removes_a_stale_definition(demo):
    out = demo.path / "out"
    build("demo", demo.path, out)
    (out / "ghost.md").write_text("stale\n", encoding="utf-8")
    build("demo", demo.path, out)
    assert not (out / "ghost.md").exists()
    assert (out / "explorer.md").exists()


def test_check_out_dir_does_not_clear_a_previous_build(demo):
    out = demo.path / "out"
    build("demo", demo.path, out)
    (out / "stale.md").write_text("stale\n", encoding="utf-8")

    check_out_dir(out)
    assert (out / "stale.md").exists()

    prepare_out_dir(out)
    assert not (out / "stale.md").exists()


def test_build_refuses_a_markdown_dir_it_does_not_own(demo):
    out = demo.path / "out"
    out.mkdir()
    (out / "notes.md").write_text("mine\n", encoding="utf-8")
    with pytest.raises(AgentGenError, match="not a previous build"):
        build("demo", demo.path, out)
    assert (out / "notes.md").exists()


def test_build_refuses_a_non_md_file_in_a_previous_build(demo):
    out = demo.path / "out"
    build("demo", demo.path, out)
    (out / "notes.txt").write_text("mine\n", encoding="utf-8")
    with pytest.raises(AgentGenError, match="not a previous build"):
        build("demo", demo.path, out)
    assert (out / "notes.txt").exists()


def test_root_must_hold_templates(tmp_path):
    with pytest.raises(AgentGenError, match="does not contain templates/"):
        build("demo", tmp_path, tmp_path / "out")


def test_variant_path_outside_root_needs_no_variants_dir(demo, tmp_path):
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text(VARIANT, encoding="utf-8")
    (demo.path / "variants" / "demo.toml").unlink()
    (demo.path / "variants").rmdir()
    written = build(str(elsewhere), demo.path, demo.path / "out")
    assert sorted(p.name for p in written) == ["explorer.md", "orchestrator.md"]


def test_main_reports_failure_as_exit_1(demo, capsys):
    demo.role("orchestrator", "description: Runs the workflow.", "Spawn `ccc run judge`.\n")
    code = main(["build", "demo", "--root", str(demo.path), "--out", str(demo.path / "out")])
    assert code == 1
    assert "agentgen:" in capsys.readouterr().err


def test_main_succeeds(demo, capsys):
    code = main(["build", "demo", "--root", str(demo.path), "--out", str(demo.path / "out")])
    assert code == 0
    assert "wrote 2 definitions" in capsys.readouterr().out


def test_build_writes_and_reads_body_only_overlay(demo):
    demo.role(
        "orchestrator",
        "description: Runs the workflow.",
        "Harness {{ harness }}.\n",
    )
    out = demo.path / "out"
    written = build("demo", demo.path, out)
    assert sorted(path.name for path in written) == [
        "explorer.md",
        "orchestrator.codex.md",
        "orchestrator.md",
    ]
    assert (out / "orchestrator.codex.md").read_text(encoding="utf-8") == "Harness codex.\n"
    reread = {role.id: role for role in read_build(out)}["orchestrator"]
    assert reread.body == "Harness claude.\n"
    assert reread.overlays == {"codex": "Harness codex.\n"}


def test_write_all_normalizes_direct_overlay_body(tmp_path):
    role = RenderedRole(
        id="explorer",
        frontmatter={"name": "explorer", "description": "Explores.", "model": "m"},
        body="base\n",
        overlays={"codex": "no newline"},
    )
    write_all(tmp_path, [role], "demo")
    assert (tmp_path / "explorer.codex.md").read_text(encoding="utf-8") == "no newline\n"
    assert read_build(tmp_path)[0].overlays == {"codex": "no newline\n"}


def test_rebuild_removes_stale_overlay(demo):
    demo.role("orchestrator", "description: Runs the workflow.", "{{ harness }}\n")
    out = demo.path / "out"
    build("demo", demo.path, out)
    assert (out / "orchestrator.codex.md").exists()
    demo.role("orchestrator", "description: Runs the workflow.", "same\n")
    build("demo", demo.path, out)
    assert not (out / "orchestrator.codex.md").exists()


def test_read_build_rejects_orphan_overlay(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "ghost.codex.md").write_text("body\n", encoding="utf-8")
    with pytest.raises(AgentGenError, match="ghost.codex.md"):
        read_build(out)


def test_read_build_reads_claude_overlay(tmp_path):
    (tmp_path / "explorer.md").write_text(
        "---\nname: explorer\ndescription: Explores.\nmodel: m\n---\nbase\n",
        encoding="utf-8",
    )
    (tmp_path / "explorer.claude.md").write_text("claude body\n", encoding="utf-8")
    assert read_build(tmp_path)[0].overlays == {"claude": "claude body\n"}


def test_read_build_reports_orphan_claude_overlay(tmp_path):
    (tmp_path / "ghost.claude.md").write_text("body\n", encoding="utf-8")
    with pytest.raises(AgentGenError, match="ghost.claude.md"):
        read_build(tmp_path)


def test_main_reports_overlay_count(demo, capsys):
    demo.role("orchestrator", "description: Runs the workflow.", "{{ harness }}\n")
    code = main(["build", "demo", "--root", str(demo.path), "--out", str(demo.path / "out")])
    assert code == 0
    output = capsys.readouterr().out
    assert "wrote 2 definitions" in output
    assert "and 1 overlay" in output


def test_main_reports_two_overlay_count(demo, capsys):
    demo.role("orchestrator", "description: Runs the workflow.", "{{ harness }}\n")
    demo.role("explorer", "description: Explores.", "{{ harness }}\n")
    code = main(["build", "demo", "--root", str(demo.path), "--out", str(demo.path / "out")])
    assert code == 0
    assert "and 2 overlays" in capsys.readouterr().out
