from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agentgen import AgentGenError
from agentgen.model import load_variant
from agentgen.render import render_variant
from agentgen.toolsets import MCP_TOOL_RE, load_catalogue

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"
VARIANTS = REPO_ROOT / "variants"

CATALOGUE = """
[sets.harmless]
description = "Always offered."
tools = ["Read"]

[sets.navigate]
description = "Symbol lookup."
tools = ["LSP"]

[sets.write]
description = "Create files."
tools = ["Write"]

[sets.edit]
description = "Change files."
include = ["write"]
tools = ["Edit", "NotebookEdit"]

[sets.run]
description = "Run, wait on and stop processes."
include = ["navigate"]
tools = ["Bash", "TaskStop", "Monitor"]

[sets.forge]
description = "Forge tools."
tools = ["mcp__forge__open_pr"]

[withheld]
Agent = "Spawn through ccc."
Grep = "Dropped beside Bash."
"""


def write_catalogue(root, text: str):
    path = root.templates / "toolsets.toml"
    path.write_text(text, encoding="utf-8")
    return path


def load(root, text: str = CATALOGUE):
    write_catalogue(root, text)
    catalogue = load_catalogue(root.templates)
    assert catalogue is not None
    return catalogue


def test_absent_catalogue_loads_as_none(root):
    assert load_catalogue(root.templates) is None


def test_catalogue_keeps_file_order_and_withheld_reasons(root):
    catalogue = load(root)
    assert [s.name for s in catalogue.sets] == ["harmless", "navigate", "write", "edit", "run", "forge"]
    assert catalogue.withheld == {"Agent": "Spawn through ccc.", "Grep": "Dropped beside Bash."}
    assert catalogue.path == root.templates / "toolsets.toml"


def test_catalogue_classifies_set_and_withheld_tools(root):
    catalogue = load(root)
    assert catalogue.granting_set("Edit") == "edit"
    assert catalogue.granting_set("Grep") is None
    assert catalogue.classifies("Monitor")
    assert catalogue.classifies("Agent")
    assert not catalogue.classifies("Bassh")
    assert not catalogue.classifies("mcp__other__tool")


HARMLESS = '[sets.harmless]\ndescription = "h"\ntools = ["Read"]\n'


@pytest.mark.parametrize(
    "text, message",
    [
        ("sets = \n", "invalid TOML"),
        (HARMLESS + "[extra]\n", r"unknown top-level key\(s\): extra"),
        ("sets = 3\n", "`sets` must be a table"),
        ("withheld = 3\n" + HARMLESS, "`withheld` must be a table"),
        (HARMLESS + "[sets.run]\ndescription = \"r\"\ntool = [\"Bash\"]\n", r"\[sets.run\]: unknown key\(s\): tool"),
        (HARMLESS + "[sets.Run]\ndescription = \"r\"\n", r"set name 'Run' must be lowercase"),
        (HARMLESS + "[sets.run_it]\ndescription = \"r\"\n", r"set name 'run_it'"),
        (HARMLESS + "[sets.run]\ntools = [\"Bash\"]\n", r"\[sets.run\]: `description` is required"),
        (HARMLESS + "[sets.run]\ndescription = \"  \"\n", r"\[sets.run\]: `description` is required"),
        (HARMLESS + "[sets]\nrun = 3\n", r"\[sets.run\] must be a table"),
        (HARMLESS + "[sets.run]\ndescription = \"r\"\ntools = \"Bash\"\n", r"\[sets.run\]: `tools` must be a list"),
        (HARMLESS + "[sets.run]\ndescription = \"r\"\ntools = [\"\"]\n", r"\[sets.run\]: `tools` must be a list"),
        (HARMLESS + "[sets.run]\ndescription = \"r\"\ninclude = [1]\n", r"\[sets.run\]: `include` must be a list"),
        (HARMLESS + "[sets.run]\ndescription = \"r\"\ninclude = [\"nope\"]\n", r"\[sets.run\]: `include` names unknown set 'nope'"),
        (
            HARMLESS
            + "[sets.a]\ndescription = \"a\"\ninclude = [\"b\"]\n"
            + "[sets.b]\ndescription = \"b\"\ninclude = [\"a\"]\n",
            r"`include` forms a cycle: a -> b -> a",
        ),
        (HARMLESS + "[sets.a]\ndescription = \"a\"\ninclude = [\"a\"]\n", r"cycle: a -> a"),
        (HARMLESS + "[sets.run]\ndescription = \"r\"\ntools = [\"Read\"]\n", r"\[sets.run\]: tool `Read` is already granted by \[sets.harmless\]"),
        (HARMLESS + "[withheld]\nRead = \"no\"\n", r"\[withheld\]: tool `Read` is granted by \[sets.harmless\]"),
        (HARMLESS + "[withheld]\nAgent = \"\"\n", r"\[withheld\]: `Agent` needs a non-empty reason"),
        (HARMLESS + "[withheld]\nAgent = 1\n", r"\[withheld\]: `Agent` needs a non-empty reason"),
        (HARMLESS + "[sets.run]\ndescription = \"r\"\ntools = [\"Bash\", \"Bash\"]\n", r"`tools` lists Bash more than once"),
        (
            HARMLESS + "[sets.w]\ndescription = \"w\"\n[sets.run]\ndescription = \"r\"\ninclude = [\"w\", \"w\"]\n",
            r"`include` lists w more than once",
        ),
        ("[sets.run]\ndescription = \"r\"\ntools = [\"Bash\"]\n", r"\[sets.harmless\] is required"),
        (
            "[sets.w]\ndescription = \"w\"\n[sets.harmless]\ndescription = \"h\"\ntools = [\"Read\"]\ninclude = [\"w\"]\n",
            r"\[sets.harmless\] may not include another set",
        ),
        ("[sets.harmless]\ndescription = \"h\"\n", r"\[sets.harmless\] must grant at least one tool"),
    ],
)
def test_catalogue_load_errors(root, text, message):
    path = write_catalogue(root, text)
    with pytest.raises(AgentGenError, match=message) as excinfo:
        load_catalogue(root.templates)
    assert str(path) in str(excinfo.value)


def test_resolve_always_includes_harmless(root):
    assert load(root).resolve("r", [], None) == ["Read"]
    assert load(root).resolve("r", None, None) == ["Read"]


def test_resolve_follows_includes_in_catalogue_order(root):
    catalogue = load(root)
    assert catalogue.resolve("r", ["run", "edit"], None) == [
        "Read",
        "LSP",
        "Write",
        "Edit",
        "NotebookEdit",
        "Bash",
        "TaskStop",
        "Monitor",
    ]
    assert catalogue.resolve("r", ["write", "run"], None) == ["Read", "LSP", "Write", "Bash", "TaskStop", "Monitor"]
    assert catalogue.resolve("r", ["run"], None) == ["Read", "LSP", "Bash", "TaskStop", "Monitor"]
    assert catalogue.resolve("r", ["write"], None) == ["Read", "Write"]


def test_resolve_drops_duplicates(root):
    catalogue = load(root)
    assert catalogue.resolve("r", ["edit", "write", "edit", "harmless"], ["Agent", "Agent"]) == [
        "Read",
        "Write",
        "Edit",
        "NotebookEdit",
        "Agent",
    ]


def test_include_diamond_loads_and_resolves_each_tool_once(root):
    catalogue = load(
        root,
        HARMLESS
        + '[sets.base]\ndescription = "b"\ntools = ["Bash"]\n'
        + '[sets.left]\ndescription = "l"\ninclude = ["base"]\ntools = ["Write"]\n'
        + '[sets.right]\ndescription = "r"\ninclude = ["base"]\ntools = ["Edit"]\n'
        + '[sets.top]\ndescription = "t"\ninclude = ["left", "right"]\ntools = ["Monitor"]\n',
    )
    assert catalogue.resolve("r", ["top"], None) == ["Read", "Bash", "Write", "Edit", "Monitor"]


def test_resolve_appends_extras_in_given_order(root):
    catalogue = load(root)
    assert catalogue.resolve("r", ["write"], ["mcp__other__tool", "Grep", "Agent"]) == [
        "Read",
        "Write",
        "mcp__other__tool",
        "Grep",
        "Agent",
    ]


@pytest.mark.parametrize(
    "extra, granting",
    [("Bash", "run"), ("Read", "harmless"), ("Write", "write"), ("mcp__forge__open_pr", "forge")],
)
def test_resolve_rejects_an_extra_a_set_grants(root, extra, granting):
    with pytest.raises(
        AgentGenError,
        match=rf"role r: `{extra}` is granted by tool set `{granting}`; name the set in `toolsets`",
    ):
        load(root).resolve("r", ["write"], [extra])


@pytest.mark.parametrize("extra", ["Bassh", "TaskOutput", "mcp__nounderscore", "mcp____tool"])
def test_resolve_rejects_an_unknown_extra(root, extra):
    with pytest.raises(
        AgentGenError,
        match=rf"role r: unknown tool '{extra}'; Claude Code drops unknown names silently; "
        r"classify it in .*templates/toolsets\.toml",
    ):
        load(root).resolve("r", [], [extra])


def test_resolve_rejects_an_unknown_set(root):
    with pytest.raises(AgentGenError, match=r"role r: unknown tool set 'runn'; known: harmless, navigate"):
        load(root).resolve("r", ["runn"], None)


@pytest.mark.parametrize("value", ["run", [3], [""], {"run": True}])
def test_resolve_rejects_a_malformed_toolsets_value(root, value):
    with pytest.raises(AgentGenError, match=r"role r: `toolsets` must be a list of non-empty strings"):
        load(root).resolve("r", value, None)


@pytest.mark.parametrize("value", ["Agent", [None]])
def test_resolve_rejects_a_malformed_tools_value(root, value):
    with pytest.raises(AgentGenError, match=r"role r: `tools` must be a list of non-empty strings"):
        load(root).resolve("r", [], value)


PROBE = os.environ.get("AGENTGEN_PROBE_CLAUDE") == "1"
probe = pytest.mark.skipif(not PROBE, reason="set AGENTGEN_PROBE_CLAUDE=1 to probe the installed Claude Code")


def _registered_tools(tmp_path: Path, tools: list[str] | None) -> list[str]:
    """The tools Claude Code registers for a probe agent given `tools`."""
    claude = shutil.which("claude")
    assert claude, "AGENTGEN_PROBE_CLAUDE=1 but `claude` is not on PATH"
    agent: dict = {"description": "Tool-set probe.", "prompt": "Reply ok."}
    if tools is not None:
        agent["tools"] = tools
    result = subprocess.run(
        [claude, "-p", "--output-format", "stream-json", "--verbose", "--model", "haiku",
         "--agents", json.dumps({"probe": agent}), "--agent", "probe", "Reply ok."],
        cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300,
    )
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            return list(event["tools"])
    pytest.fail(f"no system/init event (exit {result.returncode}): {result.stderr[-2000:]}")


def _emitted_tool_lists() -> list[list[str]]:
    """Every distinct Claude `tools` list the shipped variants emit, MCP names removed."""
    seen: list[list[str]] = []
    for path in sorted(VARIANTS.glob("*.toml")):
        for role in render_variant(TEMPLATES, load_variant(path)):
            if "claude" not in role.harnesses:
                continue
            tools = [tool for tool in role.frontmatter["tools"] if not MCP_TOOL_RE.match(tool)]
            if tools not in seen:
                seen.append(tools)
    assert seen
    return seen


@probe
def test_probe_every_claude_code_tool_is_classified(tmp_path):
    catalogue = load_catalogue(TEMPLATES)
    assert catalogue is not None
    inventory = _registered_tools(tmp_path, None)
    unclassified = [tool for tool in inventory if not tool.startswith("mcp__") and not catalogue.classifies(tool)]
    assert unclassified == [], f"classify {unclassified} in templates/toolsets.toml"


@probe
def test_probe_every_emitted_tool_list_registers_in_full(tmp_path):
    for tools in _emitted_tool_lists():
        registered = _registered_tools(tmp_path, tools)
        missing = [tool for tool in tools if tool not in registered]
        assert missing == [], f"Claude Code dropped {missing} from {tools}"
