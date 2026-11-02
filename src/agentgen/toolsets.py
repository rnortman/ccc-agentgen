"""The tool-set catalogue: named Claude Code tool sets and the tools no set grants."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import AgentGenError
from .model import CATALOGUE_NAME, ROLE_ID_RE

# The set every Claude-allowed role receives, whether or not it names it.
HARMLESS = "harmless"

# The top-level tables a catalogue may contain.
CATALOGUE_KEYS = ("sets", "withheld")
# The keys a `[sets.<name>]` table may set.
SET_KEYS = ("description", "tools", "include")

# An MCP tool name as Claude Code registers it.
MCP_TOOL_RE = re.compile(r"^mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ToolSet:
    """One `[sets.<name>]` entry."""

    name: str
    description: str
    tools: tuple[str, ...]
    include: tuple[str, ...]


@dataclass(frozen=True)
class Catalogue:
    """Tool sets in file order, the withheld tools with their reasons, and
    each set-granted tool's granting set."""

    path: Path
    sets: tuple[ToolSet, ...]
    withheld: dict[str, str]
    granted_by: dict[str, str]

    def _set(self, name: str) -> ToolSet | None:
        return next((s for s in self.sets if s.name == name), None)

    def granting_set(self, tool: str) -> str | None:
        """The name of the set whose own tools hold `tool`, if any."""
        return self.granted_by.get(tool)

    def classifies(self, tool: str) -> bool:
        """Whether a set grants `tool` or `[withheld]` lists it."""
        return tool in self.granted_by or tool in self.withheld

    def closure(self, names: list[str]) -> set[str]:
        """The named sets plus everything they include, transitively.

        Every name must be a set in the catalogue.
        """
        seen: set[str] = set()
        pending = list(names)
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            toolset = self._set(name)
            assert toolset is not None, name
            pending.extend(toolset.include)
        return seen

    def resolve(self, role_id: str, toolsets: Any, extras: Any) -> list[str]:
        """The `tools` list a role emits, without duplicates.

        The tools of `harmless` and of the closure of `toolsets`, in catalogue
        order, then `extras` (the role's own `tools`) in the given order.
        """
        where = f"role {role_id}"
        set_names = _string_list(toolsets, f"{where}: `toolsets`")
        extra_names = _string_list(extras, f"{where}: `tools`")
        for name in set_names:
            if self._set(name) is None:
                raise AgentGenError(
                    f"{where}: unknown tool set {name!r}; "
                    f"known: {', '.join(s.name for s in self.sets)}"
                )
        for tool in extra_names:
            granting = self.granting_set(tool)
            if granting is not None:
                raise AgentGenError(
                    f"{where}: `{tool}` is granted by tool set `{granting}`; "
                    "name the set in `toolsets`"
                )
            if tool not in self.withheld and not MCP_TOOL_RE.match(tool):
                raise AgentGenError(
                    f"{where}: unknown tool {tool!r}; Claude Code drops unknown names "
                    f"silently; classify it in {self.path}"
                )
        granted = self.closure([HARMLESS, *set_names])
        resolved: list[str] = []
        for toolset in self.sets:
            if toolset.name in granted:
                resolved.extend(toolset.tools)
        for tool in extra_names:
            if tool not in resolved:
                resolved.append(tool)
        return resolved


def _string_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise AgentGenError(f"{where} must be a list of non-empty strings, not {value!r}")
    return value


def load_catalogue(templates_dir: Path) -> Catalogue | None:
    """Load and validate `<templates_dir>/toolsets.toml`; None when the file is absent."""
    path = templates_dir / CATALOGUE_NAME
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise AgentGenError(f"{path}: invalid TOML: {exc}") from None

    def fail(message: str) -> AgentGenError:
        return AgentGenError(f"{path}: {message}")

    unknown = sorted(set(data) - set(CATALOGUE_KEYS))
    if unknown:
        raise fail(f"unknown top-level key(s): {', '.join(unknown)}; allowed: {', '.join(CATALOGUE_KEYS)}")
    raw_sets = data.get("sets", {})
    raw_withheld = data.get("withheld", {})
    if not isinstance(raw_sets, dict):
        raise fail("`sets` must be a table")
    if not isinstance(raw_withheld, dict):
        raise fail("`withheld` must be a table")

    owner: dict[str, str] = {}
    sets: list[ToolSet] = []
    for name, entry in raw_sets.items():
        where = f"[sets.{name}]"
        if not ROLE_ID_RE.match(name):
            raise fail(
                f"{where}: set name {name!r} must be lowercase letters, digits "
                "and `-`, starting with a letter"
            )
        if not isinstance(entry, dict):
            raise fail(f"{where} must be a table")
        bad_keys = sorted(set(entry) - set(SET_KEYS))
        if bad_keys:
            raise fail(f"{where}: unknown key(s): {', '.join(bad_keys)}; allowed: {', '.join(SET_KEYS)}")
        description = entry.get("description")
        if not isinstance(description, str) or not description.strip():
            raise fail(f"{where}: `description` is required and must be non-empty")
        tools = _string_list(entry.get("tools", []), f"{path}: {where}: `tools`")
        include = _string_list(entry.get("include", []), f"{path}: {where}: `include`")
        for listing, values in (("tools", tools), ("include", include)):
            repeated = sorted({v for v in values if values.count(v) > 1})
            if repeated:
                raise fail(f"{where}: `{listing}` lists {', '.join(repeated)} more than once")
        for tool in tools:
            if tool in owner:
                raise fail(f"{where}: tool `{tool}` is already granted by [sets.{owner[tool]}]")
            owner[tool] = name
        sets.append(ToolSet(name, description, tuple(tools), tuple(include)))

    withheld: dict[str, str] = {}
    for tool, reason in raw_withheld.items():
        if not isinstance(reason, str) or not reason.strip():
            raise fail(f"[withheld]: `{tool}` needs a non-empty reason string")
        if tool in owner:
            raise fail(f"[withheld]: tool `{tool}` is granted by [sets.{owner[tool]}]")
        withheld[tool] = reason

    names = {s.name for s in sets}
    for toolset in sets:
        for included in toolset.include:
            if included not in names:
                raise fail(f"[sets.{toolset.name}]: `include` names unknown set {included!r}")
    _reject_cycles(sets, fail)

    harmless = next((s for s in sets if s.name == HARMLESS), None)
    if harmless is None:
        raise fail(f"[sets.{HARMLESS}] is required: every Claude-allowed role receives it")
    if harmless.include:
        raise fail(f"[sets.{HARMLESS}] may not include another set")
    if not harmless.tools:
        raise fail(
            f"[sets.{HARMLESS}] must grant at least one tool; ccc reads an empty "
            "`tools` list as every tool"
        )
    return Catalogue(path=path, sets=tuple(sets), withheld=withheld, granted_by=owner)


def _reject_cycles(sets: list[ToolSet], fail: Any) -> None:
    includes = {s.name: s.include for s in sets}
    done: set[str] = set()

    def visit(name: str, trail: list[str]) -> None:
        if name in trail:
            cycle = " -> ".join([*trail[trail.index(name) :], name])
            raise fail(f"`include` forms a cycle: {cycle}")
        if name in done:
            return
        for included in includes[name]:
            visit(included, [*trail, name])
        done.add(name)

    for toolset in sets:
        visit(toolset.name, [])
