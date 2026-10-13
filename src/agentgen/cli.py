"""Command line for building and deploying ccc agent definitions."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from . import AgentGenError
from .build import render_checked
from .emit import write_all
from .model import resolve_variant_path
from .model import HARNESSES


def build(variant_name: str, root: Path, out: Path | None) -> list[Path]:
    """Render, check, and only then write. Raises AgentGenError on any violation."""
    templates_dir = root / "templates"
    if not templates_dir.is_dir():
        raise AgentGenError(f"--root {root} does not contain templates/")

    variant_path = resolve_variant_path(root, variant_name)
    variants_dir = root / "variants"
    if variant_path.parent == variants_dir and not variants_dir.is_dir():
        raise AgentGenError(f"--root {root} does not contain variants/")
    variant, roles = render_checked(variant_path, root)

    out_dir = out if out is not None else root / "build" / variant.name
    return write_all(out_dir, roles, variant.name)


def make_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser for the ``agentgen`` CLI."""
    parser = argparse.ArgumentParser(prog="agentgen", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build", help="render a variant into ccc agent definitions")
    build_cmd.add_argument("variant", help="variant name, or a path to a variant TOML")
    build_cmd.add_argument("--out", type=Path, default=None, help="output directory")
    build_cmd.add_argument(
        "--root", type=Path, default=None, help="directory holding templates/ and variants/"
    )
    deploy_cmd = sub.add_parser("deploy", help="install every variant into ccc")
    deploy_cmd.add_argument(
        "--root", type=Path, default=None, help="directory holding templates/ and variants/"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns 0 on success, 1 on validation failure."""
    args = make_parser().parse_args(argv)
    root = (args.root or Path.cwd()).resolve()
    try:
        if args.command == "build":
            written = build(args.variant, root, args.out)
        else:
            from .deploy import deploy

            result = deploy(root)
    except AgentGenError as exc:
        print(f"agentgen: {exc}", file=sys.stderr)
        return 1
    except UnicodeDecodeError as exc:
        print(f"agentgen: a template or variant file is not valid UTF-8: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        where = f" ({exc.filename})" if exc.filename else ""
        print(f"agentgen: {exc.strerror or exc}{where}", file=sys.stderr)
        return 1
    if args.command == "build":
        print_build_summary(written)
    else:
        for variant, paths in result.variants:
            print_build_summary(paths)
        print(f"composed {result.config_count} configs under {result.configs_dir}")
        print(f"source {shlex.quote(str(result.switcher_path))}")
        if result.stale_variant_dirs:
            paths = ", ".join(str(path) for path in result.stale_variant_dirs)
            print(f"note: stale generated variant directories were not removed: {paths}", file=sys.stderr)
    return 0


def print_build_summary(written: list[Path]) -> None:
    """Print the stable build summary for one emitted variant."""
    if not written:
        print("wrote nothing")
        return
    overlays = sum(
        any(path.name.endswith(f".{harness}.md") for harness in HARNESSES) for path in written
    )
    definitions = len(written) - overlays
    overlay_word = "overlay" if overlays == 1 else "overlays"
    suffix = f" and {overlays} {overlay_word}" if overlays else ""
    print(f"wrote {definitions} definitions to {written[0].parent}{suffix}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
