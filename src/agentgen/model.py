"""Variant and role-template loading, and frontmatter merge."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import AgentGenError

# A role id is a ccc agent type, a file stem, and a spawn-reference token;
# all three share this shape. Its no-dot grammar also excludes ccc's reserved
# `.claude` and `.codex` suffixes.
ROLE_ID_PATTERN = r"[a-z][a-z0-9-]*"
ROLE_ID_RE = re.compile(rf"^{ROLE_ID_PATTERN}$")

# A variant name becomes a directory name under `build/`.
VARIANT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# ccc accepts exactly these frontmatter keys.
HARNESSES = ("claude", "codex")
HARNESS_KEYS = {
    "claude": ("model", "effort", "tools"),
    "codex": ("codex_model", "codex_effort", "codex_sandbox"),
}

CCC_KEYS = (
    "name",
    "description",
    "model",
    "effort",
    "tools",
    "harness",
    "codex_model",
    "codex_effort",
    "codex_sandbox",
)

# `name` is always the role id, so a role template may not set it.
ROLE_TEMPLATE_KEYS = tuple(k for k in CCC_KEYS if k != "name")

VARIANT_TABLES = ("defaults", "vars", "roles")


@dataclass(frozen=True)
class RoleSpec:
    """A role's id, merged frontmatter, and the template it renders from."""

    id: str
    frontmatter: dict[str, Any]
    template: str


@dataclass(frozen=True)
class Variant:
    """A parsed `variants/<name>.toml`."""

    name: str
    defaults: dict[str, Any] = field(default_factory=dict)
    vars: dict[str, Any] = field(default_factory=dict)
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def role_ids(self) -> list[str]:
        return list(self.roles)


def load_variant(path: Path) -> Variant:
    """Load and validate a variant TOML; raises AgentGenError on structural violations."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AgentGenError(f"variant not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise AgentGenError(f"{path}: invalid TOML: {exc}") from None

    unknown = sorted(set(data) - set(VARIANT_TABLES) - {"name"})
    if unknown:
        raise AgentGenError(f"{path}: unknown top-level key(s): {', '.join(unknown)}")

    name = data.get("name") or path.stem
    if not isinstance(name, str) or not name:
        raise AgentGenError(f"{path}: `name` must be a non-empty string")
    if not VARIANT_NAME_RE.match(name):
        raise AgentGenError(
            f"{path}: `name` {name!r} is not a usable directory name; "
            "letters, digits, `.`, `-` and `_`, starting with a letter or digit"
        )

    defaults = data.get("defaults", {})
    variant_vars = data.get("vars", {})
    roles = data.get("roles", {})
    for key, table in (("defaults", defaults), ("vars", variant_vars), ("roles", roles)):
        if not isinstance(table, dict):
            raise AgentGenError(f"{path}: `[{key}]` must be a table")

    if not roles:
        raise AgentGenError(f"{path}: no `[roles.<id>]` tables; the variant is empty")

    _reject_non_ccc_keys(defaults, f"{path}: [defaults]")
    normalized_defaults = normalize_table_harness(defaults, f"{path}: [defaults]")
    normalized_roles: dict[str, dict[str, Any]] = {}
    for role_id, overrides in roles.items():
        if not ROLE_ID_RE.match(role_id):
            raise AgentGenError(
                f"{path}: role id {role_id!r} is not a ccc agent type; "
                f"lowercase letters, digits and `-`, starting with a letter"
            )
        if not isinstance(overrides, dict):
            raise AgentGenError(f"{path}: `[roles.{role_id}]` must be a table")
        _reject_non_ccc_keys(overrides, f"{path}: [roles.{role_id}]")
        normalized_roles[role_id] = normalize_table_harness(
            overrides, f"{path}: [roles.{role_id}]"
        )
    return Variant(
        name=name,
        defaults=normalized_defaults,
        vars=dict(variant_vars),
        roles=normalized_roles,
    )


def normalize_harness(value: Any, where: str) -> list[str] | None:
    """Normalize a harness policy, preserving order and rejecting ambiguity."""
    if isinstance(value, str):
        entries: list[Any] = value.split(",")
    elif isinstance(value, list):
        entries = value
    else:
        raise AgentGenError(f"{where}: `harness` must be a string or list, not {type(value).__name__} ({value!r})")

    normalized: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            raise AgentGenError(
                f"{where}: `harness` entries must be strings, not {type(entry).__name__} ({entry!r})"
            )
        name = entry.strip().lower()
        if not name:
            continue
        if name not in HARNESSES:
            raise AgentGenError(f"{where}: unknown harness {name!r}")
        if name in normalized:
            raise AgentGenError(f"{where}: duplicate harness {name!r}")
        normalized.append(name)
    return normalized or None


def normalize_table_harness(table: dict[str, Any], where: str) -> dict[str, Any]:
    """Copy a ccc table while normalizing its optional harness policy."""
    normalized = dict(table)
    if "harness" not in normalized:
        return normalized
    policy = normalize_harness(normalized["harness"], where)
    if policy is None:
        normalized.pop("harness")
    else:
        normalized["harness"] = policy
    return normalized


def _reject_non_ccc_keys(table: dict[str, Any], where: str) -> None:
    unknown = sorted(set(table) - set(ROLE_TEMPLATE_KEYS))
    if unknown:
        raise AgentGenError(
            f"{where}: key(s) not accepted by ccc: {', '.join(unknown)}; "
            f"allowed: {', '.join(ROLE_TEMPLATE_KEYS)}"
        )


def resolve_variant_path(root: Path, name_or_path: str) -> Path:
    """A bare name resolves under `<root>/variants/`; anything else is a path."""
    candidate = Path(name_or_path)
    if candidate.suffix == ".toml" or candidate.parent != Path("."):
        return candidate
    return root / "variants" / f"{name_or_path}.toml"


def check_role_frontmatter(frontmatter: dict[str, Any], where: str) -> None:
    """Role-template frontmatter carries only ccc keys, and must set a description."""
    _reject_non_ccc_keys(frontmatter, where)
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise AgentGenError(f"{where}: `description` is required and must be non-empty")


def merge_frontmatter(
    role_id: str,
    template_frontmatter: dict[str, Any],
    defaults: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Later wins: role template < `[defaults]` < `[roles.<id>]`. `name` is the id."""
    merged: dict[str, Any] = {}
    merged.update(template_frontmatter)
    merged.update(defaults)
    merged.update(overrides)
    allowed = merged.get("harness", list(HARNESSES))
    if "harness" in merged and (
        not isinstance(allowed, list)
        or not allowed
        or any(harness not in HARNESSES for harness in allowed)
        or len(set(allowed)) != len(allowed)
    ):
        raise AgentGenError(
            f"role {role_id}: invalid normalized harness policy {allowed!r}"
        )
    for harness in HARNESSES:
        if harness not in allowed:
            for key in HARNESS_KEYS[harness]:
                merged.pop(key, None)
    merged["name"] = role_id
    return {k: merged[k] for k in CCC_KEYS if k in merged}
