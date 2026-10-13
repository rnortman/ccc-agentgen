"""Variant rendering and validation shared by the build and deploy commands."""

from __future__ import annotations

import sys
from pathlib import Path

from . import AgentGenError
from .check import check_all
from .model import Variant, load_variant
from .render import RenderedRole, render_variant


def render_checked(
    variant_path: Path,
    root: Path,
    *,
    variant: Variant | None = None,
) -> tuple[Variant, list[RenderedRole]]:
    """Load, render, and check one variant without writing output."""
    templates_dir = root / "templates"
    if not templates_dir.is_dir():
        raise AgentGenError(f"--root {root} does not contain templates/")
    if variant is None:
        variant = load_variant(variant_path)
    roles = render_variant(templates_dir, variant)
    result = check_all(roles)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not result.ok:
        raise AgentGenError(
            "check failed; nothing written:\n" + "\n".join(f"  {e}" for e in result.errors)
        )
    return variant, roles
