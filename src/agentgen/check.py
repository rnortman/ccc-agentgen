"""ccc-contract validation of rendered definitions, plus the spawn-reference rule."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .model import CCC_KEYS, HARNESSES, ROLE_ID_PATTERN
from .render import RenderedRole

CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
CODEX_SANDBOXES = ("read-only", "workspace-write")

# ccc reads a blank value as an absent one, and every one of these must be a
# string.
STRING_KEYS = ("description", "model", "effort", "codex_model", "codex_effort", "codex_sandbox")

MAX_BODY_BYTES = 100 * 1024
WARN_BODY_BYTES = 64 * 1024

# `ccc run <role>` / `ccc launch <role>` with a bare lowercase identifier.
# A placeholder (`<type>`), a shell variable (`$AGENT`) or a Jinja remnant does
# not match, and so is not a spawn reference.
SPAWN_RE = re.compile(rf"\bccc\s+(?:run|launch)\s+({ROLE_ID_PATTERN})\b")


@dataclass
class CheckResult:
    """Accumulated errors and warnings from validation."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def spawn_references(body: str) -> set[str]:
    """Role ids this body tells an agent to spawn."""
    return set(SPAWN_RE.findall(body))


def check_role(role: RenderedRole, role_ids: Iterable[str]) -> CheckResult:
    """Apply every rule to one rendered definition."""
    result = CheckResult()
    known = set(role_ids)
    front = role.frontmatter
    where = f"{role.id}.md"

    value: dict[str, str] = {}
    for key in STRING_KEYS:
        if key not in front:
            continue
        raw = front[key]
        if not isinstance(raw, str):
            result.errors.append(
                f"{where}: `{key}` must be a string, not {type(raw).__name__} ({raw!r})"
            )
        elif raw.strip():
            value[key] = raw.strip()

    # Rule 1 — key set, name/stem, description, and harness policy.
    unknown = sorted(set(front) - set(CCC_KEYS))
    if unknown:
        result.errors.append(f"{where}: frontmatter key(s) not accepted by ccc: {', '.join(unknown)}")
    if front.get("name") != role.id:
        result.errors.append(
            f"{where}: `name` is {front.get('name')!r} but must equal the file stem {role.id!r}"
        )
    if "description" not in value:
        result.errors.append(f"{where}: `description` is required and must be non-empty")
    policy = front.get("harness")
    policy_valid = True
    if policy is not None:
        if not isinstance(policy, list) or not policy:
            result.errors.append(f"{where}: `harness` must be a non-empty list")
            policy_valid = False
            allowed = []
        else:
            allowed = []
            for harness in policy:
                if not isinstance(harness, str):
                    result.errors.append(
                        f"{where}: `harness` entries must be strings, not {type(harness).__name__} ({harness!r})"
                    )
                    policy_valid = False
                elif harness not in HARNESSES:
                    result.errors.append(f"{where}: unknown harness {harness!r}")
                    policy_valid = False
                elif harness in allowed:
                    result.errors.append(f"{where}: duplicate harness {harness!r}")
                    policy_valid = False
                else:
                    allowed.append(harness)
    else:
        allowed = list(HARNESSES)

    if policy_valid:
        for harness in allowed:
            model_key = "model" if harness == "claude" else "codex_model"
            if model_key not in value:
                if policy is None:
                    other = "codex" if harness == "claude" else "claude"
                    result.errors.append(
                        f"{where}: names no {model_key}; either add one or set harness: {other}"
                    )
                else:
                    result.errors.append(f"{where}: allows {harness} but names no {model_key}")

    # Rule 2 — effort vocabularies and sandbox.
    if "model" in value and "effort" in value and value["effort"] not in CLAUDE_EFFORTS:
        result.errors.append(
            f"{where}: `effort: {value['effort']}` is not a Claude effort "
            f"({', '.join(CLAUDE_EFFORTS)})"
        )
    if (
        "codex_model" in value
        and "codex_effort" in value
        and value["codex_effort"] not in CODEX_EFFORTS
    ):
        result.errors.append(
            f"{where}: `codex_effort: {value['codex_effort']}` is not a Codex effort "
            f"({', '.join(CODEX_EFFORTS)})"
        )
    if "codex_sandbox" in value and value["codex_sandbox"] not in CODEX_SANDBOXES:
        result.errors.append(
            f"{where}: `codex_sandbox: {value['codex_sandbox']}` is not one of "
            f"{', '.join(CODEX_SANDBOXES)}"
        )

    def check_body(body: str, body_where: str, overlay: bool = False) -> None:
        if overlay and body.startswith("\ufeff"):
            body = body[1:]
        if overlay:
            body = body.strip()
        size = len(body.encode("utf-8"))
        if not body.strip():
            result.errors.append(f"{body_where}: body is empty")
        if size > MAX_BODY_BYTES:
            result.errors.append(
                f"{body_where}: body is {size} bytes, over ccc's {MAX_BODY_BYTES}-byte ceiling"
            )
        elif size > WARN_BODY_BYTES:
            result.warnings.append(
                f"{body_where}: body is {size} bytes; over {WARN_BODY_BYTES} it can still overflow "
                "the argv ceiling once JSON/TOML-escaped"
            )
        if overlay and next((line.strip() for line in body.splitlines() if line.strip()), None) == "---":
            result.errors.append(f"{body_where}: overlay body must not start with a frontmatter fence")
        for target in sorted(spawn_references(body)):
            if target not in known:
                result.errors.append(
                    f"{body_where}: spawns `{target}`, which is not a role in this variant"
                )

    check_body(role.body, where)
    for harness, body in role.overlays.items():
        check_body(body, f"{role.id}.{harness}.md", overlay=True)

    return result


def check_all(roles: list[RenderedRole]) -> CheckResult:
    """Validate all roles, aggregating errors and warnings into one result."""
    combined = CheckResult()
    role_ids = [role.id for role in roles]
    for role in roles:
        one = check_role(role, role_ids)
        combined.errors.extend(one.errors)
        combined.warnings.extend(one.warnings)
    return combined
