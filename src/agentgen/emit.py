"""Output-directory hygiene and definition serialisation."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import yaml

from . import AgentGenError
from .model import CCC_KEYS, HARNESSES
from .render import FRONTMATTER_FENCE, RenderedRole, parse_frontmatter, split_frontmatter

# Written into an output directory to mark it as agentgen's to overwrite.
MARKER_NAME = ".agentgen-build"


def _is_dir_without_following(path: Path) -> bool:
    return stat.S_ISDIR(os.lstat(path).st_mode)


class _Dumper(yaml.SafeDumper):
    """One key per line, with sequences (`tools`) inline."""


_Dumper.add_representer(
    list,
    lambda dumper, data: dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True),
)


def serialize(role: RenderedRole) -> str:
    """A ccc agent definition: `---` fenced YAML frontmatter, then the body."""
    ordered: dict[str, Any] = {k: role.frontmatter[k] for k in CCC_KEYS if k in role.frontmatter}
    front = yaml.dump(
        ordered,
        Dumper=_Dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10_000,
    )
    body = role.body if role.body.endswith("\n") else role.body + "\n"
    return f"{FRONTMATTER_FENCE}\n{front}{FRONTMATTER_FENCE}\n{body}"


def read_definition(path: Path) -> RenderedRole:
    """Parse an emitted `<id>.md` back into a role; the inverse of `serialize`."""
    front_text, body = split_frontmatter(path.read_text(encoding="utf-8"), path.name)
    return RenderedRole(
        id=path.stem,
        frontmatter=parse_frontmatter(front_text, path.name),
        body=body,
    )


def read_build(out_dir: Path) -> list[RenderedRole]:
    """Read base definitions and their body-only harness overlays."""
    overlays: dict[str, dict[str, Path]] = {}
    bases: list[Path] = []
    for path in sorted(out_dir.glob("*.md")):
        harness = next((h for h in HARNESSES if path.name.endswith(f".{h}.md")), None)
        if harness is None:
            bases.append(path)
        else:
            role_id = path.name[: -len(f".{harness}.md")]
            overlays.setdefault(role_id, {})[harness] = path
    role_list = [read_definition(path) for path in bases]
    by_id = {role.id: role for role in role_list}
    for role_id, paths in overlays.items():
        if role_id not in by_id:
            path = next(iter(paths.values()))
            raise AgentGenError(f"overlay {path.name} has no corresponding base definition")
    return [
        RenderedRole(
            id=role.id,
            frontmatter=role.frontmatter,
            body=role.body,
            overlays={
                harness: path.read_text(encoding="utf-8")
                for harness, path in overlays.get(role.id, {}).items()
            },
        )
        for role in role_list
    ]


def prepare_out_dir(out_dir: Path) -> None:
    """Create the directory, or clear a previous build agentgen owns out of it."""
    if not out_dir.exists():
        out_dir.mkdir(parents=True)
        return
    check_out_dir(out_dir)
    for stale in out_dir.glob("*.md"):
        stale.unlink()


def check_out_dir(out_dir: Path) -> None:
    """Check that an output directory is absent, empty, or safe to rebuild."""
    if out_dir.is_symlink():
        raise AgentGenError(f"output path is a symlink: {out_dir}")
    if not out_dir.exists():
        return
    if not _is_dir_without_following(out_dir):
        raise AgentGenError(f"output path is not a directory: {out_dir}")
    entries = sorted(os.scandir(out_dir), key=lambda entry: entry.name)
    if not entries:
        return
    marker = next((entry for entry in entries if entry.name == MARKER_NAME), None)
    if marker is None or not marker.is_file(follow_symlinks=False):
        raise AgentGenError(
            f"output directory {out_dir} is not empty and holds no {MARKER_NAME} marker, "
            f"so it is not a previous build; refusing to delete anything in it"
        )
    strays = sorted(
        entry.name
        for entry in entries
        if entry.name != MARKER_NAME
        and (not entry.name.endswith(".md") or not entry.is_file(follow_symlinks=False))
    )
    if strays:
        raise AgentGenError(
            f"output directory {out_dir} holds content that is not a previous build: "
            f"{', '.join(strays)}"
        )


def write_all(out_dir: Path, roles: list[RenderedRole], variant_name: str) -> list[Path]:
    """Serialize every role to ``<out_dir>/<id>.md``, returning the paths written."""
    prepare_out_dir(out_dir)
    written: list[Path] = []
    try:
        (out_dir / MARKER_NAME).write_text(f"{variant_name}\n", encoding="utf-8")
        for role in roles:
            path = out_dir / f"{role.id}.md"
            path.write_text(serialize(role), encoding="utf-8")
            written.append(path)
            for harness, body in role.overlays.items():
                overlay_path = out_dir / f"{role.id}.{harness}.md"
                overlay_body = body if body.endswith("\n") else body + "\n"
                overlay_path.write_text(overlay_body, encoding="utf-8")
                written.append(overlay_path)
    except OSError as exc:
        done = ", ".join(p.name for p in written) or "nothing"
        raise AgentGenError(
            f"writing {out_dir}: {exc}; {done} written before the failure, "
            f"so the directory holds a partial build"
        ) from None
    return written
