"""Jinja environment, frontmatter split, and body rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from . import AgentGenError
from .model import HARNESSES, RoleSpec, Variant, check_role_frontmatter, merge_frontmatter, normalize_table_harness

FRONTMATTER_FENCE = "---"


@dataclass(frozen=True)
class RenderedRole:
    """One role's merged frontmatter and rendered body."""

    id: str
    frontmatter: dict[str, Any]
    body: str
    overlays: dict[str, str] = field(default_factory=dict)

    @property
    def harnesses(self) -> list[str]:
        return list(self.frontmatter.get("harness", HARNESSES))

    def body_for(self, harness: str) -> str:
        return self.overlays.get(harness, self.body)


def split_frontmatter(text: str, where: str) -> tuple[str, str]:
    """Return (frontmatter YAML, body) for a `---`-fenced document."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONTMATTER_FENCE:
        raise AgentGenError(f"{where}: file must start with a `---` frontmatter fence")
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONTMATTER_FENCE:
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise AgentGenError(f"{where}: frontmatter fence is never closed")


def parse_frontmatter(text: str, where: str) -> dict[str, Any]:
    """Parse YAML text into a mapping; raises AgentGenError on invalid input."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise AgentGenError(f"{where}: invalid YAML frontmatter: {exc}") from None
    if not isinstance(data, dict):
        raise AgentGenError(f"{where}: frontmatter must be a YAML mapping")
    return data


class RoleLoader(FileSystemLoader):
    """FileSystemLoader that blanks out a role template's literal frontmatter block."""

    def get_source(self, environment, template):  # type: ignore[override]
        source, filename, uptodate = super().get_source(environment, template)
        if template.startswith("roles/"):
            front, body = split_frontmatter(source, template)
            # Blank lines in place of the frontmatter and its two fences, so a
            # Jinja error's line number still points into the file on disk.
            source = "\n" * (len(front.splitlines()) + 2) + body
        return source, filename, uptodate


def make_environment(templates_dir: Path) -> Environment:
    """Jinja environment configured for role-template rendering."""
    return Environment(
        loader=RoleLoader(str(templates_dir)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )


def role_template_name(role_id: str) -> str:
    """Loader-relative path for a role's Jinja template."""
    return f"roles/{role_id}.md.j2"


def load_role_spec(templates_dir: Path, role_id: str, variant: Variant) -> RoleSpec:
    """Read a role template's frontmatter and merge it with the variant's tiers."""
    template = role_template_name(role_id)
    path = templates_dir / template
    if not path.is_file():
        raise AgentGenError(
            f"variant `{variant.name}` names role `{role_id}` "
            f"but there is no template at {path}"
        )
    front_text, _ = split_frontmatter(path.read_text(encoding="utf-8"), str(path))
    frontmatter = parse_frontmatter(front_text, str(path))
    check_role_frontmatter(frontmatter, str(path))
    frontmatter = normalize_table_harness(frontmatter, str(path))
    merged = merge_frontmatter(
        role_id, frontmatter, variant.defaults, variant.roles.get(role_id, {})
    )
    return RoleSpec(id=role_id, frontmatter=merged, template=template)


def render_variant(templates_dir: Path, variant: Variant) -> list[RenderedRole]:
    """Render every role in the variant. Raises on the first failure."""
    env = make_environment(templates_dir)
    specs = [load_role_spec(templates_dir, rid, variant) for rid in variant.role_ids]
    role_ids = sorted(spec.id for spec in specs)
    rendered: list[RenderedRole] = []
    for spec in specs:
        harnesses = list(spec.frontmatter.get("harness", HARNESSES))
        bodies: list[str] = []
        for harness in harnesses:
            context = {
                "role": {"id": spec.id, **spec.frontmatter},
                "roles": role_ids,
                "vars": variant.vars,
                "variant": variant.name,
                "harness": harness,
                "harnesses": harnesses,
            }
            try:
                # The loader's line-number padding leads the render; drop it.
                bodies.append(env.get_template(spec.template).render(context).lstrip("\n"))
            except TemplateError as exc:
                lineno = getattr(exc, "lineno", None)
                # Jinja names the template the error is actually in, which for an
                # include or a macro call is a partial, not the role template.
                name = getattr(exc, "name", None) or spec.template
                where = f"{name}, line {lineno}" if lineno else name
                raise AgentGenError(f"{where}: {type(exc).__name__}: {exc}") from None
        base = bodies[0]
        overlays = {
            harness: body for harness, body in zip(harnesses[1:], bodies[1:]) if body != base
        }
        rendered.append(
            RenderedRole(id=spec.id, frontmatter=spec.frontmatter, body=base, overlays=overlays)
        )
    return rendered
